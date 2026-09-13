"""Typed Redis pub/sub. Ephemeral delivery; no replay or delivery guarantee."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Generic, TypeVar

from redis.asyncio import Redis

from swarm.models import Fill, Model, Proposal, RiskDecision, Signal

T = TypeVar("T", bound=Model)


@dataclass(frozen=True)
class Channel(Generic[T]):
    name: str
    model: type[T]


SIGNALS = Channel("signals", Signal)
PROPOSALS = Channel("proposals", Proposal)
DECISIONS = Channel("decisions", RiskDecision)
FILLS = Channel("fills", Fill)
_CHANNELS = {c.name: c for c in (SIGNALS, PROPOSALS, DECISIONS, FILLS)}


class Bus:
    def __init__(self, url: str = "redis://localhost:6379/0", *, client=None):
        self.client = client if client is not None else Redis.from_url(url)
        self._owns_client = client is None

    @staticmethod
    def _validate(channel):
        if _CHANNELS.get(channel.name) != channel:
            raise ValueError("unknown channel or incorrect model")

    async def publish(self, channel: Channel[T], message: T) -> int:
        self._validate(channel)
        if not isinstance(message, channel.model):
            raise TypeError(f"{channel.name} requires {channel.model.__name__}")
        return await self.client.publish(channel.name, message.model_dump_json())

    @asynccontextmanager
    async def subscribe(self, channel: Channel[T]) -> AsyncIterator[AsyncIterator[T]]:
        self._validate(channel)
        async with self.client.pubsub() as pubsub:
            await pubsub.subscribe(channel.name)
            # Wait for acknowledgement so callers may publish immediately on entry.
            async for event in pubsub.listen():
                if event["type"] == "subscribe":
                    break

            async def messages():
                async for event in pubsub.listen():
                    if event["type"] == "message":
                        yield channel.model.model_validate_json(event["data"])

            try:
                yield messages()
            finally:
                await pubsub.unsubscribe(channel.name)

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()
