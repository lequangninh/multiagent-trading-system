from datetime import UTC, datetime, timedelta

import pytest

from swarm.models import Proposal
from swarm.risk.gate import Limits, PortfolioState, check, decide

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def proposal(**kw):
    return Proposal(
        **(dict(symbol="BTC/USDT", ts=NOW, direction=1, score=0.8, size_quote=100, signals=[]) | kw)
    )


def portfolio(**kw):
    return PortfolioState(
        **(
            dict(
                ts=NOW,
                equity=10000,
                day_start_equity=10000,
                day_start_ts=NOW,
                slippage_curve=[(10000, 10)],
            )
            | kw
        )
    )


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"halted": True}, "kill_switch"),
        ({"equity": 9700}, "max_daily_drawdown_pct"),
        ({"drawdown_breached_at": NOW}, "max_daily_drawdown_pct"),
        ({"positions": {"BTC/USDT": 1000}}, "max_position_pct_equity"),
        ({"positions": {"ETH/USDT": 5000}}, "max_gross_exposure_pct"),
        ({"trades": [NOW] * 20}, "max_trades_per_hour"),
        (
            {"last_trade_by_symbol": {"BTC/USDT": NOW - timedelta(seconds=299)}},
            "min_time_between_same_symbol_s",
        ),
        ({"slippage_curve": [(100, 16)]}, "max_slippage_bps"),
        ({"slippage_curve": []}, "missing_or_invalid_slippage_curve"),
    ],
)
def test_each_veto(changes, reason):
    result = decide(proposal(), portfolio(**changes), Limits())
    assert result.verdict == "REJECT" and result.size_quote == 0 and result.reason == reason


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"positions": {"BTC/USDT": 950}}, 50),
        ({"positions": {"ETH/USDT": 4975}}, 25),
        ({"slippage_curve": [(40, 10), (100, 20)]}, 40),
    ],
)
def test_resizes(changes, expected):
    result = decide(proposal(), portfolio(**changes), Limits())
    assert result.verdict == "RESIZE" and result.size_quote == expected


def test_allow_exact_cooldown_and_hour_boundary():
    state = portfolio(
        trades=[NOW - timedelta(hours=1)] * 20,
        last_trade_by_symbol={"BTC/USDT": NOW - timedelta(seconds=300)},
    )
    assert decide(proposal(), state, Limits()).verdict == "ALLOW"


def test_drawdown_resets_next_utc_day_with_new_baseline():
    assert (
        decide(
            proposal(), portfolio(drawdown_breached_at=NOW - timedelta(days=1)), Limits()
        ).verdict
        == "ALLOW"
    )
    assert (
        decide(proposal(), portfolio(day_start_ts=NOW - timedelta(days=1)), Limits()).verdict
        == "REJECT"
    )


def test_spot_sell_cannot_open_short():
    p = proposal(direction=-1, score=-0.8)
    assert decide(p, portfolio(), Limits()).verdict == "REJECT"
    assert decide(p, portfolio(positions={"BTC/USDT": 30}), Limits()).size_quote == 30


def test_real_halt_file_boundary(tmp_path):
    path = tmp_path / "HALT"
    limits = Limits(kill_switch=str(path))
    assert check(proposal(), portfolio(), limits).verdict == "ALLOW"
    path.touch()
    assert check(proposal(), portfolio(), limits).reason == "kill_switch"


def test_reserved_exposure_limits_sequence():
    state = portfolio()
    for _ in range(20):
        result = decide(proposal(), state, Limits())
        state.positions["BTC/USDT"] = state.positions.get("BTC/USDT", 0) + result.size_quote
        assert state.positions["BTC/USDT"] <= 1000
