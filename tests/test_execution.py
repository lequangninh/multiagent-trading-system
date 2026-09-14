import copy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import ccxt
import pytest

from swarm.execution.exchange import TestnetBinance as BinanceClient
from swarm.execution.exchange import create_exchange
from swarm.execution.oms import OMS, client_id, price_at_tick
from swarm.models import Proposal, RiskDecision
from swarm.risk.gate import Limits

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
SYMBOL = "BTC/USDT"


class Repo:
    def __init__(self):
        self.state = None
        self.fills = []

    async def load(self):
        return copy.deepcopy(self.state)

    async def save(self, state, fills=()):
        self.state = copy.deepcopy(state)
        self.fills.extend(fills)


class Exchange:
    def __init__(self):
        self.fetch_balance = AsyncMock(
            return_value={"total": {"USDT": 10000, "BTC": 0}, "free": {"USDT": 10000, "BTC": 0}}
        )
        self.fetch_order_book = AsyncMock(
            return_value={"bids": [[99.99, 100]], "asks": [[100.01, 100]]}
        )
        self.create_order = AsyncMock(return_value={"id": "1", "status": "open", "filled": 0})
        self.fetch_order = AsyncMock(return_value={"id": "1", "status": "open", "filled": 0})
        self.fetch_my_trades = AsyncMock(return_value=[])
        self.fetch_open_orders = AsyncMock(return_value=[])
        self.cancel_order = AsyncMock()

    def market(self, symbol):
        return {
            "base": "BTC",
            "precision": {"price": 0.01},
            "limits": {"cost": {"min": 5}, "amount": {"min": 0.001}},
        }

    def amount_to_precision(self, symbol, quantity):
        return str(int(quantity * 1000) / 1000)


def decision(size=100, ts=NOW, direction=1):
    p = Proposal(
        symbol=SYMBOL, ts=ts, direction=direction, score=direction, size_quote=size, signals=[]
    )
    return RiskDecision(proposal=p, verdict="ALLOW", size_quote=size, reason="fixture")


async def setup(tmp_path):
    exchange, repo = Exchange(), Repo()
    oms = OMS(
        exchange, repo, [SYMBOL], Limits(kill_switch=str(tmp_path / "HALT")), clock=lambda: NOW
    )
    await oms.start()
    return oms, exchange, repo


async def test_intent_persisted_before_submission_and_dedup(tmp_path):
    oms, exchange, repo = await setup(tmp_path)

    async def create(*args):
        assert repo.state["orders"][client_id(decision().proposal)]["status"] == "intent"
        return {"id": "1", "status": "open", "filled": 0}

    exchange.create_order.side_effect = create
    await oms.submit(decision())
    await oms.submit(decision())
    assert exchange.create_order.await_count == 1
    args = exchange.create_order.call_args.args
    assert args[1:3] == ("limit", "buy")
    assert args[3] * args[4] <= 100


async def test_unknown_submission_never_retried_and_reconciles(tmp_path):
    oms, exchange, repo = await setup(tmp_path)
    exchange.create_order.side_effect = ccxt.RequestTimeout("fixture")
    cid = await oms.submit(decision())
    assert not oms.ready and repo.state["orders"][cid]["status"] == "unknown"
    await oms.submit(decision())
    assert await oms.reconcile() == 0
    assert oms.ready and oms.state["orders"][cid]["status"] == "open"
    await oms.submit(decision())
    assert exchange.create_order.await_count == 1


async def test_unknown_not_found_remains_blocked(tmp_path):
    oms, exchange, repo = await setup(tmp_path)
    exchange.create_order.side_effect = ccxt.RequestTimeout("fixture")
    await oms.submit(decision())
    exchange.fetch_order.side_effect = ccxt.OrderNotFound("fixture")
    assert await oms.reconcile() == 1
    assert not oms.ready
    assert exchange.create_order.await_count == 1


async def test_halt_cannot_be_bypassed_by_allowed_message(tmp_path):
    oms, exchange, _ = await setup(tmp_path)
    (tmp_path / "HALT").touch()
    assert await oms.submit(decision()) is None
    exchange.create_order.assert_not_awaited()


async def test_drawdown_persists_after_restart_and_recovery(tmp_path):
    oms, exchange, repo = await setup(tmp_path)
    exchange.fetch_balance.return_value = {"total": {"USDT": 9600}, "free": {"USDT": 9600}}
    await oms.reconcile()
    assert repo.state["breached"]
    exchange.fetch_balance.return_value = {"total": {"USDT": 10000}, "free": {"USDT": 10000}}
    restarted = OMS(exchange, repo, [SYMBOL], oms.limits, clock=lambda: NOW)
    await restarted.start()
    assert await restarted.submit(decision()) is None
    exchange.create_order.assert_not_awaited()


