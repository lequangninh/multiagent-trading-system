"""Backtest metrics and file outputs. Undefined ratios are written as JSON null."""

import csv
import json
import math
from pathlib import Path

HOURS_PER_YEAR = 24 * 365
EQUITY_COLUMNS = ["ts", "cash", "inventory_value", "equity", "fees_paid"]
TRADE_COLUMNS = [
    "ts",
    "symbol",
    "side",
    "reason",
    "quantity",
    "price",
    "mid",
    "slippage_bps",
    "quote",
    "fee",
    "requested_quote",
    "partial",
    "lot_id",
    "realized_pnl",
    "holding_s",
]


def max_drawdown(equity: list[float]) -> float:
    peak, worst = -math.inf, 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def sharpe(rows: list[dict]) -> float:
    """Annualised Sharpe of hourly equity returns (last mark per UTC hour), risk-free 0."""
    hourly: dict = {}
    for row in rows:
        hourly[row["ts"].replace(minute=0, second=0, microsecond=0)] = row["equity"]
    values = [hourly[k] for k in sorted(hourly)]
    returns = [b / a - 1 for a, b in zip(values, values[1:]) if a > 0]
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return mean / math.sqrt(var) * math.sqrt(HOURS_PER_YEAR) if var > 0 else 0.0


def compute_metrics(equity_rows, trade_rows, initial_cash: float) -> dict:
    final = equity_rows[-1]["equity"] if equity_rows else initial_cash
    fees = sum(t["fee"] for t in trade_rows)
    net_return_pct = (final - initial_cash) / initial_cash * 100
    fee_drag_pct = fees / initial_cash * 100
    gross_return_pct = net_return_pct + fee_drag_pct
    closes = [t["realized_pnl"] for t in trade_rows if t["side"] == "sell"]
    wins = [p for p in closes if p > 0]
    losses = [p for p in closes if p < 0]
    return {
        "trades": len(trade_rows),
        "round_trips": len(closes),
        "win_rate": len(wins) / len(closes) if closes else None,
        "profit_factor": sum(wins) / -sum(losses) if losses else None,
        "sharpe": sharpe(equity_rows),
        "max_drawdown": max_drawdown([r["equity"] for r in equity_rows]),
        "turnover": sum(t["quote"] for t in trade_rows) / initial_cash,
        "fee_drag_pct": fee_drag_pct,
        "gross_return_pct": gross_return_pct,
        "net_return_pct": net_return_pct,
        "fees_paid": fees,
        "initial_equity": initial_cash,
        "final_equity": final,
        "strategy_dead": fee_drag_pct > gross_return_pct,
    }


def write_outputs(out_dir, result) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "equity_curve": out / "equity_curve.csv",
        "trades": out / "trades.csv",
        "metrics": out / "metrics.json",
    }
    with paths["equity_curve"].open("w", newline="") as handle:
        writer = csv.DictWriter(handle, EQUITY_COLUMNS)
        writer.writeheader()
        for row in result.equity:
            writer.writerow(row | {"ts": row["ts"].isoformat()})
    with paths["trades"].open("w", newline="") as handle:
        writer = csv.DictWriter(handle, TRADE_COLUMNS)
        writer.writeheader()
        for row in result.trades:
            writer.writerow(row | {"ts": row["ts"].isoformat()})
    paths["metrics"].write_text(json.dumps(result.report(), indent=2, default=str))
    return paths
