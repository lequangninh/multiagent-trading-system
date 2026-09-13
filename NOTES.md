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

# M2 review correction

Earlier passing tests used fake database transports and did not establish M2's
acceptance gate. No 40,000-row result has been observed in this workspace.
RSS URL availability and live websocket behavior also remain unverified.
Do not infer that a short history is caused by a reset without checking the data.

Review fixes: configuration validation before backfill resources, spot-only
market discovery, closed-minute backfill bounds, all websocket batch rows
persisted, complete resampling buckets only, malformed RSS dates isolated,
and `python -m swarm.data` for supervised feed/news ingestion with cleanup.
Regression suite: 45 tests passed. Docker/database acceptance must run on the Mac.
