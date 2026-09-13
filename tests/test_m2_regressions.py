import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swarm.data.backfill import backfill, run
from swarm.data.feed import MarketFeed
from swarm.data.news import parse_feed


async def test_backfill_excludes_open_candle_and_aligns_start():
    now = int(datetime.now(UTC).timestamp() * 1000) // 60000 * 60000
    exchange = SimpleNamespace(
        fetch_ohlcv=AsyncMock(
            return_value=[[now - 60000, 10, 12, 9, 11, 2], [now, 10, 12, 9, 11, 2]]
        )
    )
    store = SimpleNamespace(upsert_candles=AsyncMock())
    assert await backfill("BTC/USDT", 1, exchange, store) == 1
    assert exchange.fetch_ohlcv.call_args.kwargs["since"] % 60000 == 0
    assert len(store.upsert_candles.call_args.args[0]) == 1


async def test_backfill_rejects_unsafe_config_before_resources(tmp_path):
    path = tmp_path / "unsafe.yaml"
    path.write_text("exchange:\n  sandbox: false\n")
    with pytest.raises(ValueError, match="sandbox"):
        await run(SimpleNamespace(config=path))


async def test_feed_persists_entire_batch():
    exchange = SimpleNamespace(
        set_sandbox_mode=lambda enabled: None,
        watch_ohlcv=AsyncMock(
            side_effect=[
                [[60000, 10, 12, 9, 11, 2], [120000, 11, 12, 9, 10, 3]],
                asyncio.CancelledError(),
            ]
        ),
    )
    store = SimpleNamespace(upsert_candles=AsyncMock())
    feed = MarketFeed(["BTC/USDT"], store, exchange=exchange)
    with pytest.raises(asyncio.CancelledError):
        await feed._candles("BTC/USDT")
    assert store.upsert_candles.await_count == 2


def test_bad_rss_item_does_not_discard_good_items():
    payload = """<rss><channel>
    <item><title>bad</title><link>https://example.com/b</link><pubDate>invalid</pubDate></item>
    <item><title>good</title><link>https://example.com/g</link></item>
    </channel></rss>"""
    assert [item["title"] for item in parse_feed(payload, "example.com")] == ["good"]
