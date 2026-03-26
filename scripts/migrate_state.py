#!/usr/bin/env python3
"""Migrate trading_state.json: fix orphaned entries from old key format.

Old format: "ICP-USDC" (no executor prefix)
New format: "CoinbaseExecutor:ICP-USDC"

Orphaned entries can't be matched by the sell loop, so positions go unmanaged.
This script re-keys them to the new format (default executor: CoinbaseExecutor)
so exit logic works again. Use --remove to delete entries instead of re-keying.

Usage:
    python3 scripts/migrate_state.py [--dry-run] [--remove] [--executor EXECUTOR_ID] [STATE_FILE]

    --dry-run               Show what would change without writing
    --remove                Delete orphaned entries instead of re-keying
    --executor EXECUTOR_ID  Executor prefix for re-keying (default: CoinbaseExecutor)
    STATE_FILE              Path to trading_state.json (default: ./trading_state.json)
"""
import json
import sys
import os
import re


def is_orphaned_key(key):
    """An orphaned key has no executor prefix (no colon before the product_id)."""
    parts = key.split(":")
    if len(parts) == 1:
        return True
    if re.match(r'^[A-Z]+-[A-Z]+$', parts[0]):
        return True
    return False


def migrate_dict(d, executor_id, remove, changes, label):
    """Migrate orphaned keys in a state sub-dict."""
    orphaned = [k for k in d if is_orphaned_key(k)]
    for key in orphaned:
        if remove:
            changes.append(f"  {label}: remove '{key}'")
            del d[key]
        else:
            new_key = f"{executor_id}:{key}"
            if new_key in d:
                changes.append(f"  {label}: skip '{key}' → '{new_key}' already exists")
            else:
                changes.append(f"  {label}: rekey '{key}' → '{new_key}'")
                d[new_key] = d.pop(key)


def migrate(state_file, dry_run=False, remove=False, executor_id="CoinbaseExecutor"):
    with open(state_file, 'r') as f:
        state = json.load(f)

    # Work on a copy for dry-run safety
    if dry_run:
        import copy
        work = copy.deepcopy(state)
    else:
        work = state

    changes = []

    for section in ["entry_prices", "high_water_marks", "take_profit_flags", "entry_timestamps"]:
        if section in work:
            migrate_dict(work[section], executor_id, remove, changes, section)

    if not changes:
        print("No orphaned entries found. State is clean.")
        return

    action = "remove" if remove else f"rekey to {executor_id}"
    print(f"{'[DRY RUN] ' if dry_run else ''}Action: {action}")
    print(f"Changes ({len(changes)}):")
    for c in changes:
        print(c)

    if not dry_run:
        tmp = state_file + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(work, f, indent=2)
        os.replace(tmp, state_file)
        print(f"\nWritten to {state_file}")
    else:
        print(f"\nRe-run without --dry-run to apply.")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    remove = "--remove" in sys.argv
    executor_id = "CoinbaseExecutor"

    args = []
    skip_next = False
    for i, a in enumerate(sys.argv[1:], 1):
        if skip_next:
            skip_next = False
            continue
        if a == "--executor" and i < len(sys.argv) - 1:
            executor_id = sys.argv[i + 1]
            skip_next = True
        elif a not in ("--dry-run", "--remove"):
            args.append(a)

    state_file = args[0] if args else "trading_state.json"

    if not os.path.exists(state_file):
        print(f"Error: {state_file} not found")
        sys.exit(1)

    migrate(state_file, dry_run, remove, executor_id)
