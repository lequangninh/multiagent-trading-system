"""Chaos: each fault must degrade to "no new orders" and recover with no manual step.

Scenarios from the build spec: Redis killed mid-cycle, exchange 5xx for two minutes,
a book snapshot with a 500 bps spread, and a candle with close = 0.
"""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import ccxt
import pytest

from swarm.agents.base import MarketState
from swarm.agents.liquidity import Liquidity
from swarm.bus import DECISIONS, SIGNALS
from swarm.consensus.engine import propose
from swarm.data.feed import MarketFeed
from swarm.execution.oms import OMS
from swarm.execution.paper import consume_decisions, publish
from swarm.models import BookSnapshot, Proposal, RiskDecision, Signal
from swarm.risk.gate import Limits
from swarm.telemetry import NullTelemetry
from tests.test_execution import Exchange, Repo

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
SYMBOL = "BTC/USDT"


def decision(size=100, ts=NOW):
    p = Proposal(symbol=SYMBOL, ts=ts, direction=1, score=1, size_quote=size, signals=[])
    return RiskDecision(proposal=p, verdict="ALLOW", size_quote=size, reason="fixture")


# --- 1. Redis killed mid-cycle ---------------------------------------------------------


class FlakyBus:
    """Delivers queued decisions; while ``down`` every subscribe/publish/iteration fails."""

    def __init__(self):
        self.queue = asyncio.Queue()
        self.down = False
        self.subscriptions = 0

    async def publish(self, channel, message):
        if self.down:
            raise ConnectionError("redis gone")
        await self.queue.put(message)

    @contextlib.asynccontextmanager
    async def subscribe(self, channel):
        if self.down:
            raise ConnectionError("redis gone")
        self.subscriptions += 1

        async def messages():
            while True:
                message = await self.queue.get()
                if self.down:
                    raise ConnectionError("redis died mid-stream")
                yield message

        yield messages()


class StubOMS:
    symbols = [SYMBOL]

    def __init__(self):
        self.submitted = []

    async def submit(self, decision):
        self.submitted.append(decision)


