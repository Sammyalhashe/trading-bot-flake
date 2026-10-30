#!/usr/bin/env python3
"""Sweep trend_following ATR-stop multiplier/floor and stop-out cooldown.

Selection and validation windows are reported separately so parameters are
chosen on the earlier window only.
"""
import sys
from pathlib import Path
from itertools import product
import logging

sys.path.insert(0, str(Path(__file__).parent.parent))

from decimal import Decimal
import pandas as pd

from config import TradingConfig
from backtest import BacktestEngine

WINDOWS = {
    "selection": ("2025-02-01", "2026-02-28 23:00"),
    "validation": ("2026-03-01", "2026-08-11 23:00"),
}
MULTIPLIERS = [2.5, 3.5, 4.5]
FLOORS = [0.03, 0.05, 0.06]
COOLDOWNS = [0, 12, 24, 48]
ROUND_TRIP_FEE = Decimal("0.0022")


def round_trips(trades: pd.DataFrame) -> int:
    """Entries that were fully closed; partial TP sells belong to the same trip."""
    if len(trades) == 0:
        return 0
    return int((trades["type"] == "BUY").sum())


def trip_stats(trades: pd.DataFrame) -> tuple[float, float]:
    """Win rate and profit factor per round trip (partial sells aggregated, buy fee included)."""
    pnl_by_trip, trip = [], None
    for _, t in trades.iterrows():
        if t["type"] == "BUY":
            trip = -t["fee"]
            pnl_by_trip.append(0.0)
            trip_idx = len(pnl_by_trip) - 1
            pnl_by_trip[trip_idx] = trip
        else:
            pnl_by_trip[trip_idx] += t["pnl"]
    pnl = pd.Series(pnl_by_trip)
    if pnl.empty:
        return 0.0, 0.0
    losses = -pnl[pnl < 0].sum()
    return (pnl > 0).mean() * 100, (pnl[pnl > 0].sum() / losses if losses else float("inf"))


def run(files, mult, floor, cooldown, window):
    config = TradingConfig.from_env()
    config.round_trip_fee_pct = ROUND_TRIP_FEE
    config.atr_stop_multiplier = mult
    config.atr_stop_min_pct = floor
    config.stopout_cooldown_hours = cooldown
    res = BacktestEngine(config, 10000).run_backtest(files, "trend_following", *window)
    win, pf = trip_stats(res["trades"])
    return {
        "mult": mult, "floor": floor, "cooldown_h": cooldown,
        "trips": round_trips(res["trades"]), "fees": res["total_fees"],
        "net_pnl": res["final_value"] - 10000, "return_pct": res["total_return_pct"],
        "max_dd_pct": res["max_drawdown_pct"], "win_pct": win, "pf": pf,
    }


def main():
    logging.disable(logging.CRITICAL)
    files = sorted(str(p) for p in (Path(__file__).parent.parent / "data/historical").glob("*_USD_1h_*.csv"))
    out = []
    for name, window in WINDOWS.items():
        for mult, floor, cooldown in product(MULTIPLIERS, FLOORS, COOLDOWNS):
            row = run(files, mult, floor, cooldown, window)
            row["window"] = name
            out.append(row)
            print(row, flush=True)
    pd.DataFrame(out).to_csv("sweep_stops_results.csv", index=False)


if __name__ == "__main__":
    main()
