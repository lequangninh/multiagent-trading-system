"""Backtester acceptance: synthetic trend -> positive PnL; random walk -> fee-negative.

Both synthetic series carry a deep 20-level book every minute so the liquidity filter
passes and fills are deterministic. Presets come from the walk-forward grid: the
trend-following preset (momentum) is evaluated on the trend, the mean-reversion preset
(fairvalue) on the random walk. Neither series has an exploitable edge for the
opposite style, so the assertions are about the engine's accounting, not alpha.
"""

import csv
import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from swarm.backtest.__main__ import duration, parse_args
from swarm.backtest.data import Dataset, SymbolData, resample
from swarm.backtest.engine import Backtester, Params, default_grid
from swarm.backtest.metrics import compute_metrics, write_outputs
from swarm.backtest.sim import SimExchange
from swarm.models import BookSnapshot, Candle
from swarm.risk.gate import Limits

T0 = datetime(2026, 1, 1, tzinfo=UTC)
SYMBOL = "BTC/USDT"
MOMENTUM = Params(
    threshold=0.5, weights=dict(scanner=1, liquidity=1, fairvalue=0, momentum=1, sentiment=0)
)
FAIRVALUE = Params(
    threshold=0.5, weights=dict(scanner=1, liquidity=1, fairvalue=1, momentum=0, sentiment=0)
)


def book(close, ts, spread_bps=2.0, depth=50.0, levels=20):
    half = close * spread_bps / 2 / 10000
    return BookSnapshot(
        symbol=SYMBOL,
        ts=ts,
        bids=[(close - half - close * k / 10000, depth) for k in range(levels)],
        asks=[(close + half + close * k / 10000, depth) for k in range(levels)],
    )


def build(closes, volumes, *, with_books=True) -> Dataset:
    candles, books, prev = [], [], closes[0]
    for i, (c, v) in enumerate(zip(closes, volumes)):
        ts = T0 + timedelta(minutes=i)
        candles.append(
            Candle(
                symbol=SYMBOL,
                ts=ts,
                open=prev,
                high=max(prev, c),
                low=min(prev, c),
                close=c,
                volume=float(v),
                timeframe="1m",
            )
        )
        if with_books:
            books.append(book(c, ts + timedelta(minutes=1)))
        prev = c
    return Dataset({SYMBOL: SymbolData.build(SYMBOL, candles, books)})


def zigzag(days, up_min=240, down_min=120, up_bp=1.0, down_bp=1.5):
    """Net-up sawtooth. Volume spikes cover the minutes where the EMA12/48 up-cross lands."""
    n, closes, vols, p, i = days * 1440, [], [], 100.0, 0
    while i < n:
        for k in range(up_min):
            if i >= n:
                break
            p *= 1 + up_bp / 10000
            closes.append(p)
            vols.append(50.0 if 70 <= k < 110 else 10.0)
            i += 1
        for _ in range(down_min):
            if i >= n:
                break
            p *= 1 - down_bp / 10000
            closes.append(p)
            vols.append(10.0)
            i += 1
    return np.array(closes), np.array(vols)


