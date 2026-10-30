#!/usr/bin/env python3
import os
import html
import json
import re
import sys
from datetime import datetime

from core.telegram import send_telegram_message

# --- Configuration ---
_HOME = os.path.expanduser("~")
REPORT_FILE = os.environ.get("TRADING_REPORT_FILE", os.path.join(_HOME, ".openclaw", "workspace", "trading-bot", "report.txt"))
# File to persist market regime change counters and recent transactions
MARKET_STATE_FILE = os.environ.get("MARKET_STATE_FILE", os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.join(_HOME, ".local", "state")),
    "trading-bot", "market_state.json"
))

# Emoji definitions
REGIME_CHANGE_EMOJI = "🔄"  # blue arrow cycle for market flips
ISSUE_EMOJI = "❗"        # exclamation for actual issues

def load_market_state():
    """Load persisted market state. Create defaults if missing."""
    default_state = {
        "last_status": "Unknown",
        "last_btc_macro": "Unknown",
        "last_rotation": "Unknown",
        "bullish_changes": 0,
        "bearish_changes": 0,
        "regime_changes": 0,
        "transactions": []  # list of {"time": str, "detail": str}
    }
    if not os.path.exists(MARKET_STATE_FILE):
        # Initialise file
        with open(MARKET_STATE_FILE, 'w') as f:
            json.dump(default_state, f, indent=2)
        return default_state
    try:
        with open(MARKET_STATE_FILE, 'r') as f:
            state = json.load(f)
            # Migrate old state schema to new (add missing fields with defaults)
            for key in default_state:
                if key not in state:
                    state[key] = default_state[key]
            return state
    except Exception as e:
        print(f"{ISSUE_EMOJI} Failed to load market state ({e}), resetting.")
        with open(MARKET_STATE_FILE, 'w') as f:
            json.dump(default_state, f, indent=2)
        return default_state

