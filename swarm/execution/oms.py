"""Durable order intents, deterministic risk recheck, reconciliation, managed exits."""

import asyncio
import hashlib
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal

import ccxt
import structlog

from swarm.execution.exchange import read_retry, safe_error
from swarm.models import Fill, Proposal, RiskDecision
from swarm.risk.gate import Limits, PortfolioState, check, slippage_curve
from swarm.telemetry import NullTelemetry

log = structlog.get_logger()
ACTIVE = {"intent", "unknown", "open", "partially_filled"}


def client_id(proposal):
    return "swarm-" + hashlib.sha256(proposal.model_dump_json().encode()).hexdigest()[:26]


def now_utc():
    return datetime.now(UTC)


TIME_STOP_S = 14400


def exit_reason(entry, cost_rate, bid, age_s):
    """Managed-exit trigger shared by the live OMS and the backtester.

    Take-profit at +1.5x the estimated round-trip cost, stop-loss at -1x, time-stop 4h.
    """
    if bid >= entry * (1 + 1.5 * cost_rate):
        return "take_profit"
    if bid <= entry * (1 - cost_rate):
        return "stop_loss"
    if age_s >= TIME_STOP_S:
        return "time_stop"
    return None


def price_at_tick(book, tick, side):
    bid, ask = Decimal(str(book["bids"][0][0])), Decimal(str(book["asks"][0][0]))
    tick = Decimal(str(tick))
    target = bid + tick if side == "buy" else ask - tick
    rounding = ROUND_DOWN if side == "buy" else ROUND_UP
    return float((target / tick).to_integral_value(rounding=rounding) * tick)


def empty_state():
    return {
        "orders": {},
        "balances": {},
        "fills": [],
        "lots": {},
        "day": None,
        "baseline": 0,
        "breached": False,
        "ts": now_utc().isoformat(),
    }


