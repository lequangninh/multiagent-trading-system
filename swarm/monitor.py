"""Operational alerts: feed stale, daily drawdown, agent exception rate, reconcile drift.

``gather`` reads the database into a plain dict, ``evaluate`` is a pure function from
that dict to alerts, ``notify`` logs, records an ``events`` row and optionally POSTs a
webhook. Run standalone with ``python -m swarm.monitor``; the paper runner also starts it.
"""

import argparse
import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import structlog
from pydantic import Field

from swarm.models import Model
from swarm.telemetry import Telemetry

log = structlog.get_logger()


class Thresholds(Model):
    interval_s: int = Field(default=30, gt=0)
    cooldown_s: int = Field(default=600, ge=0)
    feed_stale_s: float = Field(default=60, gt=0)
    daily_drawdown_pct: float = Field(default=0.02, gt=0, le=1)
    agent_exceptions_per_min: int = Field(default=5, ge=0)
    webhook_url: str | None = None


@dataclass(frozen=True)
class Alert:
    key: str  # Dedup key, e.g. "feed_stale:BTC/USDT"
    kind: str
    symbol: str | None
    detail: str


def evaluate(snapshot: dict, thresholds: Thresholds, now: datetime) -> list[Alert]:
    alerts = []
    for symbol in snapshot.get("symbols", []):
        last = snapshot.get("last_book", {}).get(symbol)
        age = (now - last).total_seconds() if last else None
        if age is None or age > thresholds.feed_stale_s:
            detail = "no book snapshots stored" if age is None else f"last book {age:.0f}s ago"
            alerts.append(Alert(f"feed_stale:{symbol}", "feed_stale", symbol, detail))
    start, latest = snapshot.get("day_start_equity"), snapshot.get("latest_equity")
    if start and latest is not None and start > 0:
        drawdown = (start - latest) / start
        if drawdown > thresholds.daily_drawdown_pct:
            alerts.append(
                Alert(
                    "daily_drawdown",
                    "daily_drawdown",
                    None,
                    f"drawdown {drawdown * 100:.2f}% from day start {start:.2f} to {latest:.2f}",
                )
            )
    exceptions = snapshot.get("agent_exceptions_last_min", 0)
    if exceptions > thresholds.agent_exceptions_per_min:
        alerts.append(
            Alert(
                "agent_exceptions",
                "agent_exceptions",
                None,
                f"{exceptions} agent exceptions in the last minute",
            )
        )
    for row in snapshot.get("reconcile_discrepancies", []):
        alerts.append(
            Alert(
                f"reconcile_discrepancy:{row['id']}",
                "reconcile_discrepancy",
                row.get("symbol"),
                row.get("detail", ""),
            )
        )
    return alerts


def valid_webhook(url):
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("alerts.webhook_url must be an http(s) URL")
    return url


class Monitor:
    def __init__(self, pool, symbols, thresholds: Thresholds, *, clock=None, post=None):
        self.pool, self.symbols, self.thresholds = pool, list(symbols), thresholds
        self.clock = clock or (lambda: datetime.now(UTC))
        self.webhook = valid_webhook(
            thresholds.webhook_url or os.environ.get("SWARM_ALERT_WEBHOOK")
        )
        self._post = post or self._http_post
        self.telemetry = Telemetry(pool, clock=self.clock)
        self.last_fired: dict[str, datetime] = {}
        self.cursor = self.clock()  # Reconcile events are reported once each, after this point.

    async def gather(self) -> dict:
        now = self.clock()
        books = await self.pool.fetch(
            "SELECT symbol, max(ts) AS ts FROM book_snapshots WHERE symbol = ANY($1) GROUP BY symbol",
            self.symbols,
        )
        day_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        first = await self.pool.fetchval(
            "SELECT equity FROM equity_snapshots WHERE ts >= $1 ORDER BY ts LIMIT 1", day_start
        )
        latest = await self.pool.fetchval(
            "SELECT equity FROM equity_snapshots WHERE ts >= $1 ORDER BY ts DESC LIMIT 1", day_start
        )
        exceptions = await self.pool.fetchval(
            "SELECT count(*) FROM events WHERE kind='agent_exception' AND ts > $1",
            now - timedelta(minutes=1),
        )
        discrepancies = await self.pool.fetch(
            """SELECT id, symbol, detail FROM events
            WHERE kind='reconcile_discrepancy' AND ts > $1 ORDER BY id""",
            self.cursor,
        )
        self.cursor = now
        return {
            "symbols": self.symbols,
            "last_book": {r["symbol"]: r["ts"] for r in books},
            "day_start_equity": first,
            "latest_equity": latest,
            "agent_exceptions_last_min": int(exceptions or 0),
            "reconcile_discrepancies": [dict(r) for r in discrepancies],
        }

    async def _http_post(self, url, payload):
        import aiohttp

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.post(url, json=payload) as response:
                response.raise_for_status()

    async def notify(self, alerts: list[Alert]) -> list[Alert]:
        now = self.clock()
        fired = []
        for alert in alerts:
            last = self.last_fired.get(alert.key)
            if last and (now - last).total_seconds() < self.thresholds.cooldown_s:
                continue
            self.last_fired[alert.key] = now
            fired.append(alert)
            log.warning("alert", kind=alert.kind, symbol=alert.symbol, detail=alert.detail)
            await self.telemetry.event("alert", alert.symbol, f"{alert.kind}: {alert.detail}", now)
            if self.webhook:
                payload = {
                    "ts": now.isoformat(),
                    "kind": alert.kind,
                    "symbol": alert.symbol,
                    "detail": alert.detail,
                }
                try:
                    await self._post(self.webhook, payload)
                except Exception as exc:
                    log.warning("alert_webhook_failed", error_type=type(exc).__name__)
        return fired

    async def check(self) -> list[Alert]:
        try:
            snapshot = await self.gather()
        except Exception as exc:
            log.warning("monitor_gather_failed", error_type=type(exc).__name__)
            return []
        return await self.notify(evaluate(snapshot, self.thresholds, self.clock()))

    async def run(self):
        while True:
            await self.check()
            await asyncio.sleep(self.thresholds.interval_s)


async def main_async(config, once=False):
    from swarm.data.store import Store

    store = Store(config["database"]["dsn"])
    try:
        await store.connect()
        monitor = Monitor(store.pool, config["universe"], Thresholds(**config.get("alerts", {})))
        if once:
            fired = await monitor.check()
            print(f"{len(fired)} alert(s) fired")
        else:
            await monitor.run()
    finally:
        await store.close()


def main(argv=None):
    from swarm.main import load_config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one check and exit")
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).resolve().parents[1] / "config/settings.yaml"
    )
    args = parser.parse_args(argv)
    config = load_config(args.config)
    asyncio.run(main_async(config, args.once))


if __name__ == "__main__":
    main()
