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

# M3 calculation conventions

MarketState carries observation time, per-timeframe candles, latest book, news,
and proposed quote size. Agents exclude unfinished candles and reject gapped or
duplicate series. Signals use configuration-compatible agent names, observation
 time, and TTL 300 seconds. No network, model calls, random sources, or wall clock.

TIDAL compares latest 5m volume against the preceding 288 bars. Volume confidence
is min(ratio/4,1) above a strict 2x threshold. Realised 15m volatility is RMS of
three 5m log returns, compared with the prior rolling values' 90th percentile;
confidence is min(current/(2*percentile),1), or 1 for a positive move from zero.
NORO uses 96 typical prices weighted by volume for VWAP and population standard
 deviation of 96 closes for the z-score. Zero total volume yields no signal.
ZEPHR sorts book levels, reports quote depth within 0.5% of mid, walks both sides
for size_quote/mid base units, and uses the worse mid-relative cost (including
half-spread). Insufficient depth gives zero confidence. Books older than 60s or
future-dated yield no signal. Crossed/empty books give zero confidence.
Momentum emits only fresh EMA12/48 crossovers with Wilder ADX14 >=25; EMA seeds
at first close, ADX uses Wilder smoothing. It needs >=49 closed 5m candles.
These are research definitions, not evidence of profitable trading.

M2 gate amendment authorized by user: replace fixed >=40,000 candles with all
available testnet history and no missing minutes in the returned range. User
verified 6,231/6,231 minutes, books for all symbols, and two working RSS sources.

# M4 sentiment

Run `uv run python -m swarm.agents.sentiment --mock-llm` after ingesting RSS news.
Settings are in config/sentiment.yaml to preserve local settings.yaml/RSS edits.
Schema migration creates sentiment_scores, sentiment_seen and sentiment_batches.
No fake news is inserted: the dry run reads existing news and saves clearly marked
neutral mock scores. Mock and real result namespaces are separate. Inference runs
only in refresh/run; analyze uses an in-memory copy of persisted scores, with no
network/DB/LLM access and no renewed timestamps. Confidence is abs(score) times
model confidence; TTL is 600 seconds from batch observation time.

PostgreSQL advisory transaction locking reserves a persistent >=300-second quota
before a single call for the full universe. Failed requests consume that quota;
unseen rows are retried after cooldown. Scores and processed-news identities are
saved atomically. New worker instances reload persisted cache on refresh.
The client protocol accepts bounded news and an exact-universe JSON schema;
validation rejects missing/extra symbols and invalid fields. Provider adapters
must disable automatic retries. The shipped CLI only enables MockLLM under the
spec's prohibition on real API keys. Real provider connectivity and cheapest-model
status are not verified; gpt-5-nano is a configurable provisional default, not a
claim that it is currently the cheapest available model. This milestone does not
include a connected real provider. No API key was requested or used.

Tests use a repository double and mocked inference. PostgreSQL concurrency and
persistence still require live verification. M4 is not accepted until the user's
mock dry run confirms persisted sentiment_scores rows on the Docker database.

# M5 consensus and risk gate

Consensus requires fresh scanner AND liquidity signals and multiplies their
confidence into the directional weighted score. Missing directional agents
contribute zero against the full configured directional weight denominator;
this prevents missing votes from inflating confidence. TTL expiry is exclusive,
future signals are ignored, and the latest signal per agent wins (first seen for
identical timestamps). Proposals use base_size*abs(score). All submitted votes
are recorded transactionally, including below-threshold decisions. Database
failure prevents evaluate() from returning an executable proposal.

Risk decide() is pure. check() is the filesystem/logging boundary and must be
used by M6 for executable decisions. It snapshots HALT on every call and logs
verdict, reason and approved size. A rejected decision always carries zero size.
PortfolioState must include marked inventory plus pending buy reservations;
M6 must serialize snapshot/decision/reservation and reserve approved exposure
before the next decision. Without that accounting, any snapshot-based risk gate
can race. Sell inventory must exclude quantities already reserved for other sells.
Daily drawdown uses UTC day-start equity. The portfolio controller must latch
any intraday breach in drawdown_breached_at, even if equity subsequently recovers,
and provide a fresh baseline on the next UTC day; a stale baseline fails closed.
No long-lived portfolio controller or execution pipeline is implemented in M5.

Slippage curve entries are cumulative executable quote caps with conservative
cost ceilings in basis points. Costs must be nondecreasing. Missing/invalid depth
fails closed; sizing uses the largest tier within the limit, no extrapolation.
Position and gross limits apply to increasing exposure; spot sells are bounded
by available held inventory. HALT, drawdown and rate/cooldown vetoes apply to all.

M5 gate: uv run pytest tests/test_consensus.py tests/test_risk.py -q
25 tests passed. Tests cover every specified limit, resize boundaries, spot sells,
TTL, absent filters, vote persistence, and repeated reserved-exposure proposals.
PostgreSQL vote persistence uses a mocked connection in unit tests; it has not
been exercised against Docker in this workspace.
