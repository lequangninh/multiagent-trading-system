"""Event-driven replay through the production agent, consensus and risk code.

Every minute at which a stored 1m candle closes is a decision point. The replay
mirrors the paper runner's cycle (exits, then signals -> consensus -> risk ->
execution) but fills against ``SimExchange`` instead of the testnet. Agent signals
depend only on market data, so they are cached across parameter sets; the
walk-forward tuner therefore re-runs only consensus, risk and execution per grid cell.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import Field

from swarm.agents.base import MarketState
from swarm.agents.fairvalue import FairValue
from swarm.agents.liquidity import Liquidity
from swarm.agents.momentum import Momentum
from swarm.agents.scanner import Scanner
from swarm.backtest.data import TIMEFRAMES, Dataset
from swarm.backtest.metrics import compute_metrics
from swarm.backtest.sim import SimExchange
from swarm.consensus.engine import DIRECTIONAL, propose
from swarm.execution.oms import exit_reason
from swarm.models import Model, Positive, Proposal, Signal
from swarm.risk.gate import Limits, PortfolioState, decide, slippage_curve

BOOK_MAX_AGE_S = 60  # Same freshness the liquidity agent demands.
SENTIMENT_TTL_S = 600
LOOKBACK = timedelta(hours=26)


class Params(Model):
    threshold: float = Field(default=0.70, gt=0, le=1)
    base_size: Positive = 100
    weights: dict[str, float] = Field(
        default_factory=lambda: dict(
            scanner=1.0, fairvalue=1.0, liquidity=1.0, momentum=1.0, sentiment=1.0
        )
    )

    def label(self) -> str:
        directional = "+".join(a for a in sorted(DIRECTIONAL) if self.weights.get(a, 0) > 0)
        return f"threshold={self.threshold:g} weights={directional or 'none'}"


def default_grid() -> list[Params]:
    presets = {
        "fairvalue+momentum+sentiment": dict(fairvalue=1.0, momentum=1.0, sentiment=1.0),
        "fairvalue+momentum": dict(fairvalue=1.0, momentum=1.0, sentiment=0.0),
        "momentum": dict(fairvalue=0.0, momentum=1.0, sentiment=0.0),
        "fairvalue": dict(fairvalue=1.0, momentum=0.0, sentiment=0.0),
    }
    return [
        Params(threshold=t, weights=dict(scanner=1.0, liquidity=1.0) | w)
        for w in presets.values()
        for t in (0.5, 0.6, 0.7, 0.8, 0.9)
    ]


@dataclass
class Result:
    params: Params
    start: datetime
    end: datetime
    equity: list[dict]
    trades: list[dict]
    metrics: dict
    counters: Counter = field(default_factory=Counter)
    windows: list[dict] = field(default_factory=list)

    def report(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "params": self.params.model_dump(),
            **self.metrics,
            "counters": dict(sorted(self.counters.items())),
            **({"walk_forward": self.windows} if self.windows else {}),
        }


@dataclass
class _Lot:
    id: str
    symbol: str
    quantity: float
    initial_quantity: float
    entry: float
    ts: datetime
    cost_rate: float
    fee: float


class _Session:
    """Mutable state of one replay run."""

    def __init__(self, cash, fee_rate):
        self.exchange = SimExchange(cash, fee_rate=fee_rate)
        self.lots: dict[str, _Lot] = {}
        self.order_times: list[datetime] = []
        self.last_trade: dict[str, datetime] = {}
        self.marks: dict[str, float] = {}
        self.day = None
        self.baseline = None
        self.breached = False
        self.equity_rows: list[dict] = []
        self.trade_rows: list[dict] = []
        self.counters: Counter = Counter()

    def equity(self):
        return self.exchange.cash + self.inventory_value()

    def inventory_value(self):
        return sum(q * self.marks.get(s, 0) for s, q in self.exchange.inventory.items())


class Backtester:
    def __init__(
        self,
        dataset: Dataset,
        limits: Limits | None = None,
        *,
        initial_cash: float = 10_000,
        fee_rate: float = 0.001,
    ):
        self.dataset, self.limits = dataset, limits or Limits()
        self.initial_cash, self.fee_rate = initial_cash, fee_rate
        self.agents = (Scanner(), FairValue(), Liquidity(), Momentum())
        self._signals: dict[tuple, list[Signal]] = {}

    async def signals(self, symbol: str, now: datetime, size_quote: float) -> list[Signal]:
        key = (symbol, now, size_quote)
        if key not in self._signals:
            data = self.dataset[symbol]
            book = data.book_at(now)
            state = MarketState(
                ts=now,
                size_quote=size_quote,
                candles={tf: data.window(tf, now - LOOKBACK, now) for tf in TIMEFRAMES},
                book=book,
            )
            found = [s for a in self.agents if (s := await a.analyze(symbol, state)) is not None]
            row = data.sentiment_at(now)
            if row and (now - row["ts"]).total_seconds() < SENTIMENT_TTL_S:
                score = row["score"]
                found.append(
                    Signal(
                        agent="sentiment",
                        symbol=symbol,
                        ts=row["ts"],
                        direction=1 if score > 0 else -1 if score < 0 else 0,
                        confidence=abs(score) * row["confidence"],
                        rationale=row["summary"],
                        ttl_s=SENTIMENT_TTL_S,
                    )
                )
            self._signals[key] = found
        return self._signals[key]

    def _portfolio(self, s: _Session, proposal: Proposal, now, book) -> PortfolioState:
        symbols = list(self.dataset.symbols)
        positions = {sym: s.exchange.inventory.get(sym, 0) * s.marks.get(sym, 0) for sym in symbols}
        if proposal.direction == -1:
            positions[proposal.symbol] = (
                s.exchange.inventory.get(proposal.symbol, 0) * s.marks[proposal.symbol]
            )
        s.order_times = [t for t in s.order_times if (now - t).total_seconds() < 3600]
        top = SimExchange.top(book)
        levels, mid = ((top[1] if proposal.direction == 1 else top[0]), top[2]) if top else ([], 1)
        return PortfolioState(
            ts=now,
            equity=s.equity(),
            day_start_equity=s.baseline,
            day_start_ts=now,
            positions=positions,
            trades=list(s.order_times),
            last_trade_by_symbol=dict(s.last_trade),
            drawdown_breached_at=now if s.breached else None,
            slippage_curve=slippage_curve(levels, mid),
        )

    def _execute(self, s: _Session, decision, book, now, reason, exit_lot=None):
        p = decision.proposal
        # A risk RESIZE on an exit scales the lot quantity by the approved fraction.
        fraction = min(decision.size_quote, p.size_quote) / p.size_quote
        fill = s.exchange.execute(
            p.symbol,
            "buy" if p.direction == 1 else "sell",
            min(decision.size_quote, p.size_quote),
            book,
            now,
            quantity=s.lots[exit_lot].quantity * fraction if exit_lot else None,
        )
        if fill is None:
            s.counters["unfilled"] += 1
            return None
        s.order_times.append(now)
        s.last_trade[p.symbol] = now
        s.counters[f"fill:{fill.side}"] += 1
        row = dict(
            ts=now,
            symbol=p.symbol,
            side=fill.side,
            reason=reason,
            quantity=fill.quantity,
            price=fill.price,
            mid=fill.mid,
            slippage_bps=fill.slippage_bps,
            quote=fill.quote,
            fee=fill.fee,
            requested_quote=fill.requested_quote,
            partial=fill.partial,
            lot_id="",
            realized_pnl=0.0,
            holding_s=0,
        )
        if fill.side == "buy":
            lot = _Lot(
                id=f"{p.symbol}:{now.isoformat()}:{len(s.lots)}",
                symbol=p.symbol,
                quantity=fill.quantity,
                initial_quantity=fill.quantity,
                entry=fill.price,
                ts=now,
                cost_rate=2 * self.fee_rate + abs(fill.price / fill.mid - 1),
                fee=fill.fee,
            )
            s.lots[lot.id] = lot
            row["lot_id"] = lot.id
        else:
            remaining, pnl, touched = fill.quantity, -fill.fee, []
            keys = [k for k, lot in s.lots.items() if lot.symbol == p.symbol and lot.quantity > 0]
            if exit_lot in keys:
                keys.remove(exit_lot)
                keys.insert(0, exit_lot)
            for key in keys:
                lot = s.lots[key]
                reduction = min(lot.quantity, remaining)
                if reduction <= 0:
                    continue
                lot.quantity -= reduction
                remaining -= reduction
                pnl += reduction * (fill.price - lot.entry)
                pnl -= lot.fee * reduction / lot.initial_quantity
                touched.append(key)
                if remaining <= 0:
                    break
            row.update(
                lot_id=";".join(touched),
                realized_pnl=pnl,
                holding_s=int((now - s.lots[touched[0]].ts).total_seconds()) if touched else 0,
            )
        s.trade_rows.append(row)
        return fill

    def _decide(self, s: _Session, proposal, book, now, reason_prefix):
        decision = decide(proposal, self._portfolio(s, proposal, now, book), self.limits)
        s.counters[f"{reason_prefix}:{decision.verdict}:{decision.reason}"] += 1
        return decision

    async def _exits(self, s: _Session, symbol, now, book):
        for lid, lot in list(s.lots.items()):
            if lot.symbol != symbol or lot.quantity <= 0:
                continue
            bid = SimExchange.top(book)[0][0][0]
            if lot.quantity * bid < s.exchange.min_notional:
                s.counters["exit:dust_below_min_notional"] += 1
                continue
            reason = exit_reason(lot.entry, lot.cost_rate, bid, (now - lot.ts).total_seconds())
            if reason is None:
                continue
            proposal = Proposal(
                symbol=symbol,
                ts=now,
                direction=-1,
                score=-1,
                size_quote=lot.quantity * bid,
                signals=[],
            )
            decision = self._decide(s, proposal, book, now, "exit")
            if decision.verdict != "REJECT":
                self._execute(s, decision, book, now, reason, exit_lot=lid)

    async def run(self, params: Params, start: datetime, end: datetime) -> Result:
        s = _Session(self.initial_cash, self.fee_rate)
        for now, due in self.dataset.decision_points(start, end):
            books = {}
            for symbol in due:
                data = self.dataset[symbol]
                book = data.book_at(now)
                fresh = book is not None and 0 <= (now - book.ts).total_seconds() <= BOOK_MAX_AGE_S
                books[symbol] = book if fresh and SimExchange.top(book) else None
                candle = data.candle_closing_at(now)
                s.marks[symbol] = (
                    SimExchange.top(books[symbol])[2] if books[symbol] else candle.close
                )
            equity = s.equity()
            day = now.astimezone(UTC).date()
            if day != s.day:
                s.day, s.baseline, s.breached = day, equity, False
            if (s.baseline - equity) / s.baseline >= self.limits.max_daily_drawdown_pct:
                s.breached = True
            for symbol in due:
                book = books[symbol]
                if book is None:
                    s.counters["no_fresh_book"] += 1
                else:
                    await self._exits(s, symbol, now, book)
                signals = await self.signals(symbol, now, params.base_size)
                s.counters["signals"] += len(signals)
                proposal = propose(
                    symbol, now, signals, params.weights, params.threshold, params.base_size
                )
                if proposal is None:
                    continue
                s.counters["proposals"] += 1
                if book is None:
                    s.counters["entry:REJECT:no_fresh_book"] += 1
                    continue
                decision = self._decide(s, proposal, book, now, "entry")
                if decision.verdict != "REJECT":
                    self._execute(s, decision, book, now, "signal")
            s.equity_rows.append(
                dict(
                    ts=now,
                    cash=s.exchange.cash,
                    inventory_value=s.inventory_value(),
                    equity=s.equity(),
                    fees_paid=s.exchange.fees_paid,
                )
            )
        return Result(
            params,
            start,
            end,
            s.equity_rows,
            s.trade_rows,
            compute_metrics(s.equity_rows, s.trade_rows, self.initial_cash),
            s.counters,
        )

    async def tune(self, grid: list[Params], start, end) -> tuple[Params, dict]:
        """Pick the grid cell with the best net return on [start, end); ties keep grid order."""
        best, best_metrics = None, None
        for params in grid:
            metrics = (await self.run(params, start, end)).metrics
            if best is None or metrics["net_return_pct"] > best_metrics["net_return_pct"]:
                best, best_metrics = params, metrics
        return best, best_metrics

    async def walk_forward(
        self, start, end, train: timedelta, test: timedelta, step: timedelta, grid=None
    ) -> Result:
        """Tune on each train window only, then evaluate out-of-sample on the following test
        window. Test results are compounded into one out-of-sample curve."""
        if min(train, test, step) <= timedelta(0):
            raise ValueError("train, test and step must be positive")
        grid = grid or default_grid()
        windows, equity, trades, counters = [], [], [], Counter()
        factor, cursor = 1.0, start
        while cursor + train + test <= end:
            train_range = (cursor, cursor + train)
            test_range = (cursor + train, cursor + train + test)
            params, train_metrics = await self.tune(grid, *train_range)
            result = await self.run(params, *test_range)
            windows.append(
                {
                    "train_range": [t.isoformat() for t in train_range],
                    "test_range": [t.isoformat() for t in test_range],
                    "params": params.model_dump(),
                    "train_net_return_pct": train_metrics["net_return_pct"],
                    "test_metrics": {
                        k: v for k, v in result.metrics.items() if k != "strategy_dead"
                    },
                }
            )
            scale = factor * self.initial_cash / result.metrics["initial_equity"]
            equity += [
                r | {k: r[k] * scale for k in ("cash", "inventory_value", "equity", "fees_paid")}
                for r in result.equity
            ]
            trades += [
                t | {k: t[k] * scale for k in ("quantity", "quote", "fee", "realized_pnl")}
                for t in result.trades
            ]
            counters.update(result.counters)
            factor *= result.metrics["final_equity"] / result.metrics["initial_equity"]
            cursor += step
        if not windows:
            raise ValueError(
                f"walk-forward needs at least {train + test} of data; have {end - start}"
            )
        last = Params(**windows[-1]["params"])
        return Result(
            last,
            start,
            end,
            equity,
            trades,
            compute_metrics(equity, trades, self.initial_cash),
            counters,
            windows,
        )
