#!/usr/bin/env python3
"""
Download historical OKX derivatives data (funding rates, open interest, long/short ratio)
and save to data/derivatives/ for backtesting.

Usage:
    python backtesting/download_derivatives_data.py

Output files:
    data/derivatives/funding_rates.csv   - 8h funding rate settlements
    data/derivatives/open_interest.csv   - 1h OI snapshots
    data/derivatives/long_short_ratio.csv - 1h L/S ratio snapshots
"""
import sys
import subprocess
import json
import csv
import time
from datetime import datetime, timezone
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent.parent / "data" / "derivatives"
OKX_BASE = "https://www.okx.com/api/v5"


def okx_get(path: str, params: dict = None) -> dict:
    """Fetch from OKX public API via curl (avoids SSL issues in dev environment)."""
    url = OKX_BASE + path
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url += "?" + qs
    result = subprocess.run(
        ["curl", "-s", "--max-time", "15", url],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl failed: {result.stderr}")
    return json.loads(result.stdout)


def ts_to_dt(ts_ms: int) -> str:
    """Convert millisecond timestamp to ISO datetime string (UTC)."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def download_funding_rates(output_path: Path) -> int:
    """Download all available funding rate history from OKX (8h settlements)."""
    print("Downloading funding rates...")
    all_entries = []
    after = None
    page = 0

    while True:
        params = {"instId": "BTC-USD-SWAP", "limit": "100"}
        if after:
            params["after"] = str(after)

        data = okx_get("/public/funding-rate-history", params)
        entries = data.get("data", [])
        if not entries:
            break

        all_entries.extend(entries)
        after = min(int(e["fundingTime"]) for e in entries)
        page += 1
        print(f"  Page {page}: {len(entries)} entries, oldest {ts_to_dt(after)}")
        time.sleep(0.3)

    # Sort ascending and write
    all_entries.sort(key=lambda x: int(x["fundingTime"]))
    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "funding_rate"])
        for e in all_entries:
            writer.writerow([ts_to_dt(int(e["fundingTime"])), float(e["fundingRate"])])

    print(f"  Saved {len(all_entries)} funding rate entries to {output_path}")
    return len(all_entries)


def download_open_interest(output_path: Path) -> int:
    """Download open interest history from OKX (1h intervals, ~30 days max)."""
    print("Downloading open interest...")
    data = okx_get("/rubik/stat/contracts/open-interest-volume", {"ccy": "BTC", "period": "1H"})
    entries = data.get("data", [])

    entries.sort(key=lambda x: int(x[0]))
    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open_interest_usd", "volume_usd"])
        for e in entries:
            writer.writerow([ts_to_dt(int(e[0])), float(e[1]), float(e[2])])

    print(f"  Saved {len(entries)} OI entries to {output_path}")
    return len(entries)


def download_long_short_ratio(output_path: Path) -> int:
    """Download long/short ratio history from OKX (1h intervals)."""
    print("Downloading long/short ratio...")
    data = okx_get("/rubik/stat/contracts/long-short-account-ratio", {"ccy": "BTC", "period": "1H"})
    entries = data.get("data", [])

    entries.sort(key=lambda x: int(x[0]))
    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "long_ratio"])
        for e in entries:
            writer.writerow([ts_to_dt(int(e[0])), float(e[1])])

    print(f"  Saved {len(entries)} L/S ratio entries to {output_path}")
    return len(entries)


if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    funding_count = download_funding_rates(OUTPUT_DIR / "funding_rates.csv")
    oi_count = download_open_interest(OUTPUT_DIR / "open_interest.csv")
    ls_count = download_long_short_ratio(OUTPUT_DIR / "long_short_ratio.csv")

    print(f"\nDone. Downloaded: {funding_count} funding, {oi_count} OI, {ls_count} L/S entries")
    print(f"Files saved to: {OUTPUT_DIR}")
