"""Bounded paper runner; never accesses live exchange endpoints."""

import asyncio
from datetime import timedelta

import structlog

from swarm.agents.base import MarketState
from swarm.agents.fairvalue import FairValue
from swarm.agents.liquidity import Liquidity
from swarm.agents.momentum import Momentum
from swarm.agents.scanner import Scanner
from swarm.bus import DECISIONS, PROPOSALS, SIGNALS, Bus
from swarm.consensus.engine import Consensus
from swarm.data.feed import MarketFeed
from swarm.data.store import Store
from swarm.execution.exchange import TestnetStream, create_exchange
from swarm.execution.oms import OMS, now_utc
from swarm.execution.repository import Repository
from swarm.models import Proposal, RiskDecision, Signal
from swarm.risk.gate import Limits

log = structlog.get_logger()


async def cycle(oms, store, consensus, bus):
    for symbol in oms.symbols:
        now = now_utc()
        state = MarketState(
            ts=now,
            size_quote=consensus.base_size,
            candles={
                tf: await store.get_candles(symbol, tf, now - timedelta(hours=26), now)
                for tf in ("5m", "15m")
            },
            book=await store.get_latest_book(symbol),
        )
        signals = []
        for agent in (Scanner(), FairValue(), Liquidity(), Momentum()):
            s = await agent.analyze(symbol, state)
            if s is not None:
                signals.append(s)
                await bus.publish(SIGNALS, s)
        row = await store.pool.fetchrow(
            """SELECT * FROM sentiment_scores
            WHERE symbol=$1 AND NOT is_mock AND ts <= $2 ORDER BY ts DESC LIMIT 1""",
            symbol,
            now,
        )
        if row and (now - row["ts"]).total_seconds() < 600:
            score = row["score"]
            s = Signal(
                agent="sentiment",
                symbol=symbol,
                ts=row["ts"],
                direction=1 if score > 0 else -1 if score < 0 else 0,
                confidence=abs(score) * row["confidence"],
                rationale=row["summary"],
                ttl_s=600,
            )
            signals.append(s)
            await bus.publish(SIGNALS, s)
        proposal = await consensus.evaluate(symbol, now, signals)
        if proposal:
            await bus.publish(PROPOSALS, proposal)
            # OMS rechecks authoritative account/risk state before every submission.
            await bus.publish(
                DECISIONS,
                RiskDecision(
                    proposal=proposal,
                    verdict="ALLOW",
                    size_quote=proposal.size_quote,
                    reason="pending_oms_risk_recheck",
                ),
            )


async def run(config, duration, smoke_order=False):
    if duration <= 0:
        raise ValueError("duration must be positive")
    exchange = create_exchange(config)
    store = Store(config["database"]["dsn"])
    bus = Bus(config["redis"]["url"])
    worker = None
    feed_task = None
    feed = None
    try:
        await store.connect()
        await exchange.load_markets()
        stream = TestnetStream(
            {
                "enableRateLimit": True,
                "options": {
                    "defaultType": "spot",
                    "fetchMarkets": {"types": ["spot"]},
                    "fetchCurrencies": False,
                },
            }
        )
        feed = MarketFeed(config["universe"], store, exchange=stream)
        feed_task = asyncio.create_task(feed.run())
        if any(
            s not in exchange.markets or not exchange.market(s)["spot"] for s in config["universe"]
        ):
            raise ValueError("Universe must contain supported Spot Testnet symbols")
        async with store.pool.acquire() as connection:
            # Session lock prevents two processes reserving the same account independently.
            locked = await connection.fetchval("SELECT pg_try_advisory_lock(734061)")
            if not locked:
                raise RuntimeError("Another paper OMS owns this database/account")
            repo = Repository(connection)
            oms = OMS(exchange, repo, config["universe"], Limits(**config["risk"]))
            try:
                await oms.start()
                if not oms.ready:
                    raise RuntimeError("Initial reconciliation failed; see safe diagnostic log")
                settings = config["consensus"]
                consensus = Consensus(
                    store.pool, settings["weights"], settings["threshold"], settings["base_size"]
                )
                async with bus.subscribe(DECISIONS) as messages:

                    async def consume():
                        async for decision in messages:
                            if decision.proposal.symbol in oms.symbols:
                                await oms.submit(decision)

                    worker = asyncio.create_task(consume())
                    if smoke_order:
                        symbol = oms.symbols[0]
                        base = symbol.split("/")[0]
                        available = oms.balance.get("free", {}).get(base, 0)
                        direction = -1 if available * oms._mid(symbol) >= 20 else 1
                        proposal = Proposal(
                            symbol=symbol,
                            ts=now_utc(),
                            direction=direction,
                            score=direction,
                            size_quote=20,
                            signals=[],
                        )
                        log.info("smoke_order_requested", symbol=symbol, size_quote=20)
                        await bus.publish(
                            DECISIONS,
                            RiskDecision(
                                proposal=proposal,
                                verdict="ALLOW",
                                size_quote=20,
                                reason="explicit_testnet_smoke_request",
                            ),
                        )
                    deadline = asyncio.get_running_loop().time() + duration
                    while asyncio.get_running_loop().time() < deadline:
                        if worker.done():
                            await worker
                            raise RuntimeError("Decision bus disconnected")
                        await oms.reconcile()
                        await oms.exits()
                        try:
                            async with oms.lock:
                                await repo.publish_fills(bus)
                            await cycle(oms, store, consensus, bus)
                        except Exception as exc:
                            oms.ready = False
                            log.warning("paper_cycle_paused", error_type=type(exc).__name__)
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining > 0:
                            await asyncio.sleep(min(60, remaining))
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                    # Cancel resting orders on bounded-run exit. Existing inventory is retained.
                    for local in oms.state["orders"].values():
                        if local["status"] == "open" and local["id"]:
                            try:
                                await exchange.cancel_order(local["id"], local["symbol"])
                            except Exception as exc:
                                log.warning(
                                    "shutdown_cancel_uncertain", error_type=type(exc).__name__
                                )
                    remaining = await oms.reconcile()
                    async with oms.lock:
                        await repo.publish_fills(bus)
                    log.info(
                        "paper_complete",
                        remaining_discrepancies=remaining,
                        orders=len(oms.state["orders"]),
                    )
                    if any(lot["quantity"] > 0 for lot in oms.state["lots"].values()):
                        log.warning(
                            "managed_positions_remain", reason="exit monitoring stops with runner"
                        )
            finally:
                if worker is not None:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                await connection.execute("SELECT pg_advisory_unlock(734061)")
    finally:
        if feed_task is not None:
            feed_task.cancel()
            await asyncio.gather(feed_task, return_exceptions=True)
        if feed is not None:
            await feed.close()
        await asyncio.gather(exchange.close(), bus.aclose(), store.close())
