"""Resilient ccxt.pro Binance Spot Testnet feed."""

import asyncio
from datetime import datetime, timezone

import ccxt.pro as ccxtpro
import structlog

from swarm.data.store import Store
from swarm.models import BookSnapshot, Candle

log = structlog.get_logger()


class MarketFeed:
    def __init__(self, symbols: list[str], store: Store, *, exchange=None):
        self.symbols = symbols
        self.store = store
        self.exchange = exchange or ccxtpro.binance({"enableRateLimit": True})
        self.exchange.set_sandbox_mode(True)
        self._last_candle: dict[str, int] = {}

    async def _candles(self, symbol: str) -> None:
        while True:
            rows = await self.exchange.watch_ohlcv(symbol, "1m")
            if not rows:
                continue
            row = rows[-1]
            previous = self._last_candle.get(symbol)
            if previous is not None and row[0] - previous > 60_000:
                log.warning("candle_gap", symbol=symbol, gap_ms=row[0] - previous)
            self._last_candle[symbol] = row[0]
            candle = Candle(
                symbol=symbol,
                ts=datetime.fromtimestamp(row[0] / 1000, timezone.utc),
                open=row[1],
                high=row[2],
                low=row[3],
                close=row[4],
                volume=row[5],
                timeframe="1m",
            )
            await self.store.upsert_candles([candle])

    async def _books(self, symbol: str) -> None:
        while True:
            book = await self.exchange.watch_order_book(symbol, 20)
            timestamp = book.get("timestamp") or int(datetime.now(timezone.utc).timestamp() * 1000)
            snapshot = BookSnapshot(
                symbol=symbol,
                ts=datetime.fromtimestamp(timestamp / 1000, timezone.utc),
                bids=[tuple(level[:2]) for level in book["bids"][:20]],
                asks=[tuple(level[:2]) for level in book["asks"][:20]],
            )
            await self.store.insert_book(snapshot)

    async def _recover(self, worker, symbol: str) -> None:
        delay = 1
        while True:
            try:
                await worker(symbol)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("feed_reconnect", symbol=symbol, error=str(exc), delay_s=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            for symbol in self.symbols:
                group.create_task(self._recover(self._candles, symbol))
                group.create_task(self._recover(self._books, symbol))

    async def close(self) -> None:
        await self.exchange.close()
