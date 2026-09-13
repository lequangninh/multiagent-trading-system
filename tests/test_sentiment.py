import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from swarm.agents.base import MarketState
from swarm.agents.sentiment import MockLLM, Sentiment

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SYMBOL = "BTC/USDT"
NEWS = [{"source": "fixture", "url": "https://example.com/1", "title": "BTC news", "body": "data"}]


class Repo:
    def __init__(self):
        self.last = None
        self.rows = []
        self.seen = False
        self.news = NEWS

    async def claim(self, now, interval, limit):
        if (
            self.seen
            or not self.news
            or self.last
            and now - self.last < timedelta(seconds=interval)
        ):
            return None
        self.last = now
        return self.news

    async def save(self, scores, news, now, model):
        self.rows = [{"symbol": s, "ts": now, **v.model_dump()} for s, v in scores.items()]
        self.seen = True

    async def latest(self, now):
        return self.rows


def make(raw=None, **options):
    repo = Repo()
    client = AsyncMock()
    client.complete.return_value = raw or json.dumps(
        {SYMBOL: {"score": 0.8, "confidence": 0.5, "summary": "positive"}}
    )
    return Sentiment(repo, client, [SYMBOL], **options), repo, client


async def test_valid_batch_cached_signal_and_no_hot_path_io():
    agent, repo, client = make()
    assert await agent.refresh(NOW) == 1
    repo.latest = AsyncMock(side_effect=AssertionError("hot path DB access"))
    result = await agent.analyze(SYMBOL, MarketState(ts=NOW))
    assert result.direction == 1 and result.confidence == pytest.approx(0.4)
    assert result.ts == NOW
    assert client.complete.await_count == 1
    repo.latest.assert_not_awaited()
    assert client.complete.call_args.kwargs["schema"]["additionalProperties"] is False


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "{}",
        '{"ETH/USDT":{}}',
        json.dumps({SYMBOL: {"score": 2, "confidence": 0.5, "summary": "bad"}}),
        json.dumps({SYMBOL: {"score": 0.1, "confidence": -1, "summary": "bad"}}),
        json.dumps({SYMBOL: {"score": "0.5", "confidence": 0.5, "summary": "bad"}}),
        json.dumps({SYMBOL: {"score": 0.1, "confidence": 0.5, "summary": "bad", "extra": 1}}),
    ],
)
async def test_invalid_response_emits_nothing(raw):
    agent, repo, client = make(raw)
    assert await agent.refresh(NOW) == 0
    assert not repo.seen
    assert await agent.analyze(SYMBOL, MarketState(ts=NOW)) is None
    await agent.refresh(NOW + timedelta(seconds=299))
    assert client.complete.await_count == 1


async def test_error_consumes_quota_across_worker_restart():
    agent, repo, client = make()
    client.complete.side_effect = RuntimeError("fixture")
    await agent.refresh(NOW)
    restarted = Sentiment(repo, client, [SYMBOL])
    await restarted.refresh(NOW + timedelta(seconds=299))
    assert client.complete.await_count == 1
    await restarted.refresh(NOW + timedelta(seconds=300))
    assert client.complete.await_count == 2


async def test_concurrent_refresh_only_calls_once_and_seen_news_not_reused():
    agent, repo, client = make()
    await asyncio.gather(agent.refresh(NOW), agent.refresh(NOW))
    await agent.refresh(NOW + timedelta(seconds=301))
    assert client.complete.await_count == 1


async def test_empty_news_never_calls_llm():
    agent, repo, client = make()
    repo.news = []
    await agent.refresh(NOW)
    client.complete.assert_not_awaited()


async def test_ttl_future_time_and_missing_symbol():
    agent, _, _ = make()
    await agent.refresh(NOW)
    for stamp in [NOW - timedelta(seconds=1), NOW + timedelta(seconds=600)]:
        assert await agent.analyze(SYMBOL, MarketState(ts=stamp)) is None
    assert await agent.analyze("ETH/USDT", MarketState(ts=NOW)) is None


async def test_timeout_does_not_propagate():
    agent, repo, client = make(timeout_s=0.01)

    async def slow(**kwargs):
        await asyncio.Event().wait()

    client.complete.side_effect = slow
    assert await agent.refresh(NOW) == 0
    assert not repo.seen


async def test_error_clears_previous_cache():
    agent, repo, client = make()
    await agent.refresh(NOW)
    repo.seen = False
    client.complete.side_effect = RuntimeError("offline")
    await agent.refresh(NOW + timedelta(seconds=300))
    assert await agent.analyze(SYMBOL, MarketState(ts=NOW + timedelta(seconds=300))) is None


async def test_mock_persists_neutral_scores():
    repo = Repo()
    agent = Sentiment(repo, MockLLM(), [SYMBOL, "ETH/USDT", "SOL/USDT"])
    await agent.refresh(NOW)
    assert len(repo.rows) == 3
    assert all(r["score"] == 0 and r["confidence"] == 0 for r in repo.rows)


@pytest.mark.parametrize("options", [{"interval_s": 299}, {"timeout_s": 301}, {"ttl_s": 0}])
def test_invalid_limits(options):
    with pytest.raises(ValueError):
        make(**options)
