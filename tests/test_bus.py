import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from swarm.bus import DECISIONS, FILLS, PROPOSALS, SIGNALS, Bus, Channel
from swarm.models import Fill, Proposal, RiskDecision, Signal
from tests.test_models import TS, signal


class FakePubsub:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.unsubscribed = False
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def subscribe(self, channel):
        await self.queue.put({"type": "subscribe", "data": 1})

    async def unsubscribe(self, channel):
        self.unsubscribed = True

    async def listen(self):
        while True:
            yield await self.queue.get()


class FakeRedis:
    def __init__(self):
        self.subscription = FakePubsub()

    def pubsub(self):
        return self.subscription

    async def publish(self, channel, data):
        await self.subscription.queue.put({"type": "message", "data": data.encode()})
        return 1


def examples():
    s = signal()
    p = Proposal(symbol=s.symbol, ts=TS, direction=1, score=0.8, size_quote=100, signals=[s])
    return [
        (SIGNALS, s),
        (PROPOSALS, p),
        (DECISIONS, RiskDecision(proposal=p, verdict="REJECT", size_quote=0, reason="halt")),
        (
            FILLS,
            Fill(
                fill_id="f",
                client_order_id="o",
                symbol=s.symbol,
                ts=TS,
                side="buy",
                quantity=1,
                price=10,
                fee=0,
                fee_currency="USDT",
            ),
        ),
    ]


@pytest.mark.parametrize("channel,message", examples())
async def test_roundtrip_and_cleanup(channel, message):
    client = FakeRedis()
    bus = Bus(client=client)
    async with bus.subscribe(channel) as messages:
        assert await bus.publish(channel, message) == 1
        assert await asyncio.wait_for(anext(messages), 1) == message
    assert client.subscription.unsubscribed and client.subscription.closed


async def test_wrong_message_and_channel():
    bus = Bus(client=FakeRedis())
    with pytest.raises(TypeError):
        await bus.publish(FILLS, signal())
    with pytest.raises(ValueError):
        await bus.publish(Channel("unknown", Signal), signal())


async def test_malformed_message_cleans_up():
    client = FakeRedis()
    with pytest.raises(ValidationError):
        async with Bus(client=client).subscribe(SIGNALS) as messages:
            await client.publish("signals", "{}")
            await anext(messages)
    assert client.subscription.closed


async def test_owned_client_closed():
    bus = Bus()
    bus.client = AsyncMock()
    await bus.aclose()
    bus.client.aclose.assert_awaited_once()
