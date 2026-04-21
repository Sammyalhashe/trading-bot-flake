#!/usr/bin/env python3
"""Run only the missing backtests, skipping already-completed ones."""
import subprocess
import sys
from pathlib import Path

results_dir = Path("data/results")
results_dir.mkdir(parents=True, exist_ok=True)

periods = ["Q1_2024_Bull", "Q2_2023_Sideways", "H2_2024", "2023_Full_Year", "YTD_2025"]
timeframes = {"15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
ma_ratios = [(20, 100), (20, 50), (21, 55), (10, 30), (9, 21)]

# Check what's already done
existing = set()
for f in results_dir.glob("*_equity_curve.csv"):
    existing.add(f.stem.replace("_equity_curve", ""))

# Build missing list
missing = []
for period in periods:
    for tf in timeframes:
        # MA-ratio strategies
        for strat in ["trend_following", "auto"]:
            for ms, ml in ma_ratios:
                key = f"{period}_{tf}_{strat}_MA{ms}_{ml}"
                if key not in existing:
                    missing.append((period, tf, strat, ms, ml))
        # Simple strategies (skip supertrend on 15m — too slow)
        for strat in ["mtf_trend", "mean_reversion", "supertrend"]:
            if strat == "supertrend" and tf == "15m" and period in ("2023_Full_Year", "YTD_2025"):
                continue  # O(n^2) on 35k+ rows, skip
            key = f"{period}_{tf}_{strat}"
            if key not in existing:
                missing.append((period, tf, strat, None, None))

print(f"Missing: {len(missing)} backtests")

script = Path(__file__).parent / "backtest.py"
completed = 0
failed = 0

for i, (period, tf, strat, ms, ml) in enumerate(missing, 1):
    ma_label = f" MA{ms}/{ml}" if ms else ""
    print(f"\n[{i}/{len(missing)}] {period} / {tf} / {strat}{ma_label}")

    data_dir = f"data/backtest/{period}_{tf}"
    if not Path(data_dir).exists():
        print(f"  SKIP: no data at {data_dir}")
        failed += 1
        continue

    cmd = [
        sys.executable, str(script),
        "--data-dir", data_dir,
        "--symbols", "BTC-USD",
        "--strategies", strat,
        "--initial-capital", "10000",
    ]
    if ms and ml:
        cmd.extend(["--ma-short", str(ms), "--ma-long", str(ml)])

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)

        # Move output files
        ma_suffix = f"_MA{ms}_{ml}" if ms else ""
        prefix = f"data/results/{period}_{tf}_{strat}{ma_suffix}"
        eq = Path(f"equity_curve_{strat}.csv")
        tr = Path(f"trades_{strat}.csv")
        if eq.exists():
            eq.rename(f"{prefix}_equity_curve.csv")
        if tr.exists():
            tr.rename(f"{prefix}_trades.csv")
        completed += 1
        print(f"  OK")
    except subprocess.TimeoutExpired:
        print(f"  TIMEOUT (600s)")
        failed += 1
    except subprocess.CalledProcessError as e:
        print(f"  FAILED: {e.stderr[:200]}")
        failed += 1

print(f"\nDone: {completed} completed, {failed} failed, out of {len(missing)} missing")
