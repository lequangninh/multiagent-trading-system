"""Markdown summary of recent paper trading: ``make report`` / ``python -m swarm.report``.

``gather`` reads the database into a dict; ``render`` is a pure function to markdown.
"""

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

HIT_RATE_SQL = """
SELECT v.agent,
       count(*)::int AS votes,
       avg(CASE WHEN sign(later.close - entry.close) = (v.signal->>'direction')::int
                THEN 1.0 ELSE 0.0 END) AS hit_rate
FROM votes v
JOIN LATERAL (SELECT close FROM candles c WHERE c.symbol = v.symbol AND c.timeframe = '1m'
              AND c.ts <= v.ts ORDER BY c.ts DESC LIMIT 1) entry ON true
JOIN LATERAL (SELECT close FROM candles c WHERE c.symbol = v.symbol AND c.timeframe = '1m'
              AND c.ts >= v.ts + interval '30 minutes' ORDER BY c.ts LIMIT 1) later ON true
WHERE (v.signal->>'direction')::int <> 0 AND v.ts >= $1 AND v.ts < $2
GROUP BY v.agent ORDER BY v.agent
"""


async def gather(pool, symbols, start, end) -> dict:
    equity = await pool.fetch(
        "SELECT ts, equity FROM equity_snapshots WHERE ts >= $1 AND ts < $2 ORDER BY ts",
        start,
        end,
    )
    orders = await pool.fetch(
        """SELECT payload->>'status' AS status, count(*)::int AS n FROM orders
        WHERE ts >= $1 AND ts < $2 GROUP BY 1 ORDER BY 1""",
        start,
        end,
    )
    fills = [
        json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
        for r in await pool.fetch("SELECT payload FROM fills")
    ]
    fills = [f for f in fills if start <= datetime.fromisoformat(f["ts"]) < end]
    positions = await pool.fetch("SELECT symbol, quantity, ts FROM positions ORDER BY symbol")
    hit_rates = await pool.fetch(HIT_RATE_SQL, start, end)
    votes = await pool.fetchval("SELECT count(*) FROM votes WHERE ts >= $1 AND ts < $2", start, end)
    decisions = await pool.fetch(
        """SELECT verdict, reason, count(*)::int AS n FROM risk_decisions
        WHERE ts >= $1 AND ts < $2 GROUP BY 1, 2 ORDER BY 1, 3 DESC""",
        start,
        end,
    )
    feed = await pool.fetch(
        """SELECT symbol, count(DISTINCT time_bucket('1 minute', ts))::int AS minutes, max(ts) AS last
        FROM book_snapshots WHERE symbol = ANY($1) AND ts >= $2 AND ts < $3 GROUP BY symbol""",
        list(symbols),
        start,
        end,
    )
    events = await pool.fetch(
        """SELECT kind, count(*)::int AS n FROM events
        WHERE ts >= $1 AND ts < $2 GROUP BY kind ORDER BY kind""",
        start,
        end,
    )
    llm = await pool.fetch(
        """SELECT is_mock, count(*)::int AS calls, sum(CASE WHEN ok THEN 1 ELSE 0 END)::int AS ok,
        coalesce(sum(estimated_cost_usd), 0) AS cost FROM llm_calls
        WHERE ts >= $1 AND ts < $2 GROUP BY is_mock ORDER BY is_mock""",
        start,
        end,
    )
    return {
        "start": start,
        "end": end,
        "symbols": list(symbols),
        "equity": [dict(r) for r in equity],
        "orders": [dict(r) for r in orders],
        "fills": fills,
        "positions": [dict(r) for r in positions],
        "hit_rates": [dict(r) for r in hit_rates],
        "votes": int(votes or 0),
        "decisions": [dict(r) for r in decisions],
        "feed": {r["symbol"]: dict(r) for r in feed},
        "events": [dict(r) for r in events],
        "llm": [dict(r) for r in llm],
    }


def _table(headers, rows):
    if not rows:
        return "_none_\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(out) + "\n"


