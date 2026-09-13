# M1 scope

Only infrastructure, contracts, message bus and configuration guard are implemented.
M2–M9 remain unimplemented, per the one-milestone-per-turn rule.
Timestamps are timezone-aware; sizes and prices use base/quote units explicitly.
Redis pub/sub is ephemeral; durable recovery is future execution work.
PostgreSQL password default is for local development only; services bind to loopback.
No credentials or exchange calls are needed for M1.
Docker is unavailable in the build environment; Compose services require external verification.

Future ambiguity to resolve at M6: authenticated testnet order placement requires
testnet credentials, while the spec says no real API keys will be provided.
No keys were requested, supplied, or used in M1.

# M2 decisions

Only canonical 1m candles are stored. The read API resamples them to 5m/15m
with TimescaleDB `time_bucket`, avoiding stale continuous aggregates after a
late backfill. Market websockets and REST backfills explicitly enable ccxt's
Binance sandbox mode. RSS ingestion is deterministic and does not call an LLM.

Binance Spot Testnet resets periodically. Therefore its available 1m history
may be shorter than 30 days and the specification's fixed 40,000-row acceptance
count may occasionally be impossible while still obeying its testnet-only rule.
