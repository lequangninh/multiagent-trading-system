"""M8: telemetry never raises, alert thresholds are table-driven, report renders, dashboard valid."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from swarm.models import Proposal, RiskDecision
from swarm.monitor import Alert, Monitor, Thresholds, evaluate, valid_webhook
from swarm.report import render
from swarm.telemetry import NullTelemetry, Telemetry

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]
SYMBOLS = ["BTC/USDT", "ETH/USDT"]


def decision(verdict="REJECT", reason="kill_switch"):
    p = Proposal(symbol="BTC/USDT", ts=NOW, direction=1, score=0.8, size_quote=100, signals=[])
    return RiskDecision(proposal=p, verdict=verdict, size_quote=0, reason=reason)


# --- Telemetry -------------------------------------------------------------------


async def test_telemetry_writes_rows_and_swallows_failures():
    executor = AsyncMock()
    t = Telemetry(executor, clock=lambda: NOW)
    await t.risk_decision(decision(), "oms")
    await t.equity(NOW, 10_000, 9_000, 1_000)
    await t.event("agent_exception", "BTC/USDT", "x" * 5000)
    await t.llm_call(NOW, "m", True, 3, 100, 20, True, 0.0)
    assert executor.execute.await_count == 4
    args = executor.execute.await_args_list[0].args
    assert args[1:] == (NOW, "BTC/USDT", "REJECT", "kill_switch", 0.0, 0.8, "oms")
    assert len(executor.execute.await_args_list[2].args[4]) == 2000
    executor.execute.side_effect = RuntimeError("db down")
    await t.equity(NOW, 1, 1, 0)  # Must not raise: telemetry never touches the trade path.
    for method in ("equity", "risk_decision", "event", "llm_call"):
        assert hasattr(NullTelemetry(), method)


# --- Alerts ----------------------------------------------------------------------


def healthy():
    return {
        "symbols": SYMBOLS,
        "last_book": {s: NOW - timedelta(seconds=5) for s in SYMBOLS},
        "day_start_equity": 10_000,
        "latest_equity": 9_900,
        "agent_exceptions_last_min": 5,
        "reconcile_discrepancies": [],
    }


@pytest.mark.parametrize(
    "changes,kinds",
    [
        ({}, []),
        ({"last_book": {"BTC/USDT": NOW - timedelta(seconds=61)}}, ["feed_stale", "feed_stale"]),
        (
            {"last_book": {**{s: NOW for s in SYMBOLS}, "ETH/USDT": NOW - timedelta(61)}},
            ["feed_stale"],
        ),
        ({"latest_equity": 9_799}, ["daily_drawdown"]),
        ({"latest_equity": 9_800}, []),
        ({"day_start_equity": None}, []),
        ({"agent_exceptions_last_min": 6}, ["agent_exceptions"]),
        (
            {"reconcile_discrepancies": [{"id": 7, "symbol": None, "detail": "corrected=1"}]},
            ["reconcile_discrepancy"],
        ),
    ],
)
def test_each_alert_threshold(changes, kinds):
    alerts = evaluate(healthy() | changes, Thresholds(), NOW)
    assert [a.kind for a in alerts] == kinds


async def test_notify_logs_records_event_posts_webhook_and_cools_down():
    pool = AsyncMock()
    posts = []

    async def post(url, payload):
        posts.append((url, payload))

    clock = [NOW]
    monitor = Monitor(
        pool,
        SYMBOLS,
        Thresholds(cooldown_s=600, webhook_url="https://hooks.example/x"),
        clock=lambda: clock[0],
        post=post,
    )
    alert = Alert("feed_stale:BTC/USDT", "feed_stale", "BTC/USDT", "last book 90s ago")
    assert await monitor.notify([alert, alert]) == [alert]  # Same key fires once.
    assert pool.execute.await_count == 1 and pool.execute.await_args.args[2] == "alert"
    assert posts[0][0] == "https://hooks.example/x" and posts[0][1]["kind"] == "feed_stale"
    clock[0] = NOW + timedelta(seconds=599)
    assert await monitor.notify([alert]) == []
    clock[0] = NOW + timedelta(seconds=600)
    assert await monitor.notify([alert]) == [alert]
    monitor._post = AsyncMock(side_effect=RuntimeError("webhook down"))
    clock[0] = NOW + timedelta(seconds=1300)
    assert await monitor.notify([alert]) == [alert]  # Webhook failure never propagates.


async def test_check_survives_database_failure(monkeypatch):
    pool = AsyncMock()
    pool.fetch.side_effect = RuntimeError("db down")
    monitor = Monitor(pool, SYMBOLS, Thresholds(), clock=lambda: NOW)
    assert await monitor.check() == []


def test_webhook_validation(monkeypatch):
    assert valid_webhook(None) is None
    assert valid_webhook("http://localhost:9000/hook") == "http://localhost:9000/hook"
    with pytest.raises(ValueError):
        valid_webhook("ftp://x")
    monkeypatch.setenv("SWARM_ALERT_WEBHOOK", "https://env.example/hook")
    assert Monitor(AsyncMock(), SYMBOLS, Thresholds()).webhook == "https://env.example/hook"


# --- Report ----------------------------------------------------------------------


def test_report_renders_all_sections_with_and_without_data():
    empty = {
        "start": NOW - timedelta(days=7),
        "end": NOW,
        "symbols": SYMBOLS,
        "equity": [],
        "orders": [],
        "fills": [],
        "positions": [],
        "hit_rates": [],
        "votes": 0,
        "decisions": [],
        "feed": {},
        "events": [],
        "llm": [],
    }
    text = render(empty)
    for heading in (
        "## Equity",
        "## Orders and fills",
        "## Positions",
        "## Agents",
        "## Risk decisions",
        "## Feed coverage",
        "## Events and alerts",
        "## LLM usage",
    ):
        assert heading in text
    assert "no equity snapshots" in text and "0/10080" in text and "never trades real" not in text
    full = empty | {
        "equity": [{"ts": NOW, "equity": e} for e in (100, 120, 90, 110)],
        "orders": [{"status": "closed", "n": 2}],
        "fills": [{"quantity": 2, "price": 50, "fee": 0.1, "ts": NOW.isoformat()}],
        "positions": [{"symbol": "BTC/USDT", "quantity": 0.5, "ts": NOW}],
        "hit_rates": [{"agent": "momentum", "votes": 4, "hit_rate": 0.75}],
        "votes": 9,
        "decisions": [{"verdict": "REJECT", "reason": "kill_switch", "n": 3}],
        "feed": {"BTC/USDT": {"symbol": "BTC/USDT", "minutes": 5040, "last": NOW}},
        "events": [{"kind": "alert", "n": 1}],
        "llm": [{"is_mock": False, "calls": 2, "ok": 1, "cost": 0.0123}],
    }
    text = render(full)
    assert "+10.000%" in text and "25.000%" in text  # return and max drawdown
    assert "| momentum | 4 | 75.0%" in text and "| REJECT | kill_switch | 3 |" in text
    assert "5040/10080 | 50.0%" in text and "| real | 2 | 1 | 0.0123 |" in text
    assert "fees 0.1000 (0.100% of volume)" in text


# --- Grafana provisioning ---------------------------------------------------------


def test_dashboard_provisioning_is_consistent():
    dashboard = json.loads((ROOT / "dashboards/dashboards/json/swarm-trader.json").read_text())
    assert dashboard["uid"] == "swarm-trader" and dashboard["refresh"]
    titles = " ".join(p["title"].lower() for p in dashboard["panels"])
    for required in ("equity", "position", "hit rate", "risk rejections", "feed lag", "llm cost"):
        assert required in titles
    schema = (ROOT / "swarm/data/schema.sql").read_text()
    tables = {"equity_snapshots", "risk_decisions", "llm_calls", "events", "positions", "votes"}
    for panel in dashboard["panels"]:
        assert panel["datasource"]["uid"] == "timescale"
        sql = panel["targets"][0]["rawSql"].lower()
        assert any(t in sql for t in tables | {"book_snapshots", "orders", "candles"})
        for table in tables:
            if table in sql:
                assert f"create table if not exists {table}" in schema.lower()
    datasource = (ROOT / "dashboards/datasources/timescale.yaml").read_text()
    assert "uid: timescale" in datasource and "api.binance.com" not in datasource
    provider = (ROOT / "dashboards/dashboards/provider.yaml").read_text()
    assert "/etc/grafana/provisioning/dashboards/json" in provider
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "./dashboards:/etc/grafana/provisioning:ro" in compose