def render(data: dict) -> str:
    start, end = data["start"], data["end"]
    minutes = max(1, int((end - start).total_seconds() // 60))
    lines = [
        f"# Paper trading report: {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC",
        "",
        "Testnet paper trading only. No real funds; no proven edge.",
        "",
        "## Equity",
        "",
    ]
    equity = data["equity"]
    if equity:
        values = [r["equity"] for r in equity]
        peak, worst = values[0], 0.0
        for v in values:
            peak = max(peak, v)
            worst = max(worst, (peak - v) / peak if peak > 0 else 0)
        lines += _table(
            ["first", "last", "return", "max drawdown", "snapshots"],
            [
                [
                    f"{values[0]:.2f}",
                    f"{values[-1]:.2f}",
                    f"{(values[-1] / values[0] - 1) * 100:+.3f}%",
                    f"{worst * 100:.3f}%",
                    len(values),
                ]
            ],
        ).splitlines()
    else:
        lines.append("_no equity snapshots; run paper mode_")
    fills = data["fills"]
    volume = sum(f["quantity"] * f["price"] for f in fills)
    fees = sum(f["fee"] for f in fills)
    lines += ["", "## Orders and fills", ""]
    lines += _table(
        ["status", "orders"], [[r["status"], r["n"]] for r in data["orders"]]
    ).splitlines()
    lines += [
        "",
        f"Fills: {len(fills)}, traded volume {volume:.2f} USDT, fees {fees:.4f} "
        f"({(fees / volume * 100) if volume else 0:.3f}% of volume).",
        "",
        "## Positions",
        "",
    ]
    lines += _table(
        ["symbol", "quantity", "updated"],
        [
            [r["symbol"], f"{r['quantity']:.6g}", f"{r['ts']:%Y-%m-%d %H:%M}"]
            for r in data["positions"]
        ],
    ).splitlines()
    lines += ["", f"## Agents ({data['votes']} votes recorded)", ""]
    lines += _table(
        ["agent", "directional votes", "hit rate (30m)"],
        [[r["agent"], r["votes"], f"{r['hit_rate'] * 100:.1f}%"] for r in data["hit_rates"]],
    ).splitlines()
    lines += ["", "## Risk decisions", ""]
    lines += _table(
        ["verdict", "reason", "count"],
        [[r["verdict"], r["reason"], r["n"]] for r in data["decisions"]],
    ).splitlines()
    lines += ["", "## Feed coverage", ""]
    rows = []
    for symbol in data["symbols"]:
        row = data["feed"].get(symbol)
        covered = row["minutes"] if row else 0
        last = f"{row['last']:%Y-%m-%d %H:%M}" if row else "never"
        rows.append([symbol, f"{covered}/{minutes}", f"{covered / minutes * 100:.1f}%", last])
    lines += _table(["symbol", "minutes with book", "coverage", "last snapshot"], rows).splitlines()
    lines += ["", "## Events and alerts", ""]
    lines += _table(["kind", "count"], [[r["kind"], r["n"]] for r in data["events"]]).splitlines()
    lines += ["", "## LLM usage (estimated cost)", ""]
    lines += _table(
        ["mode", "calls", "ok", "est. cost USD"],
        [
            ["mock" if r["is_mock"] else "real", r["calls"], r["ok"], f"{r['cost']:.4f}"]
            for r in data["llm"]
        ],
    ).splitlines()
    return "\n".join(lines) + "\n"


async def main_async(config, days, out_dir):
    from swarm.data.store import Store

    end = datetime.now(UTC).replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    store = Store(config["database"]["dsn"])
    try:
        await store.connect()
        data = await gather(store.pool, config["universe"], start, end)
    finally:
        await store.close()
    text = render(data)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"paper_{end:%Y-%m-%d}.md"
    path.write_text(text)
    print(text)
    print(f"written {path}")


def main(argv=None):
    from swarm.main import validate_config

    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--out", type=Path, default=root / "reports")
    parser.add_argument("--config", type=Path, default=root / "config/settings.yaml")
    args = parser.parse_args(argv)
    if args.days <= 0:
        parser.error("--days must be positive")
    config = validate_config(yaml.safe_load(args.config.read_text()))
    asyncio.run(main_async(config, args.days, args.out))


if __name__ == "__main__":
    main()
