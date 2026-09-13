"""Backfill closed 1m candles from Binance Spot Testnet."""

import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import ccxt.async_support as ccxt
import yaml

from swarm.data.store import Store
from swarm.main import validate_config
from swarm.models import Candle


async def backfill(symbol: str, days: int, exchange, store: Store) -> int:
    if days <= 0:
        raise ValueError("days must be positive")
    end = int(datetime.now(timezone.utc).timestamp() * 1000) // 60_000 * 60_000
    cursor = end - days * 86_400_000
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
            if cursor <= row[0] < end
        ]
        await store.upsert_candles(candles)
        count += len(candles)
        next_cursor = batch[-1][0] + 60_000
        if next_cursor <= cursor:
            raise RuntimeError("exchange pagination did not advance")
        cursor = next_cursor
    return count


async def run(args) -> None:
    config = validate_config(yaml.safe_load(args.config.read_text()))
    exchange = ccxt.binance(
        {
            "enableRateLimit": True,
            "options": {"defaultType": "spot", "fetchMarkets": {"types": ["spot"]}},
        }
    )
    exchange.set_sandbox_mode(True)
    store = Store(config["database"]["dsn"])
    try:
        await store.connect()
        count = await backfill(args.symbol, args.days, exchange, store)
        print(f"upserted {count} candles for {args.symbol}")
    finally:
        try:
            await exchange.close()
        finally:
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
