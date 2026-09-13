"""LUMEN. Background inference only; analyze reads a local cache snapshot.

Run one mock batch: uv run python -m swarm.agents.sentiment --mock-llm
No credentials are needed or accepted by the CLI.
"""

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import structlog
import yaml
from pydantic import ConfigDict, Field, create_model

from swarm.agents.base import MarketState
from swarm.data.store import Store
from swarm.main import validate_config
from swarm.models import Model, Signal

log = structlog.get_logger()


class Score(Model):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    score: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(max_length=2000)


class LLMClient(Protocol):
    async def complete(self, *, model: str, news: list[dict], schema: dict) -> str:
        """Return JSON once; implementations must disable automatic retries."""
        ...


class MockLLM:
    async def complete(self, *, model, news, schema):
        # Deliberately neutral, clearly labelled synthetic output.
        return json.dumps(
            {
                s: {
                    "score": 0.0,
                    "confidence": 0.0,
                    "summary": "MOCK: neutral fixture; no inference performed",
                }
                for s in schema["properties"]
            }
        )


class Repository:
    def __init__(self, pool, *, is_mock):
        self.pool, self.is_mock = pool, is_mock

    async def claim(self, now, interval, limit):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Serialize independent workers without holding a lock during inference.
                await conn.execute("SELECT pg_advisory_xact_lock(734041)")
                last = await conn.fetchval(
                    "SELECT last_attempt FROM sentiment_batches WHERE is_mock=$1", self.is_mock
                )
                if last is not None and now - last < timedelta(seconds=interval):
                    return None
                rows = await conn.fetch(
                    """SELECT n.* FROM news n WHERE n.ts <= $1 AND NOT EXISTS (
                    SELECT 1 FROM sentiment_seen s WHERE s.source=n.source AND s.url=n.url
                    AND s.is_mock=$2) ORDER BY n.ts,n.source,n.url LIMIT $3""",
                    now,
                    self.is_mock,
                    limit,
                )
                if not rows:
                    return None
                # Reserve quota before the call. Errors and restarts cannot bypass cooldown.
                await conn.execute(
                    """INSERT INTO sentiment_batches VALUES ($1,$2)
                    ON CONFLICT (is_mock) DO UPDATE SET last_attempt=excluded.last_attempt""",
                    self.is_mock,
                    now,
                )
                return [dict(row) for row in rows]

    async def save(self, scores, news, now, model):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(
                    """INSERT INTO sentiment_scores
                    (symbol,ts,score,confidence,summary,model,is_mock) VALUES ($1,$2,$3,$4,$5,$6,$7)
                    ON CONFLICT (symbol,ts,is_mock) DO NOTHING""",
                    [
                        (symbol, now, s.score, s.confidence, s.summary, model, self.is_mock)
                        for symbol, s in scores.items()
                    ],
                )
                await conn.executemany(
                    """INSERT INTO sentiment_seen VALUES ($1,$2,$3)
                    ON CONFLICT DO NOTHING""",
                    [(n["source"], n["url"], self.is_mock) for n in news],
                )

    async def latest(self, now):
        return await self.pool.fetch(
            """SELECT DISTINCT ON (symbol) * FROM sentiment_scores
            WHERE is_mock=$1 AND ts <= $2 ORDER BY symbol,ts DESC""",
            self.is_mock,
            now,
        )


class Sentiment:
    name = "sentiment"

    def __init__(
        self,
        repository,
        client: LLMClient,
        symbols,
        *,
        model="gpt-5-nano",
        interval_s=300,
        timeout_s=30,
        ttl_s=600,
        batch_limit=50,
    ):
        if interval_s < 300 or not 0 < timeout_s < interval_s or ttl_s <= 0 or batch_limit <= 0:
            raise ValueError("invalid sentiment limits; interval must be >=300 seconds")
        self.repository, self.client = repository, client
        self.model, self.interval_s = model, interval_s
        self.timeout_s, self.ttl_s, self.batch_limit = timeout_s, ttl_s, batch_limit
        self.response = create_model(
            "SentimentBatch",
            __config__=ConfigDict(extra="forbid"),
            **{s: (Score, ...) for s in symbols},
        )
        self.cache = {}
        self.lock = asyncio.Lock()

    async def refresh(self, now):
        # No DB or LLM work in analyze; refresh runs in its own background task.
        async with self.lock:
            try:
                news = await self.repository.claim(now, self.interval_s, self.batch_limit)
                if news:
                    # Treat all article text as untrusted data; no execution/tools supported.
                    payload = [
                        {
                            "source": n["source"],
                            "url": n["url"],
                            "title": n["title"][:500],
                            "body": n["body"][:4000],
                        }
                        for n in news
                    ]
                    async with asyncio.timeout(self.timeout_s):
                        raw = await self.client.complete(
                            model=self.model, news=payload, schema=self.response.model_json_schema()
                        )
                    parsed = self.response.model_validate_json(raw)
                    scores = {s: getattr(parsed, s) for s in self.response.model_fields}
                    await self.repository.save(scores, news, now, self.model)
                rows = await self.repository.latest(now)
                self.cache = {
                    r["symbol"]: (
                        r["ts"],
                        Score(score=r["score"], confidence=r["confidence"], summary=r["summary"]),
                    )
                    for r in rows
                }
                return len(news) if news else 0
            except Exception as exc:
                self.cache = {}
                log.warning("sentiment_error", error_type=type(exc).__name__)
                return 0

    async def analyze(self, symbol: str, state: MarketState):
        entry = self.cache.get(symbol)
        if entry is None:
            return None
        ts, score = entry
        if not 0 <= (state.ts - ts).total_seconds() < self.ttl_s:
            return None
        return Signal(
            agent=self.name,
            symbol=symbol,
            ts=ts,
            direction=1 if score.score > 0 else -1 if score.score < 0 else 0,
            confidence=score.confidence * abs(score.score),
            rationale=score.summary,
            ttl_s=self.ttl_s,
        )

    async def run(self):
        while True:
            await self.refresh(datetime.now(UTC))
            await asyncio.sleep(self.interval_s)


async def dry_run(args):
    config = validate_config(yaml.safe_load(args.config.read_text()))
    options = yaml.safe_load(args.sentiment_config.read_text())
    store = Store(config["database"]["dsn"])
    try:
        await store.connect()
        repository = Repository(store.pool, is_mock=True)
        agent = Sentiment(repository, MockLLM(), config["universe"], **options)
        count = await agent.refresh(datetime.now(UTC))
        rows = await repository.latest(datetime.now(UTC))
        print(f"mock batch: {count} articles; cached score rows: {len(rows)}")
        if not rows:
            raise RuntimeError("no scores written: ingest news first, or wait for cooldown")
    finally:
        await store.close()


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock-llm", action="store_true")
    parser.add_argument("--config", type=Path, default=root / "config/settings.yaml")
    parser.add_argument("--sentiment-config", type=Path, default=root / "config/sentiment.yaml")
    args = parser.parse_args()
    if not args.mock_llm:
        parser.error("only --mock-llm is enabled; real API keys are forbidden by the build spec")
    asyncio.run(dry_run(args))


if __name__ == "__main__":
    main()
