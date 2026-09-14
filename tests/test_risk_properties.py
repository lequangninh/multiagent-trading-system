"""Property tests: no sequence of proposals can push exposure over the configured limits."""

from datetime import UTC, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from swarm.models import Proposal
from swarm.risk.gate import Limits, PortfolioState, decide

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

proposals = st.lists(
    st.tuples(
        st.sampled_from(SYMBOLS),
        st.sampled_from([-1, 1]),
        st.floats(min_value=0.01, max_value=1e7, allow_nan=False, allow_infinity=False),
    ),
    max_size=60,
)
limits = st.builds(
    Limits,
    max_position_pct_equity=st.floats(min_value=0.01, max_value=1.0),
    max_gross_exposure_pct=st.floats(min_value=0.01, max_value=1.0),
    max_slippage_bps=st.floats(min_value=1, max_value=100),
)


def reserve(positions, symbol, direction, size):
    """What the OMS does with an approved decision: reserve buys, release sells."""
    current = positions.get(symbol, 0.0)
    positions[symbol] = current + size if direction == 1 else max(0.0, current - size)


@settings(max_examples=300, deadline=None)
@given(
    sequence=proposals,
    limits=limits,
    equity=st.floats(min_value=100, max_value=1e7, allow_nan=False, allow_infinity=False),
)
def test_exposure_never_exceeds_limits(sequence, limits, equity):
    positions: dict[str, float] = {}
    tolerance = equity * 1e-9
    for i, (symbol, direction, size) in enumerate(sequence):
        proposal = Proposal(
            symbol=symbol, ts=NOW, direction=direction, score=direction, size_quote=size, signals=[]
        )
        # Worst case for the exposure rules: no cooldown, no rate limit, unlimited depth.
        state = PortfolioState(
            ts=NOW,
            equity=equity,
            day_start_equity=equity,
            day_start_ts=NOW,
            positions=dict(positions),
            slippage_curve=[(1e12, 0.0)],
        )
        result = decide(proposal, state, limits)
        assert 0 <= result.size_quote <= proposal.size_quote
        if result.verdict == "REJECT":
            assert result.size_quote == 0
            continue
        if result.verdict == "ALLOW":
            assert result.size_quote == proposal.size_quote
        reserve(positions, symbol, direction, result.size_quote)
        assert sum(positions.values()) <= equity * limits.max_gross_exposure_pct + tolerance
        for held in positions.values():
            assert held <= equity * limits.max_position_pct_equity + tolerance
        # Spot: a sell can never create a short.
        assert all(v >= 0 for v in positions.values())


@settings(max_examples=200, deadline=None)
@given(
    size=st.floats(min_value=0.01, max_value=1e7, allow_nan=False),
    curve=st.lists(
        st.tuples(st.floats(min_value=1, max_value=1e6), st.floats(min_value=0, max_value=200)),
        min_size=1,
        max_size=10,
    ),
    cap=st.floats(min_value=1, max_value=100),
)
def test_slippage_cap_never_allows_size_beyond_safe_depth(size, curve, cap):
    # Shape the random levels like the OMS curve builder does: strictly increasing
    # cumulative depth with a nondecreasing cost ceiling.
    by_depth: dict[float, float] = {}
    for q, b in curve:
        by_depth[round(q, 6)] = max(by_depth.get(round(q, 6), 0.0), b)
    ceiling, curve = 0.0, []
    for q in sorted(by_depth):
        ceiling = max(ceiling, by_depth[q])
        curve.append((q, ceiling))
    state = PortfolioState(
        ts=NOW, equity=1e9, day_start_equity=1e9, day_start_ts=NOW, slippage_curve=curve
    )
    proposal = Proposal(
        symbol="BTC/USDT", ts=NOW, direction=1, score=1, size_quote=size, signals=[]
    )
    result = decide(proposal, state, Limits(max_slippage_bps=cap))
    safe = max((q for q, b in curve if b <= cap), default=0)
    if safe <= 0:
        assert result.verdict == "REJECT" and result.reason == "max_slippage_bps"
    else:
        assert result.size_quote <= min(size, safe) + 1e-9
