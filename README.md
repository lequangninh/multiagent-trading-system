# swarm-trader

A paper-trading research system. Several analyzer agents (volume/volatility scanner,
VWAP fair value, order-book liquidity, EMA momentum, and one LLM sentiment agent) vote
on each symbol. A weighted consensus turns agreement into a proposal, a deterministic
risk gate with veto power sizes or rejects it, and an order manager executes on the
**Binance Spot Testnet only**. Everything on the trade path is deterministic code with
unit tests; the LLM is used in exactly one place and never on the hot path.

## What this does not do

- It **never trades real funds**. Every exchange URL is allow-listed to the Binance Spot
  Testnet; a mainnet URL fails startup and the HTTP layer blocks non-testnet hosts.
- It has **no proven edge**. The agents are textbook signals. The backtester exists to
  show, with fees included, whether a configuration loses money more slowly than
  others. The default configuration cannot trade at all without a real sentiment
  provider, because three directional agents with equal weights cap consensus below
  the threshold. That is deliberate.
- It does not manage money, keys or risk for anyone. Position, exposure, drawdown and
  rate limits protect a testnet balance and nothing else.
- Sentiment ships with a mock LLM only. No provider is wired.

## Requirements

Python 3.12, [uv](https://docs.astral.sh/uv/), Docker with compose. macOS or Linux.

## Setup

```bash
uv sync
cp .env.example .env        # then edit: at minimum set POSTGRES_PASSWORD
docker compose up -d        # TimescaleDB, Redis, Grafana (all bound to 127.0.0.1)
uv run python -m swarm.main --check
```

Secrets come only from the environment. `.env` is git-ignored and is read by both
docker compose and the app, so the database password has a single source. Startup
refuses placeholder values such as `changeme`, so a copied example can never run as-is.

## Collect market data

```bash
uv run python -m swarm.data     # websocket 1m candles + top-20 books, RSS news every 5 min
```

Leave it running, ideally under `tmux`. The backtester and the paper runner can only
trade on minutes that have a fresh book snapshot, and Binance has no historical book
endpoint, so every hour of collection is history you cannot get any other way.
Backfill candles alone with `uv run python -m swarm.data.backfill --symbol BTC/USDT --days 30`.

## Run paper mode

Put testnet keys in `.env` (`BINANCE_TESTNET_API_KEY`, `BINANCE_TESTNET_API_SECRET`,
from testnet.binance.vision) or export them in the shell. Then:

```bash
uv run python -m swarm.main --mode paper --duration 600
uv run python -m swarm.main --mode paper --duration 600 --smoke-order   # force one 20 USDT order
```

Each cycle reconciles local state with the exchange, manages exits (take-profit
+1.5x cost, stop-loss -1x, 4 h time-stop), evaluates agents, consensus and the risk
gate, and consumes decisions from Redis. Losing Redis or the exchange degrades to "no
new orders" and recovers on its own. A file named `HALT` in the repo root rejects
every order until removed. See `M6_SETUP.md` for details and expected log lines.

## Backtest

```bash
uv run python -m swarm.backtest --symbol BTC/USDT --days 30
uv run python -m swarm.backtest --symbol BTC/USDT --days 120 --train 60d --test 30d --step 30d
```

Replays stored candles and books through the same agent, consensus and risk code
against a simulated exchange (0.1% taker fee, half of each level executable, partial
fills). Writes `equity_curve.csv`, `trades.csv` and `metrics.json` under `backtests/`.
Walk-forward tunes threshold and weight presets on train windows only. If fees exceed
gross return the run prints `STRATEGY DEAD: fees exceed gross alpha`.

## Dashboard, alerts and reports

Grafana is provisioned automatically at http://localhost:3000 (anonymous viewer, no
login): equity curve, daily drawdown, feed lag, open positions, per-agent hit rate,
risk rejections by reason, book throughput, LLM cost and alerts.

Alerts (feed stale > 60 s, daily drawdown > 2%, more than 5 agent exceptions per
minute, reconcile discrepancy) are logged, stored as events and optionally POSTed to
`SWARM_ALERT_WEBHOOK`. The paper runner starts the monitor; run it alone with
`make monitor` during collection-only periods.

`make report` writes a markdown summary of the last 7 days to `reports/`.

## Tests

```bash
make test        # uv run pytest -q, including tests/chaos and hypothesis properties
make lint        # ruff
```

## Layout

`swarm/agents` analyzers, `swarm/consensus` scoring, `swarm/risk` the gate,
`swarm/execution` OMS and paper runner, `swarm/backtest` replay engine,
`swarm/data` feed, backfill, store and schema, `swarm/monitor.py` alerts,
`swarm/report.py`, `swarm/telemetry.py`, `dashboards/` Grafana provisioning,
`NOTES.md` per-milestone decisions and known limitations.
