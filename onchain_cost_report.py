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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--db", default=os.environ.get("PROBE_DB") or str(DEFAULT_DB))
    args = parser.parse_args()
    summary = summarize(Path(args.db), time.time() - args.days * 86400)
    if not summary:
        print("No samples yet.")
        return
    print(f"Round-trip cost, last {args.days:g} days (bps; 22 = 0.22%)")
    print(f"{'asset':5} {'size':>9} {'venue':10} {'n':>4} {'median':>7} {'p90':>7} {'buy/mid':>8} {'gas $':>7}")
    for s in summary:
        print(f"{s['asset']:5} ${s['size_usd']:>8,.0f} {s['venue']:10} {s['n']:>4} {s['median_bps']:>7.1f} "
              f"{s['p90_bps']:>7.1f} {s['median_buy_bps']:>+8.1f} {s['median_gas_usd']:>7.4f}")


if __name__ == "__main__":
    main()
