"""Pure analyzer contracts; caller supplies the observation time."""

from typing import Protocol

from pydantic import AwareDatetime, Field

from swarm.models import BookSnapshot, Candle, Model, Positive, Signal


class MarketState(Model):
    ts: AwareDatetime
    candles: dict[str, list[Candle]] = Field(default_factory=dict)
    book: BookSnapshot | None = None
    news: list[dict] = Field(default_factory=list)
    size_quote: Positive = 100

    def bars(self, symbol: str, timeframe: str) -> list[Candle]:
        minutes = int(timeframe[:-1])
        # Only closed candles may influence a decision. Reject gaps/duplicates.
        rows = sorted(
            (
                c
                for c in self.candles.get(timeframe, [])
                if c.symbol == symbol
                and c.timeframe == timeframe
                and (self.ts - c.ts).total_seconds() >= minutes * 60
            ),
            key=lambda c: c.ts,
        )
        if any((b.ts - a.ts).total_seconds() != minutes * 60 for a, b in zip(rows, rows[1:])):
            return []
        return rows


class Agent(Protocol):
    name: str

    async def analyze(self, symbol: str, state: MarketState) -> Signal | None: ...


def signal(agent, symbol, state, direction, confidence, rationale):
    return Signal(
        agent=agent,
        symbol=symbol,
        ts=state.ts,
        direction=direction,
        confidence=float(max(0, min(1, confidence))),
        rationale=rationale,
        ttl_s=300,
    )
