#!/usr/bin/env python3
"""Select a daily trend filter for BTC (or BTC+ETH) that holds up in bull, bear and sideways markets.

Vectorized on daily closes: a position decided on a closed day is held from the next day, and
every switch pays one side of fees. The selection rule below is fixed before validation is run:
score each config by its worst selection-period (return - 0.5 * |max drawdown|), keep the top 3,
then report validation for exactly those 3.
"""
import argparse
import itertools
import os
from pathlib import Path

import pandas as pd

FEE_PER_SIDE = 0.0011
SELECTION = {"bull 2023-24": ("2023-01-01", "2024-12-31"),
             "sideways 2023": ("2023-04-02", "2023-08-29"),
             "sideways 2024": ("2024-03-03", "2024-07-30")}
VALIDATION = {"bear 2025-26": ("2025-02-01", "2026-11-17"),
              "sideways 2025": ("2025-06-01", "2025-10-28")}
FILTERS = [("sma", 50), ("sma", 100), ("sma", 200), ("ema", 21, 55), ("ema", 50, 200)]
BUFFERS = [0.0, 0.02, 0.04]
CONFIRMS = [1, 3]
UNIVERSES = [("BTC",), ("BTC", "ETH")]


def load_closes(daily_dir: Path, asset: str) -> pd.Series:
    df = pd.read_csv(daily_dir / f"{asset}_USD_1d_full.csv", parse_dates=["timestamp"])
    return df.set_index("timestamp")["close"]


def positions(close: pd.Series, flt, buffer: float, confirm: int) -> pd.Series:
    """1 while the trend filter says long, with a hysteresis band and N-day confirmation."""
    if flt[0] == "sma":
        x, ref = close, close.rolling(flt[1]).mean()
    else:
        x, ref = close.ewm(span=flt[1], adjust=False).mean(), close.ewm(span=flt[2], adjust=False).mean()
    up = (x > ref * (1 + buffer)).astype(int).rolling(confirm).min() == 1
    down = (x < ref * (1 - buffer)).astype(int).rolling(confirm).min() == 1
    pos, held = [], 0
    for is_up, is_down, valid in zip(up, down, ref.notna()):
        if not valid:
            held = 0
        elif not held and is_up:
            held = 1
        elif held and is_down:
            held = 0
        pos.append(held)
    return pd.Series(pos, index=close.index)


def sleeve_equity(close: pd.Series, pos: pd.Series, start, end) -> pd.Series:
    held = pos.shift(1).fillna(0)[start:end]
    rets = close.pct_change().fillna(0)[start:end] * held - held.diff().abs().fillna(held.iloc[0]) * FEE_PER_SIDE
    return (1 + rets).cumprod()


def evaluate(closes, universe, flt, buffer, confirm, window):
    start, end = window
    sleeves = [sleeve_equity(closes[a], positions(closes[a], flt, buffer, confirm), start, end) for a in universe]
    equity = sum(sleeves) / len(sleeves)
    switches = sum(int(positions(closes[a], flt, buffer, confirm)[start:end].diff().abs().sum()) for a in universe)
    return equity.iloc[-1] - 1, (equity / equity.cummax() - 1).min(), switches


def label(universe, flt, buffer, confirm):
    name = f"SMA{flt[1]}" if flt[0] == "sma" else f"EMA{flt[1]}/{flt[2]}"
    return f"{'+'.join(universe):7} {name:9} buf {buffer:.0%} conf {confirm}d"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default_dir = Path(os.environ["BACKTEST_DATA_DIR"]).parent / "1d" if os.environ.get("BACKTEST_DATA_DIR") else None
    parser.add_argument("--daily-dir", type=Path, default=default_dir, required=default_dir is None)
    args = parser.parse_args()
    closes = {a: load_closes(args.daily_dir, a) for a in ("BTC", "ETH")}

    def benchmark_line(window):
        p = closes["BTC"][window[0]:window[1]]
        hold = p.iloc[-1] / p.iloc[0] - 1
        return f"BTC hold {hold:+.0%} (maxDD {(p / p.cummax() - 1).min():.0%}), 30% BTC {0.3 * hold:+.0%}"

    configs = list(itertools.product(UNIVERSES, FILTERS, BUFFERS, CONFIRMS))
    scored = []
    for cfg in configs:
        results = {name: evaluate(closes, *cfg, window) for name, window in SELECTION.items()}
        score = min(ret - 0.5 * abs(dd) for ret, dd, _ in results.values())
        scored.append((score, cfg, results))
    scored.sort(key=lambda item: item[0], reverse=True)

    print("== Selection periods ==")
    for name, window in SELECTION.items():
        print(f"  {name:14} {benchmark_line(window)}")
    print(f"\n{'config':38} {'score':>6}  " + "  ".join(f"{n:>22}" for n in SELECTION))
    for score, cfg, results in scored[:12]:
        cells = "  ".join(f"{r:+6.0%} dd{dd:5.0%} sw{sw:3}" for r, dd, sw in results.values())
        print(f"{label(*cfg):38} {score:+6.2f}  {cells}")

    print("\n== Validation (top 3 by the pre-registered rule; not used for selection) ==")
    for name, window in VALIDATION.items():
        print(f"  {name:14} {benchmark_line(window)}")
    print(f"\n{'config':38} " + "  ".join(f"{n:>22}" for n in VALIDATION))
    for _, cfg, _ in scored[:3]:
        cells = "  ".join(f"{r:+6.0%} dd{dd:5.0%} sw{sw:3}" for r, dd, sw in
                          (evaluate(closes, *cfg, window) for window in VALIDATION.values()))
        print(f"{label(*cfg):38} {cells}")


if __name__ == "__main__":
    main()
