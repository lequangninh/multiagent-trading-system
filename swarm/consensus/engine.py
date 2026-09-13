"""Deterministic consensus; persistence is explicitly outside scoring."""

from datetime import datetime

from swarm.models import Proposal, Signal

DIRECTIONAL = {"fairvalue", "momentum", "sentiment"}
FILTERS = {"scanner", "liquidity"}


def _evaluate(
    symbol: str,
    now: datetime,
    signals: list[Signal],
    weights: dict[str, float],
    threshold: float = 0.70,
    base_size: float = 100,
) -> Proposal | None:
    import math

    if not 0 < threshold <= 1 or not math.isfinite(base_size) or base_size <= 0:
        raise ValueError("invalid consensus settings")
    if any(not math.isfinite(w) or w < 0 for w in weights.values()):
        raise ValueError("weights must be finite and nonnegative")
    fresh = {}
    for s in signals:
        if s.symbol != symbol or s.agent not in DIRECTIONAL | FILTERS:
            continue
        if not 0 <= (now - s.ts).total_seconds() < s.ttl_s:
            continue
        if s.agent not in fresh or s.ts > fresh[s.agent].ts:
            fresh[s.agent] = s
    # Both quality filters must be present; absence never means approval.
    if not FILTERS <= fresh.keys():
        return None, 0.0
    denominator = sum(weights.get(a, 0) for a in DIRECTIONAL)
    if denominator <= 0:
        return None, 0.0
    score = (
        sum(
            weights.get(a, 0) * s.confidence * s.direction
            for a, s in fresh.items()
            if a in DIRECTIONAL
        )
        / denominator
    )
    score *= fresh["scanner"].confidence * fresh["liquidity"].confidence
    if abs(score) < threshold:
        return None, score
    return Proposal(
        symbol=symbol,
        ts=now,
        direction=1 if score > 0 else -1,
        score=score,
        size_quote=base_size * abs(score),
        signals=[fresh[a] for a in sorted(fresh)],
    ), score


def propose(symbol, now, signals, weights, threshold=0.70, base_size=100):
    return _evaluate(symbol, now, signals, weights, threshold, base_size)[0]


class Consensus:
    def __init__(self, pool, weights, threshold=0.70, base_size=100):
        self.pool, self.weights = pool, weights
        self.threshold, self.base_size = threshold, base_size

    async def evaluate(self, symbol, now, signals):
        proposal, score = _evaluate(
            symbol, now, signals, self.weights, self.threshold, self.base_size
        )
        # Persist all submitted votes, including rejected/stale inputs, for audit.
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.executemany(
                    """INSERT INTO votes (agent,symbol,ts,signal,resulting_score)
                    VALUES ($1,$2,$3,$4::jsonb,$5)""",
                    [(s.agent, s.symbol, now, s.model_dump_json(), score) for s in signals],
                )
        return proposal
