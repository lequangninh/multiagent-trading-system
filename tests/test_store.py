from datetime import UTC, datetime

import pytest

from swarm.data.store import Store

TS = datetime(2026, 1, 1, tzinfo=UTC)


class Pool:
    def __init__(self, rows=None, row=None):
        self.rows = rows or []
        self.row = row
        self.query = None
        self.args = None

    async def fetch(self, query, *args):
        self.query, self.args = query, args
        return self.rows

    async def fetchrow(self, query, *args):
        self.query, self.args = query, args
        return self.row


async def test_get_one_minute_candles():
    pool = Pool(
        rows=[
            {
                "symbol": "BTC/USDT",
                "ts": TS,
                "open": 10,
                "high": 12,
                "low": 9,
                "close": 11,
                "volume": 4,
                "timeframe": "1m",
            }
        ]
    )
    result = await Store("unused", pool=pool).get_candles("BTC/USDT", "1m", TS, TS)
    assert result[0].close == 11
    assert "timeframe='1m'" in pool.query


@pytest.mark.parametrize(("tf", "bucket"), [("5m", "5 minutes"), ("15m", "15 minutes")])
async def test_resamples_on_read(tf, bucket):
    pool = Pool()
    assert await Store("unused", pool=pool).get_candles("BTC/USDT", tf, TS, TS) == []
    assert f"time_bucket('{bucket}'" in pool.query
    assert f"'{tf}' AS timeframe" in pool.query


async def test_rejects_unknown_timeframe():
    with pytest.raises(ValueError):
        await Store("unused", pool=Pool()).get_candles("BTC/USDT", "1h", TS, TS)


async def test_latest_book_and_no_book():
    row = {"symbol": "BTC/USDT", "ts": TS, "bids": "[[10, 1]]", "asks": "[[11, 2]]"}
    result = await Store("unused", pool=Pool(row=row)).get_latest_book("BTC/USDT")
    assert result.bids == [(10, 1)]
    assert await Store("unused", pool=Pool()).get_latest_book("ETH/USDT") is None


async def test_news_rows_are_plain_dicts():
    row = {
        "ts": TS,
        "source": "source",
        "title": "title",
        "body": "body",
        "url": "https://example.com",
        "symbol_tags": ["BTC"],
    }
    assert await Store("unused", pool=Pool(rows=[row])).get_news(TS) == [row]
