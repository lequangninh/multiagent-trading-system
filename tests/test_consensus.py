from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.consensus.engine import Consensus, propose
from swarm.models import Signal

NOW = datetime(2026, 1, 1, tzinfo=UTC)
WEIGHTS = dict(fairvalue=1, momentum=1, sentiment=1, scanner=1, liquidity=1)


def signals(direction=1, confidence=1):
    return [
        Signal(
            agent=a,
            symbol="BTC/USDT",
            ts=NOW,
            direction=0 if a in {"scanner", "liquidity"} else direction,
            confidence=confidence,
            rationale="fixture",
            ttl_s=300,
        )
        for a in WEIGHTS
    ]


@pytest.mark.parametrize("direction", [-1, 1])
def test_full_agreement(direction):
    p = propose("BTC/USDT", NOW, signals(direction), WEIGHTS)
    assert p.direction == direction and p.score == direction and p.size_quote == 100


def test_filters_multiply_without_voting():
    rows = signals()
    rows[-1].confidence = 0.8
    p = propose("BTC/USDT", NOW, rows, WEIGHTS)
    assert p.score == pytest.approx(0.8) and p.size_quote == pytest.approx(80)
    rows[-2].confidence = 0.5
    assert propose("BTC/USDT", NOW, rows, WEIGHTS) is None


def test_stale_missing_future_and_duplicate():
    rows = signals()
    assert propose("BTC/USDT", NOW + timedelta(seconds=300), rows, WEIGHTS) is None
    assert propose("BTC/USDT", NOW - timedelta(seconds=1), rows, WEIGHTS) is None
    assert propose("BTC/USDT", NOW, rows[:-1], WEIGHTS) is None
    assert propose("BTC/USDT", NOW, rows + rows, WEIGHTS).score == 1
    assert propose("BTC/USDT", NOW, rows[1:], WEIGHTS) is None


def test_opposition_and_threshold_boundary():
    rows = signals()
    rows[0].direction = -1
    assert propose("BTC/USDT", NOW, rows, WEIGHTS) is None
    rows = signals()
    rows[-1].confidence = 0.7
    assert propose("BTC/USDT", NOW, rows, WEIGHTS).score == 0.7


async def test_votes_persist_below_threshold():
    conn = MagicMock()
    conn.executemany = AsyncMock()
    conn.transaction.return_value = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    rows = signals()
    rows[0].direction = -1
    assert await Consensus(pool, WEIGHTS).evaluate("BTC/USDT", NOW, rows) is None
    records = conn.executemany.call_args.args[1]
    assert len(records) == 5
    assert all(row[-1] == pytest.approx(1 / 3) for row in records)


@pytest.mark.parametrize("weights", [{"momentum": -1}, {"momentum": float("nan")}])
def test_invalid_weights(weights):
    with pytest.raises(ValueError):
        propose("BTC/USDT", NOW, signals(), weights)
