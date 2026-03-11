#!/usr/bin/env python3
import os
import requests
import sys
from datetime import datetime

# --- Configuration ---
REPORT_FILE = "/home/salhashemi2/.openclaw/workspace/trading-bot/report.txt"
TELEGRAM_TOKEN_FILE = "/run/secrets/telegram_bot_token"
CHAT_ID = "8555669756"

def send_telegram_message(message):
    if not os.path.exists(TELEGRAM_TOKEN_FILE):
        print(f"Error: Token file not found at {TELEGRAM_TOKEN_FILE}")
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
        print(f"Failed to send notification: {e}")

def main():
    print(f"[{datetime.now()}] Notify script running...")
    if not os.path.exists(REPORT_FILE):
        print(f"Report file not found: {REPORT_FILE}")
        return

    with open(REPORT_FILE, 'r') as f:
        report_content = f.read().strip()

    # Logic: Only notify if there are signals OR if it's the 8:00 AM run (daily heartbeat)
    is_heartbeat = datetime.now().hour == 8 and datetime.now().minute < 10
    has_signals = "No new trading signals" not in report_content

    if has_signals or is_heartbeat:
        print("Sending notification...")
        send_telegram_message(f"<pre>{report_content}</pre>")
    else:
        print("No new signals and not heartbeat time. Skipping notification.")

if __name__ == "__main__":
    main()
