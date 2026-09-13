"""Async TimescaleDB persistence and read API.

Only 1m candles are stored. The 5m and 15m series are resampled on read so late
backfills are immediately reflected without refreshing continuous aggregates.
"""

import json
from datetime import datetime
from pathlib import Path

import asyncpg

from swarm.models import BookSnapshot, Candle

SCHEMA = Path(__file__).with_name("schema.sql")


class Store:
    def __init__(self, dsn: str, *, pool=None):
        self.dsn = dsn
        self.pool = pool
        self._owns_pool = pool is None

    async def connect(self) -> "Store":
        if self.pool is None:
            self.pool = await asyncpg.create_pool(self.dsn)
        await self.apply_schema()
        return self

    async def apply_schema(self) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(SCHEMA.read_text())

    async def close(self) -> None:
        if self._owns_pool and self.pool is not None:
            await self.pool.close()

    async def upsert_candles(self, candles: list[Candle]) -> None:
        if not candles:
            return
        rows = [
            (c.symbol, c.ts, c.timeframe, c.open, c.high, c.low, c.close, c.volume) for c in candles
        ]
        async with self.pool.acquire() as connection:
            await connection.executemany(
                """INSERT INTO candles
                (symbol, ts, timeframe, open, high, low, close, volume)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT (symbol, timeframe, ts) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume=excluded.volume""",
                rows,
            )

    async def insert_book(self, book: BookSnapshot) -> None:
        await self.pool.execute(
            """INSERT INTO book_snapshots (symbol, ts, bids, asks)
            VALUES ($1,$2,$3::jsonb,$4::jsonb)
            ON CONFLICT (symbol, ts) DO UPDATE SET bids=excluded.bids, asks=excluded.asks""",
            book.symbol,
            book.ts,
            json.dumps(book.bids),
            json.dumps(book.asks),
        )

    async def get_candles(
        self, symbol: str, tf: str, start: datetime, end: datetime
    ) -> list[Candle]:
        if tf not in {"1m", "5m", "15m"}:
            raise ValueError("timeframe must be 1m, 5m, or 15m")
        if tf == "1m":
            query = """SELECT symbol, ts, open, high, low, close, volume, timeframe
                FROM candles WHERE symbol=$1 AND timeframe='1m' AND ts >= $2 AND ts < $3
                ORDER BY ts"""
        else:
            minutes = int(tf[:-1])
            query = f"""SELECT symbol, time_bucket('{minutes} minutes', ts) AS ts,
                first(open, ts) AS open, max(high) AS high, min(low) AS low,
                last(close, ts) AS close, sum(volume) AS volume, '{tf}' AS timeframe
                FROM candles WHERE symbol=$1 AND timeframe='1m' AND ts >= $2 AND ts < $3
                GROUP BY symbol, time_bucket('{minutes} minutes', ts) ORDER BY ts"""
        rows = await self.pool.fetch(query, symbol, start, end)
        return [Candle.model_validate(dict(row)) for row in rows]

    async def get_latest_book(self, symbol: str) -> BookSnapshot | None:
        row = await self.pool.fetchrow(
            """SELECT symbol, ts, bids, asks FROM book_snapshots
            WHERE symbol=$1 ORDER BY ts DESC LIMIT 1""",
            symbol,
        )
        if not row:
            return None
        data = dict(row)
        for side in ("bids", "asks"):
            if isinstance(data[side], str):
                data[side] = json.loads(data[side])
        return BookSnapshot.model_validate(data)

    async def get_news(self, since: datetime) -> list[dict]:
        rows = await self.pool.fetch(
            """SELECT ts, source, title, body, url, symbol_tags FROM news
            WHERE ts >= $1 ORDER BY ts""",
            since,
        )
        return [dict(row) for row in rows]
