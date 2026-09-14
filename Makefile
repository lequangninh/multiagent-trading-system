.PHONY: report test lint backtest monitor

SYMBOL ?= BTC/USDT
DAYS ?= 30

report:
	uv run python -m swarm.report --days 7

test:
	uv run pytest -q

lint:
	uv run ruff check . && uv run ruff format --check swarm tests

backtest:
	uv run python -m swarm.backtest --symbol $(SYMBOL) --days $(DAYS)

monitor:
	uv run python -m swarm.monitor
