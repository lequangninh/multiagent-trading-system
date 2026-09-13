from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from swarm.models import BookSnapshot, Candle, Fill, Order, Position, Proposal, RiskDecision, Signal

TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def signal(**overrides):
    return Signal(
        **(
            dict(
                agent="momentum",
                symbol="BTC/USDT",
                ts=TS,
                direction=1,
                confidence=0.8,
                rationale="fixture",
                ttl_s=60,
            )
            | overrides
        )
    )


def test_roundtrips():
    s = signal()
    p = Proposal(symbol=s.symbol, ts=TS, direction=1, score=0.8, size_quote=100, signals=[s])
    models = [
        s,
        p,
        RiskDecision(proposal=p, verdict="ALLOW", size_quote=100, reason="ok"),
        Candle(
            symbol=s.symbol, ts=TS, open=10, high=12, low=9, close=11, volume=100, timeframe="1m"
        ),
        BookSnapshot(symbol=s.symbol, ts=TS, bids=[(10, 2)], asks=[(11, 3)]),
        Order(
            client_order_id="o",
            symbol=s.symbol,
            ts=TS,
            side="buy",
            order_type="limit",
            quantity=1,
            price=10,
            status="open",
        ),
        Fill(
            fill_id="f",
            client_order_id="o",
            symbol=s.symbol,
            ts=TS,
            side="buy",
            quantity=1,
            price=10,
            fee=0.01,
            fee_currency="USDT",
        ),
        Position(symbol=s.symbol, ts=TS, quantity=1, average_entry_price=10),
    ]
    for model in models:
        assert type(model).model_validate_json(model.model_dump_json()) == model


@pytest.mark.parametrize(
    "overrides",
    [
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"confidence": float("nan")},
        {"direction": 2},
        {"ttl_s": 0},
        {"ts": datetime(2026, 1, 1)},
        {"unexpected": 1},
    ],
)
def test_invalid_signal(overrides):
    with pytest.raises(ValidationError):
        signal(**overrides)


@pytest.mark.parametrize("close", [0, 20, float("inf")])
def test_invalid_candle(close):
    with pytest.raises(ValidationError):
        Candle(
            symbol="BTC/USDT", ts=TS, open=10, high=12, low=9, close=close, volume=1, timeframe="1m"
        )


def test_empty_book():
    assert BookSnapshot(symbol="BTC/USDT", ts=TS, bids=[], asks=[]).bids == []
