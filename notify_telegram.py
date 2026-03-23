#!/usr/bin/env python3
import os
import json
import requests
import sys
from datetime import datetime

# --- Configuration ---
_HOME = os.path.expanduser("~")
REPORT_FILE = os.environ.get("TRADING_REPORT_FILE", os.path.join(_HOME, ".openclaw", "workspace", "trading-bot", "report.txt"))
TELEGRAM_TOKEN_FILE = "/run/secrets/telegram_bot_token"
CHAT_ID = "8555669756"
# File to persist market regime change counters and recent transactions
MARKET_STATE_FILE = os.environ.get("MARKET_STATE_FILE", os.path.join(_HOME, "trading-bot-flake", "market_state.json"))

# Emoji definitions
REGIME_CHANGE_EMOJI = "🔄"  # blue arrow cycle for market flips
ISSUE_EMOJI = "❗"        # exclamation for actual issues

def send_telegram_message(message):
    if not os.path.exists(TELEGRAM_TOKEN_FILE):
        print(f"{ISSUE_EMOJI} Token file not found at {TELEGRAM_TOKEN_FILE}")
        return

    with open(TELEGRAM_TOKEN_FILE, 'r') as f:
        token = f.read().strip()

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        print("Notification sent successfully.")
    except Exception as e:
        print(f"{ISSUE_EMOJI} Failed to send notification: {e}")

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
                # Single-asset or legacy format - just the regime
                return (content, None, None)

    return ("Unknown", None, None)

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
        change_type = f"{state['last_status']} → {current_regime}"

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
            "detail": detail_msg
        })

        # Update state
        state["last_status"] = current_regime
        if btc_macro:
            state["last_btc_macro"] = btc_macro
        if rotation:
            state["last_rotation"] = rotation

        save_market_state(state)

        # Send notification ONLY on regime change
        extra = (
            f"\nTotal regime changes: {state['regime_changes']}"\
            f"\nBullish regime changes: {state['bullish_changes']}"\
            f"\nBearish regime changes: {state['bearish_changes']}"
        )
        # Include recent regime changes (last 5)
        recent_tx = state.get("transactions", [])[-5:]
        if recent_tx:
            extra += "\nRecent regime changes:" + "".join([
                f"\n- {tx['time']}: {tx['detail']}" for tx in recent_tx
            ])
        full_message = f"<pre>{report_content}{extra}</pre>"
        print(f"Regime change detected: {change_type}. Sending notification...")
        send_telegram_message(full_message)
    else:
        print("No regime change. Skipping notification.")

if __name__ == "__main__":
    main()
