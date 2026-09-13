"""EMA crossover events with Wilder ADX(14); no continuing trend votes."""

import numpy as np

from swarm.agents.base import MarketState, signal


def ema(values, period):
    result = [float(values[0])]
    alpha = 2 / (period + 1)
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return np.array(result)


def adx(bars, period=14):
    tr, plus, minus = [], [], []
    for a, b in zip(bars, bars[1:]):
        up, down = b.high - a.high, a.low - b.low
        tr.append(max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close)))
        plus.append(up if up > down and up > 0 else 0)
        minus.append(down if down > up and down > 0 else 0)
    totals = [sum(x[:period]) for x in (tr, plus, minus)]
    dx = []
    for i in range(period - 1, len(tr)):
        if i >= period:
            totals = [s - s / period + x[i] for s, x in zip(totals, (tr, plus, minus))]
        _, p, m = totals
        dx.append(100 * abs(p - m) / (p + m) if p + m else 0)
    if len(dx) < period:
        return 0
    value = sum(dx[:period]) / period
    for x in dx[period:]:
        value = (value * (period - 1) + x) / period
    return value


class Momentum:
    name = "momentum"

    async def analyze(self, symbol: str, state: MarketState):
        bars = state.bars(symbol, "5m")
        if len(bars) < 49:
            return None
        close = [c.close for c in bars]
        delta = ema(close, 12) - ema(close, 48)
        direction = 1 if delta[-2] <= 0 < delta[-1] else -1 if delta[-2] >= 0 > delta[-1] else 0
        strength = adx(bars)
        if not direction or strength < 25:
            return None
        return signal(
            self.name,
            symbol,
            state,
            direction,
            min(strength / 50, 1),
            f"EMA12/48 crossover; ADX14={strength:.6g}",
        )
