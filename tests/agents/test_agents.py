from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from swarm.agents.base import MarketState
from swarm.agents.fairvalue import FairValue
from swarm.agents.liquidity import Liquidity
from swarm.agents.momentum import Momentum, adx, ema
from swarm.agents.scanner import Scanner
from swarm.models import BookSnapshot, Candle

TS = datetime(2026, 1, 10, tzinfo=UTC)
SYMBOL = "BTC/USDT"


def state(prices, tf="5m", volumes=None):
    step = timedelta(minutes=int(tf[:-1]))
    rows = [
        Candle(
            symbol=SYMBOL,
            ts=TS - step * (len(prices) - i),
            open=p,
            high=p,
            low=p,
            close=p,
            volume=volumes[i] if volumes else 10,
            timeframe=tf,
        )
        for i, p in enumerate(prices)
    ]
    return MarketState(ts=TS, candles={tf: rows})


@pytest.mark.parametrize("agent", [Scanner(), FairValue(), Liquidity(), Momentum()])
async def test_empty_state(agent):
    assert await agent.analyze(SYMBOL, MarketState(ts=TS)) is None


async def test_scanner_volume_anomaly_and_repeatability():
    s = state([100] * 289, volumes=[10] * 288 + [30])
    agent = Scanner()
    result = await agent.analyze(SYMBOL, s)
    assert result.direction == 0 and result.confidence == 0.75
    assert result == await agent.analyze(SYMBOL, s)


async def test_scanner_flat():
    assert await Scanner().analyze(SYMBOL, state([100] * 289)) is None


async def test_scanner_volatility_anomaly():
    result = await Scanner().analyze(SYMBOL, state([100] * 288 + [110]))
    assert result.direction == 0 and result.confidence == 1


@pytest.mark.parametrize("last,direction", [(110, -1), (90, 1)])
async def test_fairvalue_mean_reversion(last, direction):
    result = await FairValue().analyze(SYMBOL, state([100] * 95 + [last], "15m"))
    assert result.direction == direction and result.confidence == 1


async def test_fairvalue_flat_and_zero_volume():
    result = await FairValue().analyze(SYMBOL, state([100] * 96, "15m"))
    assert result.direction == 0 and result.confidence == 0
    assert await FairValue().analyze(SYMBOL, state([100] * 96, "15m", [0] * 96)) is None


def book_state(bids, asks, size=100):
    return MarketState(
        ts=TS, size_quote=size, book=BookSnapshot(symbol=SYMBOL, ts=TS, bids=bids, asks=asks)
    )


async def test_liquidity_known_cost():
    result = await Liquidity().analyze(SYMBOL, book_state([(99.99, 10)], [(100.01, 10)]))
    assert result.direction == 0
    assert result.confidence == pytest.approx(0.95)


async def test_liquidity_empty_and_insufficient():
    for s in [book_state([], []), book_state([(99.99, 0.1)], [(100.01, 0.1)])]:
        assert (await Liquidity().analyze(SYMBOL, s)).confidence == 0


async def test_liquidity_walks_levels_and_handles_unsorted_book():
    s = book_state([(99.97, 1), (99.99, 0.5)], [(100.03, 1), (100.01, 0.5)])
    assert (await Liquidity().analyze(SYMBOL, s)).confidence == pytest.approx(0.9)
    s.book.ts -= timedelta(seconds=61)
    assert await Liquidity().analyze(SYMBOL, s) is None


async def test_momentum_flat_and_no_crossover():
    for prices in [[100] * 100, list(range(100, 200))]:
        assert await Momentum().analyze(SYMBOL, state(prices)) is None


@pytest.mark.parametrize("sign", [1, -1])
async def test_momentum_crossover(sign):
    # Established trend then reversal; select the first actual crossover event.
    prices = [200 - sign * i for i in range(70)] + [
        200 - sign * 69 + sign * i * 3 for i in range(1, 50)
    ]
    delta = ema(prices, 12) - ema(prices, 48)
    index = next(i for i in range(71, len(prices)) if delta[i] * sign > 0 >= delta[i - 1] * sign)
    s = state(prices[: index + 1])
    result = await Momentum().analyze(SYMBOL, s)
    assert result is not None and result.direction == sign
    assert 0 < result.confidence <= 1
    assert result == await Momentum().analyze(SYMBOL, s)


def test_adx_reference_monotonic_and_flat():
    assert adx(state(list(range(100, 200))).candles["5m"]) == pytest.approx(100)
    assert adx(state([100] * 100).candles["5m"]) == 0


def test_ema_reference():
    assert np.allclose(ema([1, 2, 3], 3), [1, 1.5, 2.25])


async def test_incomplete_and_gapped_bars_excluded():
    s = state([100] * 95 + [110], "15m")
    s.ts -= timedelta(minutes=1)
    assert await FairValue().analyze(SYMBOL, s) is None
    s = state([100] * 97, "15m")
    del s.candles["15m"][50]
    assert await FairValue().analyze(SYMBOL, s) is None
