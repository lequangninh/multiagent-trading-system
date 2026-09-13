"""Backtest CLI: uv run python -m swarm.backtest --symbol BTC/USDT --days 30

Walk-forward: add --train 60d --test 30d --step 30d. Parameters are tuned on each
train window only and applied to the following test window.
"""

import argparse
import asyncio
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from swarm.backtest.data import LOOKBACK_HINT, load_dataset
from swarm.backtest.engine import LOOKBACK, Backtester, Params
from swarm.backtest.metrics import write_outputs
from swarm.data.store import Store
from swarm.main import validate_config
from swarm.risk.gate import Limits

DEAD_BANNER = "\n" + "!" * 72 + "\nSTRATEGY DEAD: fees exceed gross alpha\n" + "!" * 72 + "\n"


def duration(text: str) -> timedelta:
    match = re.fullmatch(r"(\d+)([dhm])", text.strip())
    if not match:
        raise argparse.ArgumentTypeError("use <n>d, <n>h or <n>m, e.g. 60d")
    n, unit = int(match.group(1)), match.group(2)
    return timedelta(**{{"d": "days", "h": "hours", "m": "minutes"}[unit]: n})


def parse_args(argv=None):
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--days", type=int, required=True)
    parser.add_argument("--end", help="ISO-8601 UTC end (default: now, floored to the minute)")
    parser.add_argument("--train", type=duration)
    parser.add_argument("--test", type=duration)
    parser.add_argument("--step", type=duration)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--config", type=Path, default=root / "config/settings.yaml")
    args = parser.parse_args(argv)
    if args.days <= 0:
        parser.error("--days must be positive")
    given = [args.train, args.test, args.step]
    if any(given) and not all(given):
        parser.error("--train, --test and --step must be given together")
    return args


def summarize(result, coverage) -> str:
    m = result.metrics
    lines = [
        f"range        {result.start.isoformat()} -> {result.end.isoformat()}",
        f"params       {result.params.label()}",
        f"coverage     {coverage}",
        f"trades       {m['trades']} (round trips {m['round_trips']})",
        f"net return   {m['net_return_pct']:+.4f}%   gross {m['gross_return_pct']:+.4f}%   "
        f"fee drag {m['fee_drag_pct']:.4f}%",
        f"sharpe       {m['sharpe']:.3f}   max drawdown {m['max_drawdown'] * 100:.3f}%   "
        f"turnover {m['turnover']:.3f}",
        f"win rate     {m['win_rate']}   profit factor {m['profit_factor']}",
    ]
    return "\n".join(lines)


async def run(args) -> int:
    config = validate_config(yaml.safe_load(args.config.read_text()))
    if args.symbol not in config["universe"]:
        print(f"{args.symbol} is not in the configured universe", file=sys.stderr)
        return 1
    end = (
        datetime.fromisoformat(args.end).astimezone(UTC)
        if args.end
        else datetime.now(UTC).replace(second=0, microsecond=0)
    )
    start = end - timedelta(days=args.days)
    settings = config.get("backtest", {})
    store = Store(config["database"]["dsn"])
    try:
        await store.connect()
        dataset = await load_dataset(store, [args.symbol], start - LOOKBACK, end)
    finally:
        await store.close()
    data = dataset[args.symbol]
    if not data.candles["1m"]:
        print(f"no 1m candles stored for {args.symbol} in the requested range", file=sys.stderr)
        return 1
    first, last = dataset.span()
    minutes = sum(1 for _ in dataset.decision_points(start, end))
    with_book = sum(
        1
        for now, _ in dataset.decision_points(start, end)
        if (b := data.book_at(now)) and (now - b.ts).total_seconds() <= 60
    )
    coverage = (
        f"{len(data.candles['1m'])} candles {first.isoformat()} -> {last.isoformat()}; "
        f"{with_book}/{minutes} decision minutes have a fresh book"
    )
    if with_book == 0:
        print(f"warning: no fresh book snapshots in range; nothing can trade ({LOOKBACK_HINT})")
    bt = Backtester(
        dataset,
        Limits(**config["risk"]),
        initial_cash=settings.get("initial_cash", 10_000),
        fee_rate=settings.get("fee_rate", 0.001),
    )
    if args.train:
        try:
            result = await bt.walk_forward(start, end, args.train, args.test, args.step)
        except ValueError as exc:
            print(f"walk-forward aborted: {exc}", file=sys.stderr)
            return 2
    else:
        consensus = config["consensus"]
        params = Params(
            threshold=consensus["threshold"],
            base_size=consensus["base_size"],
            weights=consensus["weights"],
        )
        result = await bt.run(params, start, end)
    out = args.out or Path(settings.get("output_dir", "backtests")) / (
        f"{args.symbol.replace('/', '')}_{end.strftime('%Y%m%dT%H%M')}"
        + ("_wf" if args.train else "")
    )
    paths = write_outputs(out, result)
    print(summarize(result, coverage))
    for name, path in paths.items():
        print(f"{name:<12} {path}")
    if result.metrics["strategy_dead"]:
        print(DEAD_BANNER)
    elif result.metrics["trades"] == 0:
        print("no trades executed; fee guard not applicable")
    return 0


def main(argv=None):
    sys.exit(asyncio.run(run(parse_args(argv))))


if __name__ == "__main__":
    main()
