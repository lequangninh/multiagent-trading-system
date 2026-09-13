"""NORO: close versus 96-bar typical-price VWAP, scaled by close dispersion."""

import numpy as np

from swarm.agents.base import MarketState, signal


class FairValue:
    name = "fairvalue"

    async def analyze(self, symbol: str, state: MarketState):
        bars = state.bars(symbol, "15m")
        if len(bars) < 96:
            return None
        bars = bars[-96:]
        volume = np.array([c.volume for c in bars])
        if volume.sum() == 0:
            return None
        vwap = float(np.average([(c.high + c.low + c.close) / 3 for c in bars], weights=volume))
        std = float(np.std([c.close for c in bars], ddof=0))
        z = (bars[-1].close - vwap) / std if std > 0 else 0
        return signal(
            self.name,
            symbol,
            state,
            -1 if z > 0 else 1 if z < 0 else 0,
            min(abs(z) / 3, 1),
            f"vwap={vwap:.8g}; z={z:.6g}",
        )
