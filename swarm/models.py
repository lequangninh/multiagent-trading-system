"""Shared, validated message contracts. Timestamps must include a timezone."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Direction = Literal[-1, 0, 1]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Candle(Model):
    symbol: str
    ts: AwareDatetime
    open: Positive
    high: Positive
    low: Positive
    close: Positive
    volume: NonNegative
    timeframe: str

    @model_validator(mode="after")
    def valid_range(self):
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("invalid OHLC range")
        return self


class BookSnapshot(Model):
    symbol: str
    ts: AwareDatetime
    bids: list[tuple[Positive, NonNegative]]
    asks: list[tuple[Positive, NonNegative]]


class Signal(Model):
    agent: str
    symbol: str
    ts: AwareDatetime
    direction: Direction
    confidence: float = Field(ge=0, le=1)
    rationale: str
    ttl_s: int = Field(gt=0)


class Proposal(Model):
    symbol: str
    ts: AwareDatetime
    direction: Direction
    score: float = Field(ge=-1, le=1)
    size_quote: NonNegative
    signals: list[Signal]


class RiskDecision(Model):
    proposal: Proposal
    verdict: Literal["ALLOW", "RESIZE", "REJECT"]
    size_quote: NonNegative
    reason: str


class Order(Model):
    client_order_id: str
    exchange_order_id: str | None = None
    symbol: str
    ts: AwareDatetime
    side: Literal["buy", "sell"]
    order_type: Literal["limit", "market"]
    quantity: Positive
    price: Positive | None = None
    status: Literal["pending", "open", "partially_filled", "filled", "canceled", "rejected"]


class Fill(Model):
    fill_id: str
    client_order_id: str
    symbol: str
    ts: AwareDatetime
    side: Literal["buy", "sell"]
    quantity: Positive
    price: Positive
    fee: NonNegative
    fee_currency: str


class Position(Model):
    symbol: str
    ts: AwareDatetime
    quantity: NonNegative
    average_entry_price: NonNegative
    realized_pnl: float = 0
