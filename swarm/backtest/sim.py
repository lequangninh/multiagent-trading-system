"""Simulated exchange: taker fills walked through a real book snapshot.

Slippage is not a formula but the consequence of consuming displayed depth: at most
``level_fraction`` of each level is executable, so any order larger than half a level
walks deeper (worse average price) and, once the snapshot is exhausted, fills partially.
Fees are charged in quote at ``fee_rate`` on the gross traded value.
"""

from dataclasses import dataclass
from datetime import datetime

from swarm.models import BookSnapshot


@dataclass(frozen=True)
class SimFill:
    ts: datetime
    symbol: str
    side: str
    quantity: float
    price: float  # Average fill price.
    quote: float  # Gross traded value before fee.
    fee: float
    mid: float
    slippage_bps: float  # Average price versus mid, including half the spread.
    requested_quote: float
    partial: bool


class SimExchange:
    def __init__(self, cash: float, *, fee_rate=0.001, level_fraction=0.5, min_notional=5.0):
        if cash <= 0 or not 0 <= fee_rate < 1 or not 0 < level_fraction <= 1:
            raise ValueError("invalid simulated exchange settings")
        self.cash, self.fee_rate = float(cash), fee_rate
        self.level_fraction, self.min_notional = level_fraction, min_notional
        self.inventory: dict[str, float] = {}
        self.fees_paid = 0.0

    @staticmethod
    def top(book: BookSnapshot):
        bids = sorted((b for b in book.bids if b[1] > 0), reverse=True)
        asks = sorted(a for a in book.asks if a[1] > 0)
        if not bids or not asks or bids[0][0] >= asks[0][0]:
            return None
        return bids, asks, (bids[0][0] + asks[0][0]) / 2

    def execute(
        self, symbol, side, size_quote, book: BookSnapshot, ts, *, quantity=None
    ) -> SimFill | None:
        """Buy up to ``size_quote`` of quote; sell ``size_quote``/mid of base, or exactly
        ``quantity`` base units when given (managed exits close whole lots without dust)."""
        if side not in {"buy", "sell"} or size_quote <= 0 or book.symbol != symbol:
            return None
        top = self.top(book)
        if top is None:
            return None
        bids, asks, mid = top
        filled = quote = 0.0
        if side == "buy":
            budget = min(size_quote, self.cash / (1 + self.fee_rate))
            remaining = budget
            for price, depth in asks:
                take = min(depth * self.level_fraction, remaining / price)
                if take <= 0:
                    break
                filled += take
                quote += take * price
                remaining -= take * price
                if remaining <= budget * 1e-9:
                    break
            partial = quote < size_quote * (1 - 1e-9)
        else:
            wanted = size_quote / mid if quantity is None else quantity
            target = min(wanted, self.inventory.get(symbol, 0.0))
            remaining = target
            for price, depth in bids:
                take = min(depth * self.level_fraction, remaining)
                if take <= 0:
                    break
                filled += take
                quote += take * price
                remaining -= take
                if remaining <= target * 1e-9:
                    break
            partial = remaining > target * 1e-9
        if filled <= 0 or quote < self.min_notional:
            return None
        fee = quote * self.fee_rate
        price = quote / filled
        if side == "buy":
            self.cash -= quote + fee
            self.inventory[symbol] = self.inventory.get(symbol, 0.0) + filled
        else:
            self.cash += quote - fee
            self.inventory[symbol] = max(0.0, self.inventory.get(symbol, 0.0) - filled)
        self.fees_paid += fee
        return SimFill(
            ts=ts,
            symbol=symbol,
            side=side,
            quantity=filled,
            price=price,
            quote=quote,
            fee=fee,
            mid=mid,
            slippage_bps=(price - mid) / mid * 10000 * (1 if side == "buy" else -1),
            requested_quote=size_quote,
            partial=partial,
        )
