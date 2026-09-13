"""Single-writer durable state plus fill outbox. Session lock spans the runner."""

import json
from datetime import datetime

from swarm.models import Fill


class Repository:
    def __init__(self, connection):
        self.connection = connection

    async def load(self):
        raw = await self.connection.fetchval("SELECT payload FROM oms_state WHERE id=1")
        return json.loads(raw) if isinstance(raw, str) else raw

    async def save(self, state, fills=()):
        async with self.connection.transaction():
            await self.connection.execute(
                """INSERT INTO oms_state VALUES(1,$1::jsonb)
                ON CONFLICT(id) DO UPDATE SET payload=excluded.payload""",
                json.dumps(state),
            )
            for cid, order in state["orders"].items():
                await self.connection.execute(
                    """INSERT INTO orders VALUES($1,$2,$3,$4::jsonb)
                    ON CONFLICT(client_order_id) DO UPDATE SET payload=excluded.payload""",
                    cid,
                    order["symbol"],
                    datetime.fromisoformat(order["ts"]),
                    json.dumps(order),
                )
            for symbol, quantity in state["balances"].items():
                await self.connection.execute(
                    """INSERT INTO positions VALUES($1,$2,$3)
                    ON CONFLICT(symbol) DO UPDATE SET quantity=excluded.quantity,ts=excluded.ts""",
                    symbol,
                    quantity,
                    datetime.fromisoformat(state["ts"]),
                )
            for fill in fills:
                await self.connection.execute(
                    """INSERT INTO fills(fill_id,payload)
                    VALUES($1,$2::jsonb) ON CONFLICT DO NOTHING""",
                    fill.fill_id,
                    fill.model_dump_json(),
                )

    async def publish_fills(self, bus):
        from swarm.bus import FILLS

        rows = await self.connection.fetch("SELECT fill_id,payload FROM fills WHERE NOT published")
        for row in rows:
            raw = row["payload"]
            fill = (
                Fill.model_validate_json(raw) if isinstance(raw, str) else Fill.model_validate(raw)
            )
            await bus.publish(FILLS, fill)
            await self.connection.execute(
                "UPDATE fills SET published=true WHERE fill_id=$1", row["fill_id"]
            )
