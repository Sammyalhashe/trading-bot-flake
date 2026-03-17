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
        "bullish_changes": 0,
        "bearish_changes": 0,
        "transactions": []  # list of {"time": str, "detail": str}
    }
    if not os.path.exists(MARKET_STATE_FILE):
        # Initialise file
        with open(MARKET_STATE_FILE, 'w') as f:
            json.dump(default_state, f, indent=2)
        return default_state
    try:
        with open(MARKET_STATE_FILE, 'r') as f:
            return json.load(f)
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
    Expected line format: "Market Status: <status>".
    """
    for line in report.splitlines():
        if line.lower().startswith("market status:"):
            return line.split(":", 1)[1].strip()
    return "Unknown"

def main():
    print(f"[{datetime.now()}] Notify script running...")
    if not os.path.exists(REPORT_FILE):
        print(f"{ISSUE_EMOJI} Report file not found: {REPORT_FILE}")
        return

    with open(REPORT_FILE, 'r') as f:
        report_content = f.read().strip()

    # Load and possibly update market state
    state = load_market_state()
    current_status = extract_market_status(report_content)
    if state["last_status"] != current_status:
        # Status changed
        if current_status.lower() == "bullish":
            state["bullish_changes"] += 1
        elif current_status.lower() == "bearish":
            state["bearish_changes"] += 1
        # Record the change event with the new emoji
        state["transactions"].append({
            "time": datetime.now().isoformat(),
            "detail": f"{REGIME_CHANGE_EMOJI} Regime change: {state['last_status']} → {current_status}"
        })
        state["last_status"] = current_status
        save_market_state(state)

    # Determine whether to send a notification
    is_heartbeat = datetime.now().hour == 8 and datetime.now().minute < 10
    has_signals = "No new trading signals" not in report_content

    if has_signals or is_heartbeat:
        # Build enriched message
        extra = (
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
        print("Sending notification...")
        send_telegram_message(full_message)
    else:
        print("No new signals and not heartbeat time. Skipping notification.")

if __name__ == "__main__":
    main()