class OMS:
    def __init__(
        self, exchange, repository, symbols, limits=None, *, clock=now_utc, telemetry=None
    ):
        self.exchange, self.repository, self.symbols = exchange, repository, symbols
        self.limits = limits or Limits()
        self.telemetry = telemetry or NullTelemetry()
        self.marked = None  # Latest marked equity/cash/inventory from _portfolio.
        self.clock, self.lock = clock, asyncio.Lock()
        self.state = empty_state()
        self.ready = False
        self.books, self.balance = {}, {}
        self.stage = "initializing"

    async def start(self):
        self.state = await self.repository.load() or empty_state()
        await self.reconcile()

    async def _save(self, fills=()):
        self.state["ts"] = self.clock().isoformat()
        await self.repository.save(self.state, fills)

    async def _market_snapshot(self):
        self.stage = "fetch_balance"
        self.balance = await read_retry(self.exchange.fetch_balance)
        for symbol in self.symbols:
            self.stage = "fetch_order_book"
            book = await read_retry(self.exchange.fetch_order_book, symbol, 20)
            if not book["bids"] or not book["asks"] or book["bids"][0][0] >= book["asks"][0][0]:
                raise ValueError("Invalid market book")
            self.books[symbol] = book

    def _mid(self, symbol):
        book = self.books[symbol]
        return (book["bids"][0][0] + book["asks"][0][0]) / 2

    def _portfolio(self, proposal):
        now = self.clock()
        total, free = self.balance["total"], self.balance["free"]
        # Account is restricted to configured universe + USDT for valuation.
        positions = {s: float(total.get(s.split("/")[0], 0)) * self._mid(s) for s in self.symbols}
        equity = float(total.get("USDT", 0)) + sum(positions.values())
        self.marked = dict(
            equity=equity, cash=float(total.get("USDT", 0)), inventory_value=sum(positions.values())
        )
        for order in self.state["orders"].values():
            if order["status"] in ACTIVE and order["side"] == "buy":
                positions[order["symbol"]] += (
                    max(0, order["amount"] - order.get("filled", 0)) * order["price"]
                )
        day = now.date().isoformat()
        if self.state["day"] != day:
            self.state.update(day=day, baseline=equity, breached=False)
        baseline = self.state["baseline"]
        if baseline <= 0 or equity <= 0:
            raise ValueError("No valued equity available")
        if (baseline - equity) / baseline >= self.limits.max_daily_drawdown_pct:
            self.state["breached"] = True
        active_sells = sum(
            max(0, o["amount"] - o.get("filled", 0))
            for o in self.state["orders"].values()
            if o["symbol"] == proposal.symbol and o["side"] == "sell" and o["status"] in ACTIVE
        )
        if proposal.direction == -1:
            base = proposal.symbol.split("/")[0]
            available = min(
                float(free.get(base, 0)), max(0, float(total.get(base, 0)) - active_sells)
            )
            positions[proposal.symbol] = available * self._mid(proposal.symbol)
        times = [
            datetime.fromisoformat(o["ts"])
            for o in self.state["orders"].values()
            if o["status"] != "rejected"
        ]
        last = {}
        for o in self.state["orders"].values():
            if o["status"] != "rejected":
                t = datetime.fromisoformat(o["ts"])
                last[o["symbol"]] = max(last.get(o["symbol"], t), t)
        levels = self.books[proposal.symbol]["asks" if proposal.direction == 1 else "bids"]
        curve = slippage_curve(levels, self._mid(proposal.symbol))
        return PortfolioState(
            ts=now,
            equity=equity,
            day_start_equity=baseline,
            day_start_ts=now,
            positions=positions,
            trades=times,
            last_trade_by_symbol=last,
            drawdown_breached_at=now if self.state["breached"] else None,
            slippage_curve=curve,
        )

    async def submit(self, decision: RiskDecision, *, exit_lot=None):
        async with self.lock:
            if decision.verdict not in {"ALLOW", "RESIZE"} or not self.ready:
                return None
            p = decision.proposal.model_copy(
                update={"size_quote": min(decision.size_quote, decision.proposal.size_quote)}
            )
            cid = client_id(decision.proposal)
            if cid in self.state["orders"]:
                return cid  # Durable deduplication includes unknown/rejected intents.
            if not 0 <= (self.clock() - p.ts).total_seconds() < 60:
                return None
            try:
                await self._market_snapshot()
                state = self._portfolio(p)
                verdict = check(p, state, self.limits)
                await self.telemetry.risk_decision(verdict, "oms", self.clock())
                await (
                    self._save()
                )  # Persist daily baseline and drawdown latch before any I/O order.
                if verdict.verdict == "REJECT":
                    return None
                side = "buy" if p.direction == 1 else "sell"
                market = self.exchange.market(p.symbol)
                tick = market["precision"]["price"]
                price = price_at_tick(self.books[p.symbol], tick, side)
                # Size quote is a hard cap at order limit price; never round quantity upward.
                amount = float(
                    self.exchange.amount_to_precision(
                        p.symbol, verdict.size_quote / max(price, self._mid(p.symbol))
                    )
                )
                if side == "sell":
                    amount = float(
                        self.exchange.amount_to_precision(
                            p.symbol, min(amount, self.balance["free"].get(market["base"], 0))
                        )
                    )
                min_amount = market.get("limits", {}).get("amount", {}).get("min") or 0
                min_cost = market.get("limits", {}).get("cost", {}).get("min") or 0
                if amount <= 0 or amount < min_amount or amount * price < min_cost:
                    log.info("order_skipped", reason="exchange_minimum", symbol=p.symbol)
                    return None
                if side == "buy" and amount * price * 1.002 > self.balance["free"].get("USDT", 0):
                    return None
                # Check HALT again immediately before creating the durable intent.
                if check(p, state, self.limits).verdict == "REJECT":
                    return None
                self.state["orders"][cid] = dict(
                    symbol=p.symbol,
                    ts=self.clock().isoformat(),
                    side=side,
                    amount=amount,
                    price=price,
                    filled=0.0,
                    status="intent",
                    id=None,
                    exit_lot=exit_lot,
                    cost_rate=0.002 + abs(price / self._mid(p.symbol) - 1),
                )
                await self._save()
                try:
                    order = await self.exchange.create_order(
                        p.symbol, "limit", side, amount, price, {"newClientOrderId": cid}
                    )
                except (ccxt.InsufficientFunds, ccxt.InvalidOrder) as exc:
                    self.state["orders"][cid]["status"] = "rejected"
                    log.warning("order_rejected", error_type=type(exc).__name__)
                except Exception as exc:
                    # Includes ambiguous timeouts/5xx. Never blindly retry a submission.
                    self.state["orders"][cid]["status"] = "unknown"
                    self.ready = False
                    log.warning("order_unknown", client_order_id=cid, error_type=type(exc).__name__)
                else:
                    self._apply_order(cid, order)
                await self._save()
                return cid
            except Exception as exc:
                self.ready = False
                log.warning("execution_paused", error_type=type(exc).__name__)
                return None

    def _apply_order(self, cid, remote):
        order = self.state["orders"][cid]
        order.update(
            id=remote.get("id"),
            status=remote.get("status") or "unknown",
            filled=float(remote.get("filled") or 0),
        )

    async def reconcile(self):
        async with self.lock:
            self.ready = False
            discrepancies, unresolved, new_fills = 0, 0, []
            try:
                self.stage = "fetch_orders_and_trades"
                for cid, local in list(self.state["orders"].items()):
                    if local["status"] == "rejected":
                        continue
                    try:
                        remote = await read_retry(
                            self.exchange.fetch_order,
                            None,
                            local["symbol"],
                            {"origClientOrderId": cid},
                        )
                    except ccxt.OrderNotFound:
                        unresolved += 1
                        continue
                    if local["status"] != remote["status"] or local.get("filled", 0) != (
                        remote.get("filled") or 0
                    ):
                        discrepancies += 1
                    self._apply_order(cid, remote)
                    if (
                        remote["status"] == "open"
                        and (self.clock() - datetime.fromisoformat(local["ts"])).total_seconds()
                        >= 60
                    ):
                        # Cancel aging limits before a later cycle may reprice an exit.
                        # A cancel timeout leaves the order active; reconciliation retries reads.
                        await self.exchange.cancel_order(remote["id"], local["symbol"])
                        remote = await read_retry(
                            self.exchange.fetch_order,
                            None,
                            local["symbol"],
                            {"origClientOrderId": cid},
                        )
                        self._apply_order(cid, remote)
                    trades = await read_retry(
                        self.exchange.fetch_my_trades,
                        local["symbol"],
                        None,
                        1000,
                        {"orderId": remote["id"]},
                    )
                    if (
                        abs(sum(t["amount"] for t in trades) - float(remote.get("filled") or 0))
                        > 1e-9
                    ):
                        unresolved += 1  # Incomplete trade history must not be silently accepted.
                    for trade in trades:
                        fid = local["symbol"] + ":" + str(trade["id"])
                        if fid in self.state["fills"]:
                            continue
                        fee = trade.get("fee") or {"cost": 0, "currency": "USDT"}
                        fill = Fill(
                            fill_id=fid,
                            client_order_id=cid,
                            symbol=local["symbol"],
                            ts=datetime.fromtimestamp(trade["timestamp"] / 1000, UTC),
                            side=local["side"],
                            quantity=trade["amount"],
                            price=trade["price"],
                            fee=max(0, fee["cost"] or 0),
                            fee_currency=fee["currency"] or "USDT",
                        )
                        new_fills.append(fill)
                        self.state["fills"].append(fid)
                        if fill.side == "buy":
                            quantity = fill.quantity - (
                                fill.fee
                                if fill.fee_currency == local["symbol"].split("/")[0]
                                else 0
                            )
                            self.state["lots"][fid] = {
                                "symbol": fill.symbol,
                                "quantity": quantity,
                                "entry": fill.price,
                                "ts": fill.ts.isoformat(),
                                "cost_rate": local.get("cost_rate", 0.002),
                            }
                        else:
                            remaining = fill.quantity
                            keys = list(self.state["lots"])
                            lid = local.get("exit_lot")
                            if lid in keys:
                                keys.remove(lid)
                                keys.insert(0, lid)
                            for key in keys:
                                lot = self.state["lots"][key]
                                if lot["symbol"] == fill.symbol:
                                    reduction = min(lot["quantity"], remaining)
                                    lot["quantity"] -= reduction
                                    remaining -= reduction
                await self._market_snapshot()
                balances = {
                    s: float(self.balance["total"].get(s.split("/")[0], 0)) for s in self.symbols
                }
                if self.state["balances"] and balances != self.state["balances"]:
                    discrepancies += 1
                self.state["balances"] = balances
                for symbol, held in balances.items():
                    for lot in self.state["lots"].values():
                        if lot["symbol"] == symbol:
                            allowed = min(lot["quantity"], held)
                            if allowed != lot["quantity"]:
                                discrepancies += 1
                            lot["quantity"] = allowed
                            held -= allowed
                # External orders reserve exchange balances; pause instead of taking ownership.
                self.stage = "fetch_open_orders"
                external = await read_retry(self.exchange.fetch_open_orders)
                unresolved += sum(
                    o.get("clientOrderId") not in self.state["orders"] for o in external
                )
                # Update drawdown every cycle, not just when there is a new proposal.
                probe = Proposal(
                    symbol=self.symbols[0],
                    ts=self.clock(),
                    direction=1,
                    score=1,
                    size_quote=1,
                    signals=[],
                )
                self.stage = "portfolio_and_persistence"
                self._portfolio(probe)
                await self._save(new_fills)
                self.ready = unresolved == 0
                await self.telemetry.equity(self.clock(), **self.marked)
                if discrepancies or unresolved:
                    await self.telemetry.event(
                        "reconcile_discrepancy",
                        detail=f"corrected={discrepancies} unresolved={unresolved}",
                        ts=self.clock(),
                    )
                log.log(
                    30 if discrepancies or unresolved else 20,
                    "reconcile",
                    corrected_discrepancies=discrepancies,
                    remaining_discrepancies=unresolved,
                )
                return unresolved
            except Exception as exc:
                log.warning("reconcile_failed", stage=self.stage, **safe_error(exc))
                # Reload last durable state: in-memory fill IDs must not mask unsaved fills.
                self.state = await self.repository.load() or empty_state()
                return -1

    async def exits(self):
        for lid, lot in list(self.state["lots"].items()):
            if lot["quantity"] <= 0 or not self.ready:
                continue
            if any(
                o.get("exit_lot") == lid and o["status"] in ACTIVE
                for o in self.state["orders"].values()
            ):
                continue
            symbol = lot["symbol"]
            bid = self.books[symbol]["bids"][0][0]
            age = (self.clock() - datetime.fromisoformat(lot["ts"])).total_seconds()
            reason = exit_reason(lot["entry"], lot["cost_rate"], bid, age)
            if reason:
                p = Proposal(
                    symbol=symbol,
                    ts=self.clock(),
                    direction=-1,
                    score=-1,
                    size_quote=lot["quantity"] * bid,
                    signals=[],
                )
                await self.submit(
                    RiskDecision(
                        proposal=p, verdict="ALLOW", size_quote=p.size_quote, reason=reason
                    ),
                    exit_lot=lid,
                )
