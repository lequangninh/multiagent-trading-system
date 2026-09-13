from datetime import UTC, datetime

from swarm.data.backfill import backfill
from swarm.data.news import parse_feed


class Exchange:
    def __init__(self):
        self.calls = 0

    async def fetch_ohlcv(self, symbol, timeframe, since, limit):
        self.calls += 1
        if self.calls > 1:
            return []
        return [[since, 10, 12, 9, 11, 5]]


class Store:
    def __init__(self):
        self.candles = []

    async def upsert_candles(self, candles):
        self.candles.extend(candles)


async def test_backfill_stops_on_empty_page():
    store = Store()
    count = await backfill("BTC/USDT", 1, Exchange(), store)
    assert count == 1
    assert store.candles[0].timeframe == "1m"


def test_parse_rss_and_symbol_tags():
    xml = """<rss><channel><item><title>Bitcoin and ETH move</title>
    <description>Solana follows</description><link>https://example.com/a</link>
    <pubDate>Thu, 01 Jan 2026 00:00:00 GMT</pubDate></item></channel></rss>"""
    [item] = parse_feed(xml, "example.com")
    assert item["ts"] == datetime(2026, 1, 1, tzinfo=UTC)
    assert item["symbol_tags"] == ["BTC", "ETH", "SOL"]


def test_parse_rss_skips_incomplete_items():
    assert parse_feed("<rss><channel><item><title>x</title></item></channel></rss>", "x") == []
