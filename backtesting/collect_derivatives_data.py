#!/usr/bin/env python3
"""
Ongoing collector: appends latest OKX derivatives data to local CSVs.
Designed to run hourly (e.g., via systemd timer or cron).

Appends only new rows (deduplicates by timestamp), so it's safe to run frequently.

Usage:
    python backtesting/collect_derivatives_data.py

Add to server as a systemd timer or cron:
    */60 * * * * /path/to/venv/bin/python /path/to/backtesting/collect_derivatives_data.py
"""
import sys
import subprocess
import json
import csv
import io
import time
from datetime import datetime, timezone
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent.parent / "data" / "derivatives"
OKX_BASE = "https://www.okx.com/api/v5"


def okx_get(path: str, params: dict = None) -> dict:
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
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_last_timestamp(csv_path: Path) -> str | None:
    """Read the last timestamp in an existing CSV."""
    if not csv_path.exists():
        return None
    with open(csv_path) as f:
        lines = f.read().strip().splitlines()
    if len(lines) < 2:
        return None
    return lines[-1].split(",")[0]


def append_funding_rates(output_path: Path) -> int:
    """Fetch latest funding rates and append any new ones."""
    last_ts = get_last_timestamp(output_path)

    data = okx_get("/public/funding-rate-history", {"instId": "BTC-USD-SWAP", "limit": "10"})
    entries = data.get("data", [])
    if not entries:
        return 0

    entries.sort(key=lambda x: int(x["fundingTime"]))
    new_rows = 0
    write_header = not output_path.exists()

    with open(output_path, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["timestamp", "funding_rate"])
        for e in entries:
            row_ts = ts_to_dt(int(e["fundingTime"]))
            if last_ts is None or row_ts > last_ts:
                writer.writerow([row_ts, float(e["fundingRate"])])
                new_rows += 1

    return new_rows


def append_open_interest(output_path: Path) -> int:
    """Fetch latest OI and append any new rows."""
    last_ts = get_last_timestamp(output_path)

    data = okx_get("/rubik/stat/contracts/open-interest-volume", {"ccy": "BTC", "period": "1H"})
    entries = data.get("data", [])
    if not entries:
        return 0

    entries.sort(key=lambda x: int(x[0]))
    new_rows = 0
    write_header = not output_path.exists()

    with open(output_path, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["timestamp", "open_interest_usd", "volume_usd"])
        for e in entries:
            row_ts = ts_to_dt(int(e[0]))
            if last_ts is None or row_ts > last_ts:
                writer.writerow([row_ts, float(e[1]), float(e[2])])
                new_rows += 1

    return new_rows


def append_long_short_ratio(output_path: Path) -> int:
    """Fetch latest L/S ratio and append any new rows."""
    last_ts = get_last_timestamp(output_path)

    data = okx_get("/rubik/stat/contracts/long-short-account-ratio", {"ccy": "BTC", "period": "1H"})
    entries = data.get("data", [])
    if not entries:
        return 0

    entries.sort(key=lambda x: int(x[0]))
    new_rows = 0
    write_header = not output_path.exists()

    with open(output_path, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["timestamp", "long_ratio"])
        for e in entries:
            row_ts = ts_to_dt(int(e[0]))
            if last_ts is None or row_ts > last_ts:
                writer.writerow([row_ts, float(e[1])])
                new_rows += 1

    return new_rows


if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now}] Collecting derivatives data...")

    try:
        f_new = append_funding_rates(OUTPUT_DIR / "funding_rates.csv")
        print(f"  Funding rates: +{f_new} new rows")
    except Exception as e:
        print(f"  Funding rates: FAILED ({e})", file=sys.stderr)

    try:
        oi_new = append_open_interest(OUTPUT_DIR / "open_interest.csv")
        print(f"  Open interest: +{oi_new} new rows")
    except Exception as e:
        print(f"  Open interest: FAILED ({e})", file=sys.stderr)

    try:
        ls_new = append_long_short_ratio(OUTPUT_DIR / "long_short_ratio.csv")
        print(f"  Long/short ratio: +{ls_new} new rows")
    except Exception as e:
        print(f"  Long/short ratio: FAILED ({e})", file=sys.stderr)

    print("Done.")