async def settle(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


async def test_redis_killed_mid_cycle_pauses_then_recovers():
    bus, oms = FlakyBus(), StubOMS()
    worker = asyncio.create_task(
        consume_decisions(bus, oms, NullTelemetry(), backoff_s=(0.01, 0.05))
    )
    await bus.publish(DECISIONS, decision(1))
    await settle(lambda: len(oms.submitted) == 1)
    bus.down = True
    await bus.queue.put(decision(2))  # In-flight message arrives as the connection dies.
    await settle(lambda: worker.done() or bus.queue.empty())
    assert not worker.done(), "consumer must survive the outage"
    # Degraded: the cycle cannot publish, so no decision can reach the OMS; nothing is forced.
    assert not await publish(bus, SIGNALS, fixture_signal(), NullTelemetry())
    assert len(oms.submitted) == 1
    bus.down = False
    await settle(lambda: bus.subscriptions >= 2)  # Reconnected without intervention.
    await bus.publish(DECISIONS, decision(4))
    await settle(lambda: len(oms.submitted) == 2)
    assert oms.submitted[-1].size_quote == 4
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


def fixture_signal():
    return Signal(
        agent="scanner", symbol=SYMBOL, ts=NOW, direction=0, confidence=1, rationale="", ttl_s=300
    )


# --- 2. Exchange returns 5xx for two minutes ----------------------------------------------


class OutageExchange(Exchange):
    def __init__(self, clock):
        super().__init__()
        self.clock, self.outage = clock, None  # (start, end)
        self.originals = {}
        for name in (
            "fetch_balance",
            "fetch_order_book",
            "fetch_order",
            "fetch_my_trades",
            "fetch_open_orders",
            "create_order",
            "cancel_order",
        ):
            self.originals[name] = getattr(self, name)
            setattr(self, name, self._guard(self.originals[name]))

    def _guard(self, method):
        async def wrapped(*args, **kwargs):
            if self.outage and self.outage[0] <= self.clock() < self.outage[1]:
                raise ccxt.ExchangeNotAvailable("503 Service Unavailable")
            return await method(*args, **kwargs)

        return wrapped


async def test_exchange_5xx_for_two_minutes_blocks_orders_then_recovers(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "swarm.execution.exchange.asyncio.sleep", AsyncMock()
    )  # skip read_retry waits
    clock = [NOW]
    exchange = OutageExchange(lambda: clock[0])
    oms = OMS(
        exchange,
        Repo(),
        [SYMBOL],
        Limits(kill_switch=str(tmp_path / "HALT")),
        clock=lambda: clock[0],
    )
    await oms.start()
    assert oms.ready
    exchange.outage = (NOW, NOW + timedelta(minutes=2))
    assert await oms.reconcile() == -1 and not oms.ready
    for _ in range(3):
        clock[0] += timedelta(seconds=30)
        assert await oms.submit(decision(ts=clock[0])) is None  # Fresh proposal, still refused.
        assert await oms.reconcile() == -1
    assert exchange.originals["create_order"].await_count == 0
    clock[0] = NOW + timedelta(minutes=2, seconds=1)
    assert await oms.reconcile() == 0 and oms.ready
    assert await oms.submit(decision(ts=clock[0])) is not None
    assert oms.state["orders"] and list(oms.state["orders"].values())[0]["status"] == "open"


# --- 3. Book snapshot with a 500 bps spread ---------------------------------------------


def wide_book():
    return BookSnapshot(symbol=SYMBOL, ts=NOW, bids=[(97.5, 100)], asks=[(102.5, 100)])


async def test_wide_spread_book_gives_zero_liquidity_confidence_and_no_proposal():
    state = MarketState(ts=NOW, book=wide_book())
    liquidity = await Liquidity().analyze(SYMBOL, state)
    assert liquidity.confidence == 0
    strong = [
        Signal(agent=a, symbol=SYMBOL, ts=NOW, direction=d, confidence=1, rationale="", ttl_s=300)
        for a, d in (("scanner", 0), ("fairvalue", 1), ("momentum", 1), ("sentiment", 1))
    ]
    weights = dict(scanner=1, liquidity=1, fairvalue=1, momentum=1, sentiment=1)
    assert propose(SYMBOL, NOW, strong + [liquidity], weights) is None
    healthy = liquidity.model_copy(update={"confidence": 1.0})
    assert (
        propose(SYMBOL, NOW, strong + [healthy], weights) is not None
    )  # Recovers with a sane book.


async def test_wide_spread_book_is_vetoed_by_the_gate_even_for_an_allowed_decision(tmp_path):
    exchange, repo = Exchange(), Repo()
    oms = OMS(
        exchange, repo, [SYMBOL], Limits(kill_switch=str(tmp_path / "HALT")), clock=lambda: NOW
    )
    await oms.start()
    exchange.fetch_order_book.return_value = {"bids": [[97.5, 100]], "asks": [[102.5, 100]]}
    assert await oms.submit(decision()) is None
    exchange.create_order.assert_not_awaited()
    exchange.fetch_order_book.return_value = {"bids": [[99.99, 100]], "asks": [[100.01, 100]]}
    assert await oms.submit(decision(50)) is not None
    exchange.create_order.assert_awaited_once()


# --- 4. Candle with close = 0 -------------------------------------------------------------


class ZeroCloseExchange:
    def __init__(self):
        self.calls = 0

    def set_sandbox_mode(self, flag):
        pass

    async def watch_ohlcv(self, symbol, timeframe):
        self.calls += 1
        base = int(NOW.timestamp() * 1000)
        if self.calls == 1:
            return [[base, 100, 101, 99, 0, 5], [base + 60_000, 100, 102, 99, 101, 5]]
        await asyncio.sleep(3600)  # Nothing more; the test cancels the task.


class RecordingStore:
    def __init__(self):
        self.candles = []

    async def upsert_candles(self, candles):
        self.candles += candles


async def test_zero_close_candle_is_dropped_without_reconnect():
    store, exchange = RecordingStore(), ZeroCloseExchange()
    feed = MarketFeed([SYMBOL], store, exchange=exchange)
    task = asyncio.create_task(feed._candles(SYMBOL))
    await settle(lambda: len(store.candles) == 1 or task.done())
    assert not task.done(), "a bad row must not end the stream"
    assert [c.close for c in store.candles] == [101]
    assert exchange.calls == 2  # Continued to the next batch; no reconnect path taken.
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    with pytest.raises(ValueError):
        BookSnapshot(
            symbol=SYMBOL, ts=NOW, bids=[(0, 1)], asks=[(1, 1)]
        )  # Zero prices never enter models.
