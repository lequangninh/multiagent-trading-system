"""TIDAL: nondirectional anomaly filter."""

import numpy as np

from swarm.agents.base import MarketState, signal


class Scanner:
    name = "scanner"

    async def analyze(self, symbol: str, state: MarketState):
        bars = state.bars(symbol, "5m")
        if len(bars) < 289:
            return None
        median = float(np.median([c.volume for c in bars[-289:-1]]))
        volume = bars[-1].volume
        ratio = volume / median if median > 0 else (float("inf") if volume > 0 else 0)
        # 15-minute realised volatility: RMS of three consecutive 5m log returns.
        returns = np.diff(np.log([c.close for c in bars[-289:]]))
        vols = np.sqrt(np.convolve(returns**2, np.ones(3) / 3, mode="valid"))
        cutoff = float(np.quantile(vols[:-1], 0.9))
        vol_hit = vols[-1] > cutoff and vols[-1] > 0
        if ratio <= 2 and not vol_hit:
            return None
        strength = min(ratio / 4, 1) if ratio > 2 else 0
        if vol_hit:
            strength = max(strength, min(float(vols[-1]) / (2 * cutoff), 1) if cutoff else 1)
        return signal(
            self.name,
            symbol,
            state,
            0,
            strength,
            f"volume_ratio={ratio:.4g}; rv15={vols[-1]:.6g}; q90={cutoff:.6g}",
        )
