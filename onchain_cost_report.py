#!/usr/bin/env python3
"""Summarise onchain_cost_probe samples: round-trip cost per venue, asset and size."""
import argparse
import math
import os
import sqlite3
import statistics
import time
from collections import defaultdict
from pathlib import Path

from onchain_cost_probe import DEFAULT_DB


def _p90(values):
    ordered = sorted(values)
    return ordered[math.ceil(0.9 * len(ordered)) - 1]


def summarize(db_path, since_ts):
    if not Path(db_path).exists():
        return []
    groups = defaultdict(list)
    with sqlite3.connect(db_path) as db:
        for asset, size, venue, rt, buy, gas in db.execute(
                "select asset, size_usd, venue, roundtrip_bps, buy_bps, gas_usd from samples where ts >= ?",
                (since_ts,)):
            groups[(asset, size, venue)].append((rt, buy, gas))
    summary = [{"asset": asset, "size_usd": size, "venue": venue, "n": len(samples),
                "median_bps": statistics.median(s[0] for s in samples),
                "p90_bps": _p90([s[0] for s in samples]),
                "median_buy_bps": statistics.median(s[1] for s in samples),
                "median_gas_usd": statistics.median(s[2] for s in samples)}
               for (asset, size, venue), samples in groups.items()]
    return sorted(summary, key=lambda s: (s["asset"], s["size_usd"], s["median_bps"]))


def summarize_fills(db_path, since_ts):
    """Live test-swap fills (onchain_test_swap) per venue, asset and side."""
    if not Path(db_path).exists():
        return []
    with sqlite3.connect(db_path) as db:
        if not db.execute("select 1 from sqlite_master where name = 'fills'").fetchone():
            return []
        groups = defaultdict(list)
        for venue, asset, side, slip, cost, gas in db.execute(
                "select venue, asset, side, slippage_bps, cost_bps, gas_usd from fills where ts >= ?", (since_ts,)):
            groups[(venue, asset, side)].append((slip, cost, gas))
    return sorted(({"venue": venue, "asset": asset, "side": side, "n": len(rows),
                    "median_slippage_bps": statistics.median(r[0] for r in rows),
                    "median_cost_bps": statistics.median(r[1] for r in rows),
                    "median_gas_usd": statistics.median(r[2] for r in rows)}
                   for (venue, asset, side), rows in groups.items()),
                  key=lambda r: (r["asset"], r["venue"], r["side"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--db", default=os.environ.get("PROBE_DB") or str(DEFAULT_DB))
    args = parser.parse_args()
    since = time.time() - args.days * 86400
    summary = summarize(Path(args.db), since)
    if not summary:
        print("No samples yet.")
        return
    print(f"Round-trip cost, last {args.days:g} days (bps; 22 = 0.22%)")
    print(f"{'asset':5} {'size':>9} {'venue':10} {'n':>4} {'median':>7} {'p90':>7} {'buy/mid':>8} {'gas $':>7}")
    for s in summary:
        print(f"{s['asset']:5} ${s['size_usd']:>8,.0f} {s['venue']:10} {s['n']:>4} {s['median_bps']:>7.1f} "
              f"{s['p90_bps']:>7.1f} {s['median_buy_bps']:>+8.1f} {s['median_gas_usd']:>7.4f}")
    fills = summarize_fills(Path(args.db), since)
    if fills:
        print("\nLive test fills (bps; slippage vs quote, cost vs Coinbase mid)")
        print(f"{'asset':5} {'venue':10} {'side':4} {'n':>4} {'slip':>7} {'cost':>7} {'gas $':>7}")
        for f in fills:
            print(f"{f['asset']:5} {f['venue']:10} {f['side']:4} {f['n']:>4} {f['median_slippage_bps']:>+7.1f} "
                  f"{f['median_cost_bps']:>+7.1f} {f['median_gas_usd']:>7.4f}")


if __name__ == "__main__":
    main()
