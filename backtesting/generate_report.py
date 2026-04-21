#!/usr/bin/env python3
"""Generate comprehensive results report from all equity curve and trades files."""
import re
import sys
import pandas as pd
import numpy as np
from pathlib import Path

RESULTS_DIR = Path(__file__).parent.parent / "data" / "results"

# Candles per year for Sharpe annualization
CANDLES_PER_YEAR = {
    "15m": 365 * 24 * 4,
    "30m": 365 * 24 * 2,
    "1h":  365 * 24,
    "4h":  365 * 6,
    "1d":  365,
}

PERIOD_ORDER = ["Q1_2024_Bull", "Q2_2023_Sideways", "H2_2024", "2023_Full_Year", "YTD_2025"]
TF_ORDER = ["15m", "30m", "1h", "4h", "1d"]


def parse_filename(stem: str):
    """Parse equity curve filename stem into components."""
    # Pattern: {period}_{tf}_{strategy}[_{MA params}]
    tf_pattern = r"(15m|30m|1h|4h|1d)"
    m = re.match(
        rf"^(.+?)_({tf_pattern[1:-1]})_(.+?)(?:_MA(\d+)_(\d+))?$",
        stem.replace("_equity_curve", "")
    )
    if not m:
        return None
    period, tf, strategy, ma_short, ma_long = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
    ma_label = f"MA{ma_short}/{ma_long}" if ma_short else None
    return period, tf, strategy, ma_label, ma_short, ma_long


def compute_metrics(equity_path: Path, trades_path: Path, tf: str) -> dict:
    eq = pd.read_csv(equity_path)

    initial = 10000.0
    final = eq["portfolio_value"].iloc[-1]
    total_return = (final / initial - 1) * 100

    # Drawdown (already in file, but recompute to be safe)
    peak = eq["portfolio_value"].cummax()
    drawdown = (eq["portfolio_value"] / peak - 1) * 100
    max_drawdown = drawdown.min()

    # Sharpe
    returns = eq["portfolio_value"].pct_change().dropna()
    ann_factor = CANDLES_PER_YEAR.get(tf, 365 * 24)
    if returns.std() > 0:
        sharpe = returns.mean() / returns.std() * np.sqrt(ann_factor)
    else:
        sharpe = 0.0

    # Trade stats
    num_trades = win_rate = avg_win = avg_loss = profit_factor = total_fees = 0.0
    if trades_path.exists():
        tr = pd.read_csv(trades_path)
        buys = tr[tr["type"] == "BUY"]
        sells = tr[tr["type"] == "SELL"]
        total_fees = tr["fee"].sum()
        if len(sells) > 0:
            num_trades = len(sells)
            wins = sells[sells["pnl"] > 0]
            losses = sells[sells["pnl"] < 0]
            win_rate = len(wins) / num_trades * 100
            avg_win = wins["pnl_pct"].mean() if len(wins) > 0 else 0
            avg_loss = losses["pnl_pct"].mean() if len(losses) > 0 else 0
            gross_profit = wins["pnl"].sum() if len(wins) > 0 else 0
            gross_loss = abs(losses["pnl"].sum()) if len(losses) > 0 else 0
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    return {
        "final_value": round(final, 2),
        "total_return_pct": round(total_return, 3),
        "max_drawdown_pct": round(max_drawdown, 3),
        "sharpe_ratio": round(sharpe, 3),
        "num_trades": int(num_trades),
        "win_rate_pct": round(win_rate, 2),
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(avg_loss, 3),
        "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else 999.0,
        "total_fees": round(total_fees, 2),
    }


