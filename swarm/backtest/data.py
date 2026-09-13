"""Replay dataset: canonical 1m candles resampled in memory, per-minute books, sentiment.

Resampling mirrors ``Store.get_candles``: a 5m/15m bar exists only when every one of
its 1m constituents is present, so gaps in history never produce phantom bars.
"""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from swarm.models import BookSnapshot, Candle

TIMEFRAMES = ("5m", "15m")
LOOKBACK_HINT = "the feed stores books only while `python -m swarm.data` or paper mode runs"


def resample(candles: list[Candle], minutes: int) -> list[Candle]:
    if minutes == 1:
        return sorted(candles, key=lambda c: c.ts)
    buckets: dict[datetime, list[Candle]] = {}
    for c in candles:
        if c.timeframe != "1m":
            raise ValueError("only canonical 1m candles may be resampled")
        bucket = c.ts - timedelta(
            minutes=c.ts.minute % minutes, seconds=c.ts.second, microseconds=c.ts.microsecond
        )
        buckets.setdefault(bucket, []).append(c)
    out = []
    for bucket in sorted(buckets):
        rows = sorted(buckets[bucket], key=lambda c: c.ts)
        if [c.ts for c in rows] != [bucket + timedelta(minutes=i) for i in range(minutes)]:
            continue  # Incomplete bucket: identical to the SQL HAVING count(*) = minutes rule.
        out.append(
            Candle(
                symbol=rows[0].symbol,
                ts=bucket,
                open=rows[0].open,
                high=max(c.high for c in rows),
                low=min(c.low for c in rows),
                close=rows[-1].close,
                volume=sum(c.volume for c in rows),
                timeframe=f"{minutes}m",
            )
        )
    return out


@dataclass
class SymbolData:
    symbol: str
    candles: dict[str, list[Candle]]
    books: list[BookSnapshot]
    sentiment: list[dict] = field(default_factory=list)
    _index: dict[str, list[datetime]] = field(init=False, repr=False)

    @classmethod
    def build(cls, symbol, candles_1m, books=(), sentiment=()):
        ordered = resample(list(candles_1m), 1)
        if any(c.symbol != symbol for c in ordered):
            raise ValueError("candles belong to a different symbol")
        candles = {"1m": ordered} | {tf: resample(ordered, int(tf[:-1])) for tf in TIMEFRAMES}
        return cls(
            symbol,
            candles,
            sorted((b for b in books if b.symbol == symbol), key=lambda b: b.ts),
            sorted((dict(r) for r in sentiment), key=lambda r: r["ts"]),
        )

    def __post_init__(self):
        self._index = {tf: [c.ts for c in rows] for tf, rows in self.candles.items()}
        self._book_ts = [b.ts for b in self.books]
        self._sentiment_ts = [r["ts"] for r in self.sentiment]

    def window(self, tf: str, start: datetime, end: datetime) -> list[Candle]:
        """Candles with start <= ts < end."""
        index = self._index[tf]
        return self.candles[tf][bisect_left(index, start) : bisect_left(index, end)]

    def candle_closing_at(self, now: datetime) -> Candle | None:
        index = self._index["1m"]
        i = bisect_left(index, now - timedelta(minutes=1))
        if i < len(index) and index[i] == now - timedelta(minutes=1):
            return self.candles["1m"][i]
        return None

    def book_at(self, now: datetime) -> BookSnapshot | None:
        i = bisect_right(self._book_ts, now)
        return self.books[i - 1] if i else None

    def sentiment_at(self, now: datetime) -> dict | None:
        i = bisect_right(self._sentiment_ts, now)
        return self.sentiment[i - 1] if i else None


@dataclass
class Dataset:
    symbols: dict[str, SymbolData]

    def __getitem__(self, symbol: str) -> SymbolData:
        return self.symbols[symbol]

    def decision_points(self, start: datetime, end: datetime):
        """Yield (now, symbols) for every minute in [start, end) at which a 1m candle closes."""
        due: dict[datetime, list[str]] = {}
        for symbol, data in self.symbols.items():
            for ts in data.window("1m", start - timedelta(minutes=1), end - timedelta(minutes=1)):
                due.setdefault(ts.ts + timedelta(minutes=1), []).append(symbol)
        for now in sorted(due):
            yield now, due[now]

    def span(self):
        stamps = [c.ts for d in self.symbols.values() for c in d.candles["1m"]]
        return (min(stamps), max(stamps) + timedelta(minutes=1)) if stamps else (None, None)


async def load_dataset(store, symbols, start, end) -> Dataset:
    data = {}
    for symbol in symbols:
        data[symbol] = SymbolData.build(
            symbol,
            await store.get_candles(symbol, "1m", start, end),
            await store.get_books(symbol, start, end),
            await store.get_sentiment_scores(symbol, start, end),
        )
    return Dataset(data)
