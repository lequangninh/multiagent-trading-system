# M6: Spot Testnet execution

Only Binance Spot Testnet credentials are accepted by design. Configure them on
your Mac, not in chat or source code. Do not use live exchange keys. Environment
variables are read directly; .env files are not loaded automatically.

## Configure credentials in macOS zsh

These prompts hide the input and keep the values out of shell command history:

```zsh
read -rs "BINANCE_TESTNET_API_KEY?Testnet API key: "
read -rs "BINANCE_TESTNET_API_SECRET?Testnet API secret: "
export BINANCE_TESTNET_API_KEY BINANCE_TESTNET_API_SECRET
```

Use one testnet account with this database. Reusing the database for another
account is unsupported: the persisted order IDs and equity baseline belong to
the original account. Only one runner may own the database at a time. Avoid
manual or other-system orders in the same testnet account during the check.
The valued portfolio includes the configured BTC/ETH/SOL universe and USDT;
other testnet assets do not contribute to risk equity.

## Verify locally

```bash
uv run pytest tests/test_execution.py -q
uv run pytest -q
uv run ruff check .
docker compose up -d
uv run python -m swarm.main --mode paper --duration 600
```

The runner applies migrations, starts the public market feed, consumes Redis
`decisions`, evaluates the deterministic agents/consensus, and reconciles once
per cycle followed by at most 60 seconds' sleep. RPC/retry latency can extend
that interval. Real cached sentiment may be used; mock sentiment is excluded.
With no real sentiment, default directional weights cap consensus at 2/3, below
0.70. No threshold is weakened and no automatic order is forced.

To explicitly exercise authenticated order placement despite absent consensus,
use this separate testnet smoke run:

```bash
uv run python -m swarm.main --mode paper --duration 600 --smoke-order
```

This proposes one 20 USDT limit order. It sells existing BTC if sufficient free
BTC is present, otherwise proposes a buy. It bypasses signal generation only;
OMS performs the same authoritative risk check, HALT check, exposure reservation,
precision, minimum-notional and available-funds checks. It may still be vetoed.
Smoke orders are test fixtures, not strategy performance evidence.

Inspect the latest persisted orders and fills:

```bash
docker compose exec timescaledb psql -U swarm -d swarm -c "SELECT client_order_id, symbol, payload->>'status' AS status FROM orders ORDER BY ts DESC LIMIT 10;"
docker compose exec timescaledb psql -U swarm -d swarm -c "SELECT count(*) AS fills FROM fills;"
```

An accepted order does not guarantee a fill. Expected final log:
`paper_complete remaining_discrepancies=0`, and at least one order with an
exchange ID (not merely an intent or rejected order). M6's actual 10-minute
acceptance check remains pending until these results are observed locally.

## Behavior and limitations

Order IDs are deterministic hashes of proposals. Durable intents are committed
before requests. Timeouts/5xx become unknown outcomes; all new orders pause until
reconciliation resolves them. Even an OrderNotFound response after an ambiguous
submission does not trigger a blind retry. Unresolved IDs or external open orders
keep the system paused. Safe reads retry transient network errors up to 3 times.

Reconciliation fetches orders by client ID, deduplicates exchange trade IDs,
corrects holdings from exchange balances, saves fills in PostgreSQL and publishes
through a durable outbox. Redis pub/sub does not guarantee subscriber delivery;
a publish/ack crash can produce duplicates, so consumers must deduplicate fill IDs.
Per-order trade reads are capped at 1000; incomplete trade histories pause trading.

Bought fills create managed lots. Estimated round-trip cost is 0.2% fees plus
entry limit deviation from mid; take-profit is +1.5x that estimate, stop-loss -1x,
and time-stop is 4 hours. These are local triggers, not exchange-native stops.
Triggered exits are limit sells and can remain unfilled. Every exit obeys risk
vetoes (including the same-symbol cooldown and HALT). Open orders older than 60s
are canceled; exit lots may be repriced on later cycles after reconciliation.

At normal duration expiry resting known orders are canceled and reconciled.
Inventory is retained. Exits stop being monitored when the runner exits; restart
it to resume. An interrupt or network failure can leave testnet orders open;
check the account and restart for reconciliation. No real funds are supported.