def main():
    rows = []
    missing_trades = []

    equity_files = sorted(RESULTS_DIR.glob("*_equity_curve.csv"))
    print(f"Found {len(equity_files)} equity curve files")

    for eq_path in equity_files:
        parsed = parse_filename(eq_path.stem)
        if not parsed:
            print(f"  SKIP (parse failed): {eq_path.name}")
            continue

        period, tf, strategy, ma_label, ma_short, ma_long = parsed
        trades_path = eq_path.parent / eq_path.name.replace("_equity_curve.csv", "_trades.csv")
        if not trades_path.exists():
            missing_trades.append(eq_path.name)

        try:
            metrics = compute_metrics(eq_path, trades_path, tf)
        except Exception as e:
            print(f"  ERROR {eq_path.name}: {e}")
            continue

        rows.append({
            "period": period,
            "timeframe": tf,
            "strategy": strategy,
            "ma_params": ma_label or "",
            "ma_short": int(ma_short) if ma_short else None,
            "ma_long": int(ma_long) if ma_long else None,
            **metrics,
        })

    df = pd.DataFrame(rows)

    # Sort: period → timeframe → strategy → MA
    period_cat = pd.Categorical(df["period"], categories=PERIOD_ORDER, ordered=True)
    tf_cat = pd.Categorical(df["timeframe"], categories=TF_ORDER, ordered=True)
    df["_period_ord"] = period_cat.codes
    df["_tf_ord"] = tf_cat.codes
    df = df.sort_values(["_period_ord", "_tf_ord", "strategy", "ma_short", "ma_long"]).drop(
        columns=["_period_ord", "_tf_ord"]
    ).reset_index(drop=True)

    out_csv = RESULTS_DIR / "comprehensive_results.csv"
    df.to_csv(out_csv, index=False)
    print(f"\nSaved {len(df)} rows → {out_csv}")

    # --- Summary by strategy (across all periods/timeframes) ---
    print("\n" + "=" * 110)
    print("STRATEGY SUMMARY (averaged across all periods and timeframes)")
    print("=" * 110)
    print(f"{'Strategy':<20} {'MA Params':<12} {'Avg Return':>12} {'Avg MaxDD':>12} {'Avg Sharpe':>12} {'Avg Trades':>12} {'Avg WinRate':>12}")
    print("-" * 110)

    # Group by strategy + MA
    for (strat, ma), grp in df.groupby(["strategy", "ma_params"], sort=False):
        ma_label = ma if ma else "(no MA)"
        print(f"{strat:<20} {ma_label:<12} {grp['total_return_pct'].mean():>+11.2f}% "
              f"{grp['max_drawdown_pct'].mean():>11.2f}% "
              f"{grp['sharpe_ratio'].mean():>12.2f} "
              f"{grp['num_trades'].mean():>12.1f} "
              f"{grp['win_rate_pct'].mean():>11.1f}%")

    # --- Summary by timeframe ---
    print("\n" + "=" * 110)
    print("TIMEFRAME SUMMARY (averaged across all periods and strategies)")
    print("=" * 110)
    print(f"{'Timeframe':<12} {'Configs':>8} {'Avg Return':>12} {'Avg MaxDD':>12} {'Avg Sharpe':>12} {'Avg Trades':>12}")
    print("-" * 110)
    for tf in TF_ORDER:
        grp = df[df["timeframe"] == tf]
        if len(grp) == 0:
            continue
        print(f"{tf:<12} {len(grp):>8} {grp['total_return_pct'].mean():>+11.2f}% "
              f"{grp['max_drawdown_pct'].mean():>11.2f}% "
              f"{grp['sharpe_ratio'].mean():>12.2f} "
              f"{grp['num_trades'].mean():>12.1f}")

    # --- Best configs by Sharpe (min 3 trades) ---
    print("\n" + "=" * 110)
    print("TOP 20 CONFIGS BY SHARPE RATIO (min 3 trades)")
    print("=" * 110)
    print(f"{'Period':<22} {'TF':<6} {'Strategy':<20} {'MA':<12} {'Return':>10} {'MaxDD':>10} {'Sharpe':>8} {'Trades':>8} {'WinRate':>10}")
    print("-" * 110)
    top = df[df["num_trades"] >= 3].nlargest(20, "sharpe_ratio")
    for _, r in top.iterrows():
        ma = r["ma_params"] if r["ma_params"] else ""
        print(f"{r['period']:<22} {r['timeframe']:<6} {r['strategy']:<20} {ma:<12} "
              f"{r['total_return_pct']:>+9.2f}% {r['max_drawdown_pct']:>9.2f}% "
              f"{r['sharpe_ratio']:>8.2f} {r['num_trades']:>8} {r['win_rate_pct']:>9.1f}%")

    # --- Per-period best ---
    print("\n" + "=" * 110)
    print("BEST CONFIG PER PERIOD (by Sharpe, min 3 trades)")
    print("=" * 110)
    for period in PERIOD_ORDER:
        grp = df[(df["period"] == period) & (df["num_trades"] >= 3)]
        if grp.empty:
            continue
        best = grp.loc[grp["sharpe_ratio"].idxmax()]
        ma = best["ma_params"] if best["ma_params"] else ""
        print(f"{period:<22}  {best['timeframe']:<6} {best['strategy']:<20} {ma:<12} "
              f"Return: {best['total_return_pct']:>+7.2f}%  "
              f"Sharpe: {best['sharpe_ratio']:>6.2f}  "
              f"MaxDD: {best['max_drawdown_pct']:>7.2f}%  "
              f"Trades: {int(best['num_trades'])}")

    if missing_trades:
        print(f"\nNote: {len(missing_trades)} equity curves had no matching trades file")

    print("\n" + "=" * 110)
    print(f"Full results: {out_csv}")

    # Save report as text too
    import io, sys as _sys
    out_log = RESULTS_DIR / "full_comprehensive_run.log"
    out_log.write_text(f"Generated: {pd.Timestamp.now()}\nRows: {len(df)}\n")
    df.to_string(buf=open(out_log, "a"), index=False)
    print(f"Log:          {out_log}")


if __name__ == "__main__":
    main()
