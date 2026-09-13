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
