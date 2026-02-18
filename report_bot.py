#!/usr/bin/env python3
import os
import re
import json
import time
import secrets
import jwt
import requests
import urllib.parse
from datetime import datetime, timedelta
from cryptography.hazmat.primitives import serialization

# --- Configuration ---
LOG_FILE = "/home/salhashemi2/.openclaw/workspace/trading-bot/trading.log"
API_JSON_FILE = "/home/salhashemi2/cdb_api_key.json"
REPORT_FILE = "/home/salhashemi2/.openclaw/workspace/trading-bot/report.txt"

# --- Authentication & API Logic ---
def get_credentials():
    if not os.path.exists(API_JSON_FILE):
        raise FileNotFoundError(f"API credentials file not found at {API_JSON_FILE}")
    with open(API_JSON_FILE, 'r') as f:
        data = json.load(f)
    api_key_name = data.get('name')
    private_key_pem = data.get('privateKey')
    if not api_key_name or not private_key_pem:
        raise ValueError("Error: 'name' or 'privateKey' not found in JSON file.")
    return api_key_name, private_key_pem

def build_jwt(api_key_name, private_key_pem, service, uri):
    private_key = serialization.load_pem_private_key(private_key_pem.encode('utf-8'), password=None)
    jwt_payload = {
        "iss": "cdp", "nbf": int(time.time()), "exp": int(time.time()) + 120,
        "sub": api_key_name, "uri": f"{service} {uri}"
    }
    return jwt.encode(jwt_payload, private_key, algorithm="ES256", headers={"kid": api_key_name, "nonce": secrets.token_hex()})

def coinbase_request(method, path, body=None):
    try:
        api_key_name, private_key = get_credentials()
        host = "api.coinbase.com"
        full_request_uri = f"https://{host}{path}"
        path_for_jwt = urllib.parse.urlparse(path).path
        jwt_uri_suffix = f"{host}{path_for_jwt}"
        token = build_jwt(api_key_name, private_key, method, jwt_uri_suffix)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        response = requests.get(full_request_uri, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None

def get_current_price(product_id):
    path = f"/api/v3/brokerage/products/{product_id}"
    data = coinbase_request("GET", path)
    return float(data['price']) if data and 'price' in data else None

# --- Main Reporting Logic ---

def get_latest_log_run_lines():
    """
    Reads the entire log file and returns the lines corresponding to the
    most recent complete run.
    """
    if not os.path.exists(LOG_FILE):
        return []

    with open(LOG_FILE, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    
    # Find the start of the last run
    last_run_start_index = content.rfind("--- 🤖 Starting Crypto Bot Run ---")
    if last_run_start_index == -1:
        return []
    
    # Get the content of the last run
    last_run_content = content[last_run_start_index:]
    return last_run_content.strip().split('\n')


def parse_logs_for_signals():
    """Parses the most recent run from the log file for trade signals."""
    lines = get_latest_log_run_lines()
    if not lines:
        return []

    signals = []
    
    # Regex to find any signal line from the log
    signal_regex = re.compile(r"INFO - SIGNAL (BUY|SELL): ([\d.]+) (?:of )?([\w]+)")

    for line in lines:
        match = signal_regex.search(line)
        if match:
            signal_type, amount, asset = match.groups()
            
            # Find the price for this asset just before the signal
            price_at_signal = None
            
            # Get the timestamp of the signal line to search backwards
            try:
                line_timestamp_str = line.split(',')[0]
                line_dt = datetime.strptime(line_timestamp_str, "%Y-%m-%d %H:%M:%S")
            except (ValueError, IndexError):
                continue # Skip if the line format is unexpected

            # Search for the price log entry immediately preceding this signal
            for sub_line in reversed(lines[:lines.index(line)]):
                if f"INFO - {asset}-USD: Price=" in sub_line:
                    try:
                        price_str = sub_line.split('$')[-1]
                        price_at_signal = float(price_str)
                        break # Found the most recent price
                    except (ValueError, IndexError):
                        continue
            
            if price_at_signal:
                signals.append({
                    "asset": asset,
                    "type": signal_type,
                    "amount": float(amount),
                    "price_at_signal": price_at_signal
                })
                
    return signals


def generate_report(signals):
    """Generates and sends a performance report."""
    report_lines = ["--- 🤖 Trading Bot Report (Last 24H) ---"]
    
    if not signals:
        report_lines.append("\nNo actionable trading signals found in the logs.")
        with open(REPORT_FILE, 'w') as f:
            f.write("\n".join(report_lines))
        return

    total_performance = 0.0
    for signal in signals:
        asset = signal['asset']
        product_id = f"{asset}-USD"
        current_price = get_current_price(product_id)
        
        if current_price is None:
            report_lines.append(f"Could not fetch current price for {asset}.")
            continue

        price_at_signal = signal['price_at_signal']
        amount = signal['amount']
        signal_type = signal['type']
        
        performance = 0.0
        if signal_type == "BUY":
            performance = (current_price - price_at_signal) * amount
            change_char = "📈" if performance >= 0 else "📉"
            report_lines.append(f"  - BUY {asset}: Signal ${price_at_signal:,.2f}, Now ${current_price:,.2f}. P/L: {change_char} ${performance:,.2f}")
        
        elif signal_type == "SELL":
            performance = (price_at_signal - current_price) * amount
            change_char = "📈" if performance >= 0 else "📉"
            report_lines.append(f"  - SELL {asset}: Signal ${price_at_signal:,.2f}, Now ${current_price:,.2f}. Avoided Loss: {change_char} ${performance:,.2f}")
        
        total_performance += performance

    report_lines.append("\n---------------------------------------------------------")
    final_change_char = "✅" if total_performance >= 0 else "❌"
    report_lines.append(f"Total Estimated P/L: {final_change_char} ${total_performance:,.2f}")
    report_lines.append("---------------------------------------------------------")
    report_lines.append("\nDisclaimer: Paper trading summary. No real trades executed.")
    
    with open(REPORT_FILE, 'w') as f:
        f.write("\n".join(report_lines))

if __name__ == "__main__":
    signals = parse_logs_for_signals()
    generate_report(signals)