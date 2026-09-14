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

# M6 execution

User authorized testnet-only credentials configured locally on their Mac.
M6_SETUP.md contains credential prompts, normal paper run and explicit smoke run.
Implementation includes guarded HTTP exchange adapter, persistent intents and
risk reservations, restart-safe daily drawdown state, managed bought-fill exits,
reconciliation, SQL fill outbox, Redis decision consumption and bounded runner.
No authenticated requests were made here; missing environment credentials fail
startup. The required 600-second exchange-order acceptance remains pending on Mac.
Existing default consensus plus neutral/missing sentiment cannot exceed 2/3;
--smoke-order explicitly tests execution through risk without changing weights.
Unit tests use mocks. PostgreSQL locking/outbox and authenticated exchange
integration require the user's local Docker/testnet verification.

# M6 reconciliation startup fix

The original account-wide fetch_open_orders() encountered CCXT's default
warnWithoutSymbol exception. Explicitly acknowledge the account-wide request's
higher weight while retaining CCXT rate limiting. Regression test runs real CCXT
fetch_open_orders dispatch with only the final endpoint mocked. Error logs now
include operation stage, numeric exchange code if present, and allowlisted hints;
raw request/response text and credentials are never logged. Initial failed
reconciliation stops the runner before issuing a smoke-order request.
Verification: 17 execution tests and 124 full-suite tests passed; Ruff clean.
The observed generic ExchangeError is consistent with this defect; if another
exchange error remains, the new diagnostic fields identify it safely.

# M7 backtester

`uv run python -m swarm.backtest --symbol BTC/USDT --days 30` replays every stored
1m candle close as a decision point through the unchanged Scanner, FairValue,
Liquidity and Momentum classes, `consensus.engine.propose`, and `risk.gate.decide`.
Sentiment is replayed point-in-time from real (non-mock) `sentiment_scores` rows.
Two helpers were extracted so live and replay share one definition rather than a
copy: `risk.gate.slippage_curve` (the OMS's depth/cost curve) and
`execution.oms.exit_reason` (take-profit +1.5x cost, stop-loss -1x, 4h time-stop).
Existing OMS tests cover both after the extraction.

Fills come from `backtest.sim.SimExchange`: taker fee 0.1% in quote, at most 50% of
each displayed level is executable, so slippage is the average price versus mid
from walking real depth, and orders that exhaust the 20 stored levels fill
partially. Fills below a 5 USDT notional are skipped like the OMS minimum check.
Managed exits sell the exact lot quantity so no dust accumulates. Exits pass
through the same risk gate as entries (cooldown and rate limits apply, as in the
OMS). The HALT file is deliberately ignored in replay; `decide` is called with
`halted=False`. Votes are not written to the live `votes` table.

Books are loaded as the last snapshot per minute (`Store.get_books`), which is all
a minute-cadence replay can observe. Liquidity refuses books older than 60s and
consensus requires a liquidity signal, so minutes without a fresh book cannot
trade; the CLI prints the covered fraction. Marks use fresh-book mid, else the 1m
close. Resampling to 5m/15m happens in memory with the same complete-bucket rule
as `Store.get_candles`.

Metrics: Sharpe is annualised from hourly equity returns (0 when undefined);
`profit_factor`/`win_rate` are null with no losing/closing trades. Extra keys:
`round_trips`, `gross_return_pct`, `net_return_pct`, `strategy_dead`, and
`counters` (signals, proposals, per-rule verdicts, unfilled). The guard
`fee_drag_pct > gross_return_pct` sets `strategy_dead` and prints the banner.

Walk-forward (`--train 60d --test 30d --step 30d`) tunes over `default_grid()`
(thresholds 0.5-0.9 x four directional presets) by net return on each train
window only, then evaluates the following test window; test windows are
compounded into one out-of-sample curve and per-window choices are stored under
`walk_forward` in metrics.json. Agent signals are cached per (symbol, minute,
base_size), so the grid costs one signal pass. `base_size` is not tuned.

Verification: 137 tests pass (13 new); Ruff clean. The acceptance command wrote
all three outputs. The local database holds 4.3 days of BTC candles but only 30
minutes with book snapshots (the feed ran briefly), so the real-data run has zero
trades and the guard is not applicable. Test synthetics: a sawtooth uptrend with
the momentum preset gives 12 round trips, all take-profits, net positive; seeded
driftless random walks with the fairvalue preset give >=10 trades, net negative,
fees exceeding gross. Default settings (all weights 1, threshold 0.70) cannot trade
without real sentiment, as noted in M6.

Ideas, not implemented: synthetic-book fallback when no snapshot exists (would
fabricate liquidity), tuning `base_size`, multi-symbol CLI runs (engine supports
it), 5m decision cadence to speed long replays.

# M8 observability

Four things the panels need were only logged before, never stored. New tables in
schema.sql: `equity_snapshots` (OMS writes marked equity every reconcile),
`risk_decisions` (every authoritative OMS-side gate verdict with its reason),
`llm_calls` (per sentiment batch: article count, chars in/out, ok flag, estimated
cost) and `events` (agent_exception, reconcile_discrepancy, alert). Writes go
through `swarm.telemetry.Telemetry`, which swallows and logs its own failures so a
database hiccup cannot alter the trade path; `NullTelemetry` is the default, so
existing OMS test doubles are untouched. Paper `cycle` now isolates a failing
agent (it abstains, others still vote) and records the exception as an event.

LLM cost is an estimate: tokens ~ chars/4 times list prices in
config/sentiment.yaml (`usd_per_1m_*_tokens`, default 0). Mock calls cost 0.
No real provider is connected, so the panel only shows mock rows until M4's
provider is wired with the user's keys.

Grafana: `dashboards/` is mounted as the provisioning root. `datasources/`
declares the TimescaleDB source (uid `timescale`, password from
`POSTGRES_PASSWORD`, same default as the DB service); `dashboards/provider.yaml`
loads `dashboards/json/swarm-trader.json` (11 panels, generated once, committed as
plain JSON). Anonymous Viewer access is enabled on the loopback-bound port so the
dashboard renders without login; admin/admin remains Grafana's default.
Per-agent hit rate joins each directional vote with the 1m close 30 minutes later;
filters (direction 0) are excluded, so scanner/liquidity never appear there.

Alerts (`swarm/monitor.py`): gather -> pure `evaluate` -> notify. Feed stale
>60s per symbol (or no snapshots), UTC-day drawdown >2% from equity_snapshots,
>5 agent exceptions in the last minute, and each reconcile_discrepancy event
once. Each alert logs at WARNING, writes an `events` row (shown as dashboard
annotations) and POSTs JSON to `alerts.webhook_url` or `SWARM_ALERT_WEBHOOK` if
set; webhook failures are logged only. Per-key cooldown 600s. The paper runner
starts the monitor; `python -m swarm.monitor` (or `--once`) runs it standalone
for feed-only periods. Running both duplicates alerts; documented, not prevented.

`make report` writes `reports/paper_<date>.md` for the last 7 days: equity and
drawdown, orders/fills/fees, positions, per-agent hit rate, risk verdicts by
reason, feed coverage, events, LLM usage. Makefile also has test, lint,
backtest and monitor targets.

Verification: 151 tests pass (14 new); Ruff clean. Grafana 13.2.1 reports
datasource "Database Connection OK", the dashboard is provisioned and readable
anonymously, and every panel query plus the annotation query executes through
`/api/ds/query` without error against the live database. equity_snapshots and
risk_decisions are empty until the next paper run, so those panels show "No
data" rather than paper-trading history; the M6 10-minute testnet run remains
the user's pending gate and will populate them.
