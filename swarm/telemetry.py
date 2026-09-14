"""Observability writes. Every method swallows its own errors: telemetry must never
alter the trade path, so a failed insert is logged at WARNING and otherwise ignored."""

from datetime import UTC, datetime

import structlog

log = structlog.get_logger()


class NullTelemetry:
    async def equity(self, ts, equity, cash, inventory_value):
        pass

    async def risk_decision(self, decision, source, ts=None):
        pass

    async def event(self, kind, symbol=None, detail="", ts=None):
        pass

    async def llm_call(self, ts, model, is_mock, articles, input_chars, output_chars, ok, cost):
        pass


class Telemetry(NullTelemetry):
    """Accepts anything with an asyncpg-style ``execute`` (connection or pool)."""

    def __init__(self, executor, *, clock=None):
        self.executor = executor
        self.clock = clock or (lambda: datetime.now(UTC))

    async def _write(self, name, query, *args):
        try:
            await self.executor.execute(query, *args)
        except Exception as exc:
            log.warning("telemetry_write_failed", table=name, error_type=type(exc).__name__)

    async def equity(self, ts, equity, cash, inventory_value):
        await self._write(
            "equity_snapshots",
            """INSERT INTO equity_snapshots (ts, equity, cash, inventory_value)
            VALUES ($1,$2,$3,$4) ON CONFLICT (ts) DO UPDATE SET equity=excluded.equity,
            cash=excluded.cash, inventory_value=excluded.inventory_value""",
            ts,
            float(equity),
            float(cash),
            float(inventory_value),
        )

    async def risk_decision(self, decision, source, ts=None):
        await self._write(
            "risk_decisions",
            """INSERT INTO risk_decisions
            (ts, symbol, verdict, reason, size_quote, proposal_score, source)
            VALUES ($1,$2,$3,$4,$5,$6,$7)""",
            ts or self.clock(),
            decision.proposal.symbol,
            decision.verdict,
            decision.reason,
            float(decision.size_quote),
            float(decision.proposal.score),
            source,
        )

    async def event(self, kind, symbol=None, detail="", ts=None):
        await self._write(
            "events",
            "INSERT INTO events (ts, kind, symbol, detail) VALUES ($1,$2,$3,$4)",
            ts or self.clock(),
            kind,
            symbol,
            str(detail)[:2000],
        )

    async def llm_call(self, ts, model, is_mock, articles, input_chars, output_chars, ok, cost):
        await self._write(
            "llm_calls",
            """INSERT INTO llm_calls
            (ts, model, is_mock, articles, input_chars, output_chars, ok, estimated_cost_usd)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
            ts,
            model,
            bool(is_mock),
            int(articles),
            int(input_chars),
            int(output_chars),
            bool(ok),
            float(cost),
        )