def random_walk(days, seed, sigma_bp=5.0, spike_p=0.2, spike=6.0):
    """Driftless log random walk; volume spikes last a whole 5m bar so the scanner sees them."""
    rng = np.random.default_rng(seed)
    n = days * 1440
    closes = 100 * np.exp(np.cumsum(rng.normal(0, sigma_bp / 10000, n)))
    blocks = np.where(rng.random(n // 5) < spike_p, spike, 1.0)
    vols = 10 * np.repeat(blocks, 5) * np.exp(rng.normal(0, 0.3, n))
    return closes, vols


def span(dataset):
    first, last = dataset.span()
    return first, last


@pytest.fixture(scope="module")
def trend():
    return build(*zigzag(4))


# --- SimExchange ---------------------------------------------------------------


def test_sim_walks_book_with_half_level_cap_and_fee():
    ex = SimExchange(10_000)
    snapshot = BookSnapshot(
        symbol=SYMBOL, ts=T0, bids=[(99.0, 1.0)], asks=[(100.0, 1.0), (101.0, 1.0), (102.0, 1.0)]
    )
    fill = ex.execute(SYMBOL, "buy", 100.0, snapshot, T0)
    # 0.5 @100 fills half the first level, the remaining 50 quote walks to 101.
    assert fill.quantity == pytest.approx(0.5 + 50 / 101)
    assert fill.price > 100 and fill.slippage_bps > 0 and not fill.partial
    assert fill.fee == pytest.approx(100 * 0.001)
    assert ex.cash == pytest.approx(10_000 - 100 - 0.1)
    assert ex.inventory[SYMBOL] == pytest.approx(fill.quantity)


def test_sim_partial_fill_when_book_exhausted_and_sell_bounded_by_inventory():
    ex = SimExchange(10_000)
    snapshot = BookSnapshot(symbol=SYMBOL, ts=T0, bids=[(99.0, 0.12)], asks=[(100.0, 0.2)])
    fill = ex.execute(SYMBOL, "buy", 100.0, snapshot, T0)
    assert fill.partial and fill.quantity == pytest.approx(0.1) and fill.quote == pytest.approx(10)
    sell = ex.execute(SYMBOL, "sell", 1_000.0, snapshot, T0)
    assert sell.partial and sell.quantity == pytest.approx(0.06)  # half of the only bid level
    assert ex.inventory[SYMBOL] == pytest.approx(0.04)
    assert ex.execute(SYMBOL, "sell", 1.0, snapshot, T0) is None  # below min notional
    assert ex.execute(SYMBOL, "sell", 100.0, snapshot, T0, quantity=0.04) is None  # 3.96 quote
    assert ex.execute(SYMBOL, "buy", 100.0, snapshot.model_copy(update={"asks": []}), T0) is None


# --- Data ----------------------------------------------------------------------


def test_resample_requires_complete_buckets():
    closes = [100 + i for i in range(30)]
    dataset = build(closes, [1] * 30)
    assert len(dataset[SYMBOL].candles["5m"]) == 6 and len(dataset[SYMBOL].candles["15m"]) == 2
    rows = [c for c in dataset[SYMBOL].candles["1m"] if c.ts != T0 + timedelta(minutes=7)]
    assert [c.ts for c in resample(rows, 5)] == [
        T0 + timedelta(minutes=m) for m in (0, 10, 15, 20, 25)
    ]
    five = resample(rows, 5)[0]
    assert (five.open, five.close, five.high, five.volume) == (100, 104, 104, 5)
    with pytest.raises(ValueError):
        resample(resample(rows, 5), 3)


# --- Replay properties ---------------------------------------------------------


async def test_trending_series_yields_positive_pnl(trend):
    result = await Backtester(trend).run(MOMENTUM, *span(trend))
    m = result.metrics
    assert m["trades"] >= 10 and m["round_trips"] == m["trades"] // 2
    assert m["win_rate"] == 1.0 and m["net_return_pct"] > 0
    assert m["gross_return_pct"] > m["fee_drag_pct"] > 0 and not m["strategy_dead"]
    reasons = [t["reason"] for t in result.trades]
    assert reasons[::2] == ["signal"] * len(reasons[::2])
    assert set(reasons[1::2]) == {"take_profit"}
    assert result.equity[-1]["inventory_value"] == 0
    assert result.trades == (await Backtester(trend).run(MOMENTUM, *span(trend))).trades


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_random_walk_is_fee_negative(seed):
    dataset = build(*random_walk(4, seed))
    m = (await Backtester(dataset).run(FAIRVALUE, *span(dataset))).metrics
    assert m["trades"] >= 10
    assert m["net_return_pct"] < 0
    assert m["fee_drag_pct"] > m["gross_return_pct"] and m["strategy_dead"]


async def test_no_fresh_book_means_no_trades(trend):
    dataset = build(*zigzag(2), with_books=False)
    result = await Backtester(dataset).run(MOMENTUM, *span(dataset))
    assert result.metrics["trades"] == 0 and result.counters["no_fresh_book"] > 0
    assert result.counters["proposals"] == 0  # liquidity filter absent -> consensus abstains


async def test_risk_gate_resizes_and_vetoes_in_replay(trend):
    big = MOMENTUM.model_copy(update={"base_size": 5_000})
    result = await Backtester(trend, Limits(max_position_pct_equity=0.05)).run(big, *span(trend))
    buys = [t for t in result.trades if t["side"] == "buy"]
    cap = 0.05 * result.metrics["final_equity"] * (1 + 1e-9)  # Cap tracks marked equity.
    assert buys and all(499 <= t["quote"] <= cap for t in buys)
    assert result.counters["entry:RESIZE:max_position_pct_equity"] == len(buys)
    halted = Limits(max_trades_per_hour=1)
    result = await Backtester(trend, halted).run(MOMENTUM, *span(trend))
    assert result.counters["exit:REJECT:max_trades_per_hour"] > 0


# --- Outputs and metrics --------------------------------------------------------


async def test_outputs_written(tmp_path, trend):
    result = await Backtester(trend).run(MOMENTUM, *span(trend))
    paths = write_outputs(tmp_path / "out", result)
    with paths["equity_curve"].open() as handle:
        rows = list(csv.DictReader(handle))
    assert rows and float(rows[-1]["equity"]) == pytest.approx(result.metrics["final_equity"])
    with paths["trades"].open() as handle:
        trades = list(csv.DictReader(handle))
    assert len(trades) == result.metrics["trades"] and trades[0]["side"] == "buy"
    metrics = json.loads(paths["metrics"].read_text())
    for key in (
        "sharpe",
        "max_drawdown",
        "profit_factor",
        "win_rate",
        "turnover",
        "fee_drag_pct",
        "trades",
    ):
        assert key in metrics
    assert metrics["params"]["threshold"] == 0.5 and metrics["counters"]["fill:buy"] > 0


def test_metrics_reference_values():
    equity = [
        {"ts": T0 + timedelta(hours=i), "equity": e} for i, e in enumerate([100, 110, 99, 120])
    ]
    trades = [
        {"side": "buy", "quote": 50, "fee": 0.05, "realized_pnl": 0},
        {"side": "sell", "quote": 60, "fee": 0.06, "realized_pnl": 9.0},
        {"side": "sell", "quote": 30, "fee": 0.03, "realized_pnl": -3.0},
    ]
    m = compute_metrics(equity, trades, 100)
    assert m["max_drawdown"] == pytest.approx(0.1)
    assert m["win_rate"] == 0.5 and m["profit_factor"] == pytest.approx(3.0)
    assert m["turnover"] == pytest.approx(1.4) and m["trades"] == 3
    assert m["net_return_pct"] == pytest.approx(20) and m["fee_drag_pct"] == pytest.approx(0.14)
    assert not m["strategy_dead"] and m["sharpe"] != 0
    dead = compute_metrics([{"ts": T0, "equity": 99.9}], [trades[0]], 100)
    assert dead["strategy_dead"] and dead["profit_factor"] is None and dead["win_rate"] is None


# --- Walk-forward ----------------------------------------------------------------


async def test_walk_forward_tunes_on_train_windows_only(trend, monkeypatch):
    bt = Backtester(trend)
    calls = []
    original = bt.run

    async def spy(params, start, end):
        calls.append((params, start, end))
        return await original(params, start, end)

    monkeypatch.setattr(bt, "run", spy)
    start, end = span(trend)
    grid = [MOMENTUM, FAIRVALUE]
    day = timedelta(days=1)
    # Step 2d keeps windows disjoint: [d0,d1) trains for [d1,d2); [d2,d3) trains for [d3,d4).
    result = await bt.walk_forward(start, end, day, day, 2 * day, grid)
    assert len(result.windows) == 2
    for window in result.windows:
        train = tuple(datetime.fromisoformat(t) for t in window["train_range"])
        test = tuple(datetime.fromisoformat(t) for t in window["test_range"])
        assert train[1] == test[0] and test[1] - test[0] == day
        tuned = [c for c in calls if (c[1], c[2]) == train]
        assert [c[0] for c in tuned] == grid  # every grid cell was scored on the train window
        assert not any(c[1] < test[1] and c[2] > test[0] and (c[1], c[2]) != test for c in calls)
    # The second train window contains momentum trades, so the trend-following preset wins
    # there and is what the following test window is evaluated with.
    assert result.windows[1]["params"]["weights"]["momentum"] == 1
    assert result.windows[1]["train_net_return_pct"] > 0
    assert result.metrics["trades"] == sum(w["test_metrics"]["trades"] for w in result.windows)
    with pytest.raises(ValueError):
        await bt.walk_forward(start, end, timedelta(days=30), day, day, grid)


def test_default_grid_and_cli_parsing():
    grid = default_grid()
    assert len(grid) == 20 and len({p.label() for p in grid}) == 20
    assert duration("60d") == timedelta(days=60) and duration("90m") == timedelta(minutes=90)
    with pytest.raises(SystemExit):
        parse_args(["--symbol", SYMBOL, "--days", "30", "--train", "60d"])
    args = parse_args(["--symbol", SYMBOL, "--days", "30"])
    assert args.days == 30 and args.train is None
