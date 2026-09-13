"""ZEPHR: conservative two-sided execution cost relative to midprice."""

from swarm.agents.base import MarketState, signal


def cost(levels, quantity, mid, side):
    remaining, quote = quantity, 0.0
    for price, size in levels:
        take = min(remaining, size)
        quote += take * price
        remaining -= take
        if remaining <= 1e-12:
            return max(0, side * (quote / quantity - mid) / mid * 10000)
    return float("inf")


class Liquidity:
    name = "liquidity"

    async def analyze(self, symbol: str, state: MarketState):
        book = state.book
        if (
            book is None
            or book.symbol != symbol
            or not 0 <= (state.ts - book.ts).total_seconds() <= 60
        ):
            return None
        bids = sorted(book.bids, reverse=True)
        asks = sorted(book.asks)
        if not bids or not asks or bids[0][0] >= asks[0][0]:
            return signal(self.name, symbol, state, 0, 0, "empty or crossed book")
        mid = (bids[0][0] + asks[0][0]) / 2
        spread = (asks[0][0] - bids[0][0]) / mid * 10000
        bid_depth = sum(p * q for p, q in bids if p >= mid * 0.995)
        ask_depth = sum(p * q for p, q in asks if p <= mid * 1.005)
        quantity = state.size_quote / mid
        slippage = max(cost(bids, quantity, mid, -1), cost(asks, quantity, mid, 1))
        return signal(
            self.name,
            symbol,
            state,
            0,
            1 - min(slippage / 20, 1),
            f"spread_bps={spread:.6g}; bid_depth_quote={bid_depth:.8g}; "
            f"ask_depth_quote={ask_depth:.8g}; slippage_bps={slippage:.6g}",
        )