async def test_pending_orders_reserve_position_capacity(tmp_path):
    oms, exchange, _ = await setup(tmp_path)
    await oms.submit(decision(900))
    oms.clock = lambda: NOW + timedelta(seconds=301)
    await oms.submit(decision(900, ts=oms.clock()))
    assert sum(o["amount"] * 100 for o in oms.state["orders"].values()) <= 1000


async def test_reconcile_fills_once_and_creates_exit_lot(tmp_path):
    oms, exchange, repo = await setup(tmp_path)
    await oms.submit(decision())
    exchange.fetch_order.return_value = {"id": "1", "status": "closed", "filled": 1}
    exchange.fetch_my_trades.return_value = [
        {
            "id": "f1",
            "timestamp": int(NOW.timestamp() * 1000),
            "amount": 1,
            "price": 100,
            "fee": {"cost": 0.1, "currency": "USDT"},
        }
    ]
    exchange.fetch_balance.return_value = {
        "total": {"USDT": 9900, "BTC": 1},
        "free": {"USDT": 9900, "BTC": 1},
    }
    await oms.reconcile()
    await oms.reconcile()
    assert len(repo.fills) == 1 and len(oms.state["lots"]) == 1
    assert oms.state["balances"][SYMBOL] == 1


@pytest.mark.parametrize("bid,age", [(101, 301), (99, 301), (100, 14401)])
async def test_take_profit_stop_and_time_exit_pass_through_gate(tmp_path, bid, age):
    oms, exchange, _ = await setup(tmp_path)
    oms.state["lots"]["fixture"] = {
        "symbol": SYMBOL,
        "quantity": 1,
        "entry": 100,
        "ts": NOW.isoformat(),
        "cost_rate": 0.002,
    }
    oms.clock = lambda: NOW + timedelta(seconds=age)
    exchange.fetch_balance.return_value = {
        "total": {"USDT": 9900, "BTC": 1},
        "free": {"USDT": 9900, "BTC": 1},
    }
    book = {"bids": [[bid, 100]], "asks": [[bid + 0.02, 100]]}
    exchange.fetch_order_book.return_value = book
    oms.books[SYMBOL] = book
    await oms.exits()
    assert exchange.create_order.call_args.args[2] == "sell"
    assert list(oms.state["orders"].values())[0]["exit_lot"] == "fixture"


async def test_unmanaged_exchange_order_blocks_new_orders(tmp_path):
    oms, exchange, _ = await setup(tmp_path)
    exchange.fetch_open_orders.return_value = [{"clientOrderId": "external"}]
    assert await oms.reconcile() == 1
    assert await oms.submit(decision()) is None


async def test_stale_proposal_and_rejected_decision(tmp_path):
    oms, exchange, _ = await setup(tmp_path)
    assert await oms.submit(decision(ts=NOW - timedelta(seconds=61))) is None
    d = decision().model_copy(update={"verdict": "REJECT", "size_quote": 0})
    assert await oms.submit(d) is None
    exchange.create_order.assert_not_awaited()


async def test_final_http_boundary_blocks_mainnet():
    exchange = BinanceClient()
    try:
        with pytest.raises(ValueError, match="Non-testnet"):
            await exchange.fetch("https://api.binance.com/api/v3/order")
    finally:
        await exchange.close()


def test_missing_credentials_fail_without_network(monkeypatch):
    monkeypatch.delenv("BINANCE_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET_API_SECRET", raising=False)
    with pytest.raises(ValueError, match="is not set"):
        create_exchange(
            {"exchange": {"sandbox": True, "urls": {"rest": "https://testnet.binance.vision/api"}}}
        )


def test_tick_prices_and_client_id_stability():
    book = {"bids": [[100, 1]], "asks": [[100.03, 1]]}
    assert price_at_tick(book, 0.01, "buy") == 100.01
    assert price_at_tick(book, 0.01, "sell") == 100.02
    assert client_id(decision().proposal) == client_id(decision().proposal)


async def test_configured_ccxt_accountwide_open_orders(monkeypatch):
    # Exercise real CCXT dispatch and warning logic, replacing only the HTTP call.
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "offline-fixture-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "offline-fixture-secret")
    exchange = create_exchange(
        {"exchange": {"sandbox": True, "urls": {"rest": "https://testnet.binance.vision/api"}}}
    )
    exchange.markets = {}
    exchange.privateGetOpenOrders = AsyncMock(return_value=[])
    try:
        assert await exchange.fetch_open_orders() == []
        exchange.privateGetOpenOrders.assert_awaited_once()
    finally:
        await exchange.close()


def test_safe_error_excludes_sensitive_request_text():
    from swarm.execution.exchange import safe_error

    exc = ccxt.ExchangeError(
        "https://example.com?signature=PRIVATE apiKey=SECRET "
        '{"code":-2015,"msg":"sensitive request"}'
    )
    result = safe_error(exc)
    assert result["exchange_code"] == -2015
    assert result["hint"] == "check_testnet_key_permissions_or_ip"
    assert "PRIVATE" not in str(result) and "SECRET" not in str(result)
