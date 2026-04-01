#!/usr/bin/env python3
"""
Combined Experiment: MA20/100 + bear_position_scale=0.25 (trend_following)

Tests the COMBINED effect of two optimizations together vs each in isolation:
  1. Baseline:  MA50/200, no bear scaling (scale=0.0)
  2. MA20/100 only:  MA20/100, no bear scaling (scale=0.0)
  3. Bear 0.25 only:  MA50/200, bear_position_scale=0.25
  4. Combined:  MA20/100 + bear_position_scale=0.25

All configs use trend_following strategy on 1h BTC-USD across 5 periods.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os
import pandas as pd
import numpy as np
import logging

from config import TradingConfig
from backtesting.backtest import BacktestEngine
from backtesting.experiment_bear_rally import find_data_files

logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

PERIODS = {
    "Q1_2024_Bull":     "Bull Market (BTC $42k->$61k)",
    "Q2_2023_Sideways": "Sideways/Choppy Market",
    "2023_Full_Year":   "Full Year 2023 (Mixed)",
    "YTD_2025":         "YTD 2025-2026 (Recent)",
    "H2_2024":          "H2 2024",
}

CONFIGS = [
    # (label, short_ma, long_ma, bear_scale)
    ("Baseline (MA50/200, no bear)",   50, 200, 0.0),
    ("MA20/100 only",                  20, 100, 0.0),
    ("Bear 0.25 only (MA50/200)",      50, 200, 0.25),
    ("COMBINED (MA20/100 + bear0.25)", 20, 100, 0.25),
]

STRATEGY = "trend_following"
TIMEFRAME = "1h"
SYMBOLS = ["BTC-USD"]
INITIAL_CAPITAL = 10000


def run_single(data_files, short_w, long_w, bear_scale):
    """Run a single backtest with specified MA windows and bear scale."""
    os.environ["SHORT_WINDOW"] = str(short_w)
    os.environ["LONG_WINDOW"] = str(long_w)
    os.environ["BEAR_POSITION_SCALE"] = str(bear_scale)
    config = TradingConfig.from_env()

    engine = BacktestEngine(config, initial_capital=INITIAL_CAPITAL)
    return engine.run_backtest(data_files, STRATEGY)


def main():
    rows = []

    for period_key, period_desc in PERIODS.items():
        data_dir = f"data/backtest/{period_key}_{TIMEFRAME}"
        data_files = find_data_files(data_dir, SYMBOLS)
        if not data_files:
            logger.warning(f"No data for {period_key}/{TIMEFRAME}, skipping")
            continue

        for label, short_w, long_w, bear_scale in CONFIGS:
            result = run_single(data_files, short_w, long_w, bear_scale)
            if result:
                rows.append({
                    "config": label,
                    "period": period_key,
                    "return_pct": result["total_return_pct"],
                    "max_dd_pct": result["max_drawdown_pct"],
                    "sharpe": result["sharpe_ratio"],
                    "trades": result["num_trades"],
                    "win_rate": result["win_rate_pct"],
                    "profit_factor": result["profit_factor"],
                    "fees": result["total_fees"],
                })
                logger.info(
                    f"  {label:>38} | {period_key:>20} | "
                    f"Ret={result['total_return_pct']:>+8.2f}%  "
                    f"DD={result['max_drawdown_pct']:>6.2f}%  "
                    f"Sharpe={result['sharpe_ratio']:>6.2f}  "
                    f"Trades={result['num_trades']}"
                )

    # Restore defaults
    os.environ["SHORT_WINDOW"] = "50"
    os.environ["LONG_WINDOW"] = "200"

    if not rows:
        print("No results produced.")
        return

    df = pd.DataFrame(rows)

    # ── Summary table ───────────────────────────────────────────────────
    w = 120
    print("\n" + "=" * w)
    print("COMBINED EXPERIMENT: MA20/100 + bear_position_scale=0.25  (trend_following, 1h, BTC-USD)")
    print("=" * w)

    print(f"\n{'Configuration':>40} | {'Avg Return':>12} | {'Avg MaxDD':>12} | "
          f"{'Avg Sharpe':>12} | {'Avg Trades':>12} | {'Avg WinRate':>12} | {'Avg PF':>10}")
    print("-" * w)

    for label, _, _, _ in CONFIGS:
        sub = df[df["config"] == label]
        if sub.empty:
            continue
        print(f"{label:>40} | "
              f"{sub['return_pct'].mean():>+11.2f}% | "
              f"{sub['max_dd_pct'].mean():>11.2f}% | "
              f"{sub['sharpe'].mean():>12.2f} | "
              f"{sub['trades'].mean():>12.1f} | "
              f"{sub['win_rate'].mean():>11.1f}% | "
              f"{sub['profit_factor'].mean():>10.2f}")

    # ── Per-period breakdown ────────────────────────────────────────────
    print(f"\n{'':>40} ", end="")
    for period_key in PERIODS:
        print(f"| {period_key:>20} ", end="")
    print()
    print("-" * w)

    for metric_label, metric_col, fmt in [
        ("Return %", "return_pct", "{:>+20.2f}"),
        ("MaxDD %",  "max_dd_pct", "{:>20.2f}"),
        ("Sharpe",   "sharpe",     "{:>20.2f}"),
        ("Trades",   "trades",     "{:>20.0f}"),
    ]:
        print(f"\n  >> {metric_label}")
        for label, _, _, _ in CONFIGS:
            print(f"  {label:>38} ", end="")
            for period_key in PERIODS:
                cell = df[(df["config"] == label) & (df["period"] == period_key)]
                if cell.empty:
                    print(f"| {'N/A':>20} ", end="")
                else:
                    val = cell[metric_col].values[0]
                    print(f"| {fmt.format(val)} ", end="")
            print()

    # ── Delta vs baseline ───────────────────────────────────────────────
    baseline_label = CONFIGS[0][0]
    print(f"\n{'':=<{w}}")
    print("DELTA vs BASELINE (percentage-point difference in return)")
    print(f"{'':=<{w}}")

    print(f"{'Configuration':>40} ", end="")
    for period_key in PERIODS:
        print(f"| {period_key:>20} ", end="")
    print(f"| {'AVG DELTA':>12}")
    print("-" * w)

    for label, _, _, _ in CONFIGS[1:]:
        deltas = []
        print(f"{label:>40} ", end="")
        for period_key in PERIODS:
            bl = df[(df["config"] == baseline_label) & (df["period"] == period_key)]
            exp = df[(df["config"] == label) & (df["period"] == period_key)]
            if bl.empty or exp.empty:
                print(f"| {'N/A':>20} ", end="")
            else:
                delta = exp["return_pct"].values[0] - bl["return_pct"].values[0]
                deltas.append(delta)
                print(f"| {delta:>+20.2f} ", end="")
        avg_d = np.mean(deltas) if deltas else float("nan")
        print(f"| {avg_d:>+12.2f}")

    print("\n" + "=" * w)
    print("Done.")


if __name__ == "__main__":
    main()
