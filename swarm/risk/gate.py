"""Pure risk decisions. Caller snapshots HALT and portfolio before each decision.

ALLOW/RESIZE are permissions for this snapshot only; reserve approved exposure
before processing another proposal. Spot sells reduce existing inventory only.
"""

from pathlib import Path

from pydantic import AwareDatetime, Field

from swarm.models import Model, NonNegative, Positive, Proposal, RiskDecision


class Limits(Model):
    max_position_pct_equity: float = Field(default=0.10, gt=0, le=1)
    max_gross_exposure_pct: float = Field(default=0.50, gt=0, le=1)
    max_daily_drawdown_pct: float = Field(default=0.03, gt=0, le=1)
    max_trades_per_hour: int = Field(default=20, gt=0)
    max_slippage_bps: Positive = 15
    min_time_between_same_symbol_s: int = Field(default=300, ge=0)
    kill_switch: str = "./HALT"


class PortfolioState(Model):
    ts: AwareDatetime
    equity: Positive
    day_start_equity: Positive
    day_start_ts: AwareDatetime
    # Quote marked inventory including reserved buy exposure.
    positions: dict[str, NonNegative] = Field(default_factory=dict)
    trades: list[AwareDatetime] = Field(default_factory=list)
    last_trade_by_symbol: dict[str, AwareDatetime] = Field(default_factory=dict)
    halted: bool = False
    # Latched by portfolio controller after any drawdown breach, reset next UTC day.
    drawdown_breached_at: AwareDatetime | None = None
    # Cumulative executable quote depth paired with conservative cost in bps.
    slippage_curve: list[tuple[Positive, NonNegative]] = Field(default_factory=list)


def halt_present(limits: Limits) -> bool:
    return Path(limits.kill_switch).exists()


def decide(proposal: Proposal, portfolio_state: PortfolioState, limits: Limits) -> RiskDecision:
    from datetime import UTC

    p, state = proposal, portfolio_state

    def result(verdict, size, reason):
        return RiskDecision(proposal=p, verdict=verdict, size_quote=size, reason=reason)

    def reject(reason):
        return result("REJECT", 0, reason)

    if state.halted:
        return reject("kill_switch")
    today = state.ts.astimezone(UTC).date()
    if state.day_start_ts.astimezone(UTC).date() != today:
        return reject("daily_equity_baseline_stale")
    if state.drawdown_breached_at and state.drawdown_breached_at.astimezone(UTC).date() == today:
        return reject("max_daily_drawdown_pct")
    if (
        state.day_start_equity - state.equity
    ) / state.day_start_equity >= limits.max_daily_drawdown_pct:
        return reject("max_daily_drawdown_pct")
    if p.ts > state.ts or p.direction not in {-1, 1} or p.size_quote <= 0:
        return reject("invalid_proposal")
    if any(t > state.ts for t in state.trades):
        return reject("future_trade_timestamp")
    if (
        sum(0 <= (state.ts - t).total_seconds() < 3600 for t in state.trades)
        >= limits.max_trades_per_hour
    ):
        return reject("max_trades_per_hour")
    last = state.last_trade_by_symbol.get(p.symbol)
    if last and (state.ts - last).total_seconds() < limits.min_time_between_same_symbol_s:
        return reject("min_time_between_same_symbol_s")
    size, reasons = p.size_quote, []
    existing = state.positions.get(p.symbol, 0)
    if p.direction == 1:
        caps = [
            (state.equity * limits.max_position_pct_equity - existing, "max_position_pct_equity"),
            (
                state.equity * limits.max_gross_exposure_pct - sum(state.positions.values()),
                "max_gross_exposure_pct",
            ),
        ]
    else:
        caps = [(existing, "spot_inventory")]
    for cap, reason in caps:
        if cap <= 0:
            return reject(reason)
        if cap < size:
            size = cap
            reasons.append(reason)
    curve = state.slippage_curve
    if not curve or any(b[0] <= a[0] or b[1] < a[1] for a, b in zip(curve, curve[1:])):
        return reject("missing_or_invalid_slippage_curve")
    safe_size = max((q for q, bps in curve if bps <= limits.max_slippage_bps), default=0)
    if safe_size <= 0:
        return reject("max_slippage_bps")
    if size > safe_size:
        size = safe_size
        reasons.append("max_slippage_bps")
    return result(
        "RESIZE" if size < p.size_quote else "ALLOW",
        size,
        ";".join(reasons) if reasons else "within_limits",
    )


def check(proposal, portfolio_state, limits):
    """Filesystem boundary: use this entry point for each executable decision."""
    import structlog

    state = portfolio_state.model_copy(
        update={"halted": portfolio_state.halted or halt_present(limits)}
    )
    decision = decide(proposal, state, limits)
    structlog.get_logger().info(
        "risk_decision",
        symbol=proposal.symbol,
        verdict=decision.verdict,
        reason=decision.reason,
        size_quote=decision.size_quote,
    )
    return decision