def save_market_state(state):
    with open(MARKET_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def extract_market_status(report):
    """Return the market status string from the report text.

    Expected formats:
    - Dual-regime: "Market Regime: STRONG_BULL (BTC: BULL | Rotation: ETH_LEADING)"
    - Single-asset: "Market Regime (BTC): BULL"
    - Legacy: "Market Status: BULL"

    Returns:
        tuple: (regime, btc_macro, rotation) where regime is the primary state
               btc_macro and rotation may be None if not using dual-signal
    """
    for line in report.splitlines():
        if "market regime:" in line.lower() or "market status:" in line.lower():
            # Extract everything after the colon
            content = line.split(":", 1)[1].strip()

            # Check for dual-signal format with parentheses
            if "(" in content and ")" in content:
                # Extract regime before parentheses
                regime = content.split("(")[0].strip()

                # Extract BTC macro and rotation from parentheses
                paren_content = content.split("(")[1].split(")")[0]
                btc_macro = None
                rotation = None

                if "BTC:" in paren_content:
                    btc_part = paren_content.split("BTC:")[1].split("|")[0].strip()
                    btc_macro = btc_part

                if "Rotation:" in paren_content:
                    rotation_part = paren_content.split("Rotation:")[1].strip()
                    rotation = rotation_part

                return (regime, btc_macro, rotation)
            else:
                # Single-asset or legacy format. Keep only the regime word: the
                # bot's banner line appends "━━━", which would read as a change.
                return (content.split()[0] if content else content, None, None)

    return ("Unknown", None, None)

REGIME_ORDER = ["STRONG_BEAR", "BEAR", "NEUTRAL", "BULL", "STRONG_BULL"]

def regime_emoji(old, new):
    """Chart-up when the regime became more bullish, chart-down otherwise."""
    if old in REGIME_ORDER and new in REGIME_ORDER:
        return "📈" if REGIME_ORDER.index(new) > REGIME_ORDER.index(old) else "📉"
    return REGIME_CHANGE_EMOJI

def extract_signal_lines(report):
    """Short BTC trend / ETH-BTC rotation lines from the latest matching log lines."""
    lines = []
    btc = re.findall(r"BTC Macro: (\$[\d,]+).*?Spread: ([+-]?[\d.]+)%", report)
    if btc:
        price, spread = btc[-1]
        lines.append(f"BTC {price} · MA spread {float(spread):+.1f}%")
    eth = re.findall(r"Ratio=([\d.]+) \(ETH (\$[\d,]+) / BTC (\$[\d,]+)\)", report)
    if eth:
        ratio, eth_price, _ = eth[-1]
        lines.append(f"ETH/BTC {float(ratio):.4f} · ETH {eth_price}")
    return lines

def format_change_time(iso_time):
    try:
        return datetime.fromisoformat(iso_time).strftime("%b %d %H:%M")
    except (TypeError, ValueError):
        return str(iso_time)

def build_regime_message(old_regime, new_regime, btc_macro, rotation, report, transactions):
    lines = [f"{regime_emoji(old_regime, new_regime)} <b>Regime: {html.escape(old_regime)} → {html.escape(new_regime)}</b>", ""]
    if btc_macro:
        lines.append(f"BTC trend: {html.escape(btc_macro)}")
    if rotation:
        lines.append(f"Rotation: {html.escape(rotation)}")
    lines.extend(html.escape(line) for line in extract_signal_lines(report))
    recent = transactions[-3:]
    if recent:
        lines += ["", "<b>Recent</b>"]
        for tx in recent:
            change = tx.get("change") or tx.get("detail", "")
            lines.append(f"{format_change_time(tx.get('time'))} · {html.escape(change)}")
    return "\n".join(lines)

def main():
    print(f"[{datetime.now()}] Notify script running...")
    if not os.path.exists(REPORT_FILE):
        print(f"{ISSUE_EMOJI} Report file not found: {REPORT_FILE}")
        return

    with open(REPORT_FILE, 'r') as f:
        report_content = f.read().strip()

    # Load and possibly update market state
    state = load_market_state()
    current_regime, btc_macro, rotation = extract_market_status(report_content)

    # Only proceed if regime changed
    if state["last_status"] != current_regime:
        # Regime changed - determine direction and reason
        previous_regime = state["last_status"]
        change_type = f"{previous_regime} → {current_regime}"

        # Map regime to bullish/bearish for legacy counters
        def is_bullish_regime(regime):
            return regime.upper() in ["BULL", "STRONG_BULL", "BULLISH"]

        def is_bearish_regime(regime):
            return regime.upper() in ["BEAR", "STRONG_BEAR", "BEARISH"]

        old_bullish = is_bullish_regime(state["last_status"])
        new_bullish = is_bullish_regime(current_regime)
        old_bearish = is_bearish_regime(state["last_status"])
        new_bearish = is_bearish_regime(current_regime)

        # Increment counters based on transition
        if new_bullish and not old_bullish:
            state["bullish_changes"] += 1
        elif new_bearish and not old_bearish:
            state["bearish_changes"] += 1

        state["regime_changes"] += 1

        # Build detailed change message with reason if dual-signal mode
        detail_msg = f"{REGIME_CHANGE_EMOJI} Regime change: {change_type}"
        if btc_macro and rotation:
            detail_msg += f" (BTC: {btc_macro}, Rotation: {rotation})"

        # Record the change event
        state["transactions"].append({
            "time": datetime.now().isoformat(),
            "detail": detail_msg,
            "change": change_type
        })

        # Update state
        state["last_status"] = current_regime
        if btc_macro:
            state["last_btc_macro"] = btc_macro
        if rotation:
            state["last_rotation"] = rotation

        save_market_state(state)

        # Send notification ONLY on regime change
        full_message = build_regime_message(
            previous_regime, current_regime, btc_macro, rotation,
            report_content, state.get("transactions", []))
        print(f"Regime change detected: {change_type}. Sending notification...")
        send_telegram_message(full_message)
    else:
        print("No regime change. Skipping notification.")

if __name__ == "__main__":
    main()
