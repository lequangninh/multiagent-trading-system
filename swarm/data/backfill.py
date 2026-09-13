"""Backfill closed 1m candles from Binance Spot Testnet."""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt.async_support as ccxt
import yaml

from swarm.data.store import Store
from swarm.models import Candle


async def backfill(symbol: str, days: int, exchange, store: Store) -> int:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    cursor = int(since.timestamp() * 1000)
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    count = 0
    while cursor < end:
        batch = await exchange.fetch_ohlcv(symbol, "1m", since=cursor, limit=1000)
        if not batch:
            break
        candles = [
            Candle(
                symbol=symbol,
                ts=datetime.fromtimestamp(row[0] / 1000, timezone.utc),
                open=row[1],
                high=row[2],
                low=row[3],
                close=row[4],
                volume=row[5],
                timeframe="1m",
            )
            for row in batch
            if row[0] < end
        ]
        await store.upsert_candles(candles)
        count += len(candles)
        next_cursor = batch[-1][0] + 60_000
        if next_cursor <= cursor:
            raise RuntimeError("exchange pagination did not advance")
        cursor = next_cursor
    return count


async def run(args) -> None:
    config = yaml.safe_load(args.config.read_text())
    exchange = ccxt.binance({"enableRateLimit": True})
    exchange.set_sandbox_mode(True)
    store = Store(config["database"]["dsn"])
    try:
        await store.connect()
        count = await backfill(args.symbol, args.days, exchange, store)
        print(f"upserted {count} candles for {args.symbol}")
    finally:
        await exchange.close()
        await store.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--days", type=int, required=True)
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).resolve().parents[2] / "config/settings.yaml"
    )
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("--days must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
