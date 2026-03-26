#!/usr/bin/env python3
"""Migrate trading_state.json: clean orphaned entries from old key format.

Old format: "ICP-USDC" (no executor prefix)
New format: "CoinbaseExecutor:ICP-USDC"

Orphaned entries can't be matched by the sell loop, so positions go unmanaged.
This script removes them (they represent positions that were either already sold
manually or are no longer tracked).

Usage:
    python3 scripts/migrate_state.py [--dry-run] [STATE_FILE]

    --dry-run   Show what would change without writing
    STATE_FILE  Path to trading_state.json (default: ./trading_state.json)
"""
import json
import sys
import os
import re

def is_orphaned_key(key):
    """An orphaned key has no executor prefix (no colon before the product_id)."""
    # Valid keys look like: "CoinbaseExecutor:BTC-USDC" or "EthereumExecutor_0x4FA0:ETH-USDC"
    # Orphaned keys look like: "BTC-USDC" (just a product_id)
    # Short keys look like: "CoinbaseExecutor:BTC-USDC:SHORT"
    # A product_id is always "{ASSET}-{QUOTE}" e.g. "BTC-USDC"
    parts = key.split(":")
    if len(parts) == 1:
        # No colon at all — "BTC-USDC" format → orphaned
        return True
    # Check if the first part looks like a product_id (ASSET-QUOTE) rather than an executor name
    if re.match(r'^[A-Z]+-[A-Z]+$', parts[0]):
        return True
    return False


def migrate(state_file, dry_run=False):
    with open(state_file, 'r') as f:
        state = json.load(f)

    changes = []

    # Clean orphaned entry_prices
    orphaned = [k for k in state.get("entry_prices", {}) if is_orphaned_key(k)]
    for key in orphaned:
        changes.append(f"  entry_prices: remove '{key}' (entry=${state['entry_prices'][key]})")
        if not dry_run:
            del state["entry_prices"][key]

    # Clean orphaned high_water_marks
    orphaned_hwm = [k for k in state.get("high_water_marks", {}) if is_orphaned_key(k)]
    for key in orphaned_hwm:
        changes.append(f"  high_water_marks: remove '{key}'")
        if not dry_run:
            del state["high_water_marks"][key]

    # Clean orphaned take_profit_flags
    orphaned_tp = [k for k in state.get("take_profit_flags", {}) if is_orphaned_key(k)]
    for key in orphaned_tp:
        changes.append(f"  take_profit_flags: remove '{key}'")
        if not dry_run:
            del state["take_profit_flags"][key]

    # Clean orphaned entry_timestamps
    orphaned_ts = [k for k in state.get("entry_timestamps", {}) if is_orphaned_key(k)]
    for key in orphaned_ts:
        changes.append(f"  entry_timestamps: remove '{key}'")
        if not dry_run:
            del state["entry_timestamps"][key]

    if not changes:
        print("No orphaned entries found. State is clean.")
        return

    print(f"{'[DRY RUN] ' if dry_run else ''}Changes ({len(changes)}):")
    for c in changes:
        print(c)

    if not dry_run:
        # Atomic write
        tmp = state_file + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, state_file)
        print(f"\nWritten to {state_file}")
    else:
        print(f"\nRe-run without --dry-run to apply.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run = "--dry-run" in sys.argv
    state_file = args[0] if args else "trading_state.json"

    if not os.path.exists(state_file):
        print(f"Error: {state_file} not found")
        sys.exit(1)

    migrate(state_file, dry_run)
