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

_HOME = os.path.expanduser("~")
LOG_FILE = os.environ.get("TRADING_LOG_FILE", os.path.join(_HOME, ".openclaw", "workspace", "trading-bot", "trading.log"))
API_JSON_FILE = os.environ.get("COINBASE_API_JSON", os.path.join(_HOME, "cdb_api_key.json"))
REPORT_FILE = os.environ.get("TRADING_REPORT_FILE", os.path.join(_HOME, ".openclaw", "workspace", "trading-bot", "report.txt"))

def get_credentials():
    with open(API_JSON_FILE, 'r') as f: data = json.load(f)
    return data.get('name'), data.get('privateKey')

def build_jwt(name, key, service, uri):
    pk = serialization.load_pem_private_key(key.encode('utf-8'), None)
    payload = {"iss":"cdp","nbf":int(time.time()),"exp":int(time.time())+120,"sub":name,"uri":f"{service} {uri}"}
    return jwt.encode(payload, pk, "ES256", headers={"kid":name,"nonce":secrets.token_hex()})

def coinbase_request(method, path):
    try:
        name, key = get_credentials()
        host = "api.coinbase.com"
        token = build_jwt(name, key, method, f"{host}{urllib.parse.urlparse(path).path}")
        return requests.get(f"https://{host}{path}", headers={"Authorization":f"Bearer {token}"}, timeout=15).json()
    except: return None

def get_current_price(pid):
    try: return float(coinbase_request("GET", f"/api/v3/brokerage/products/{pid}")['price'])
    except: return None

def parse_logs_for_signals():
    if not os.path.exists(LOG_FILE): return []
    with open(LOG_FILE, 'r', encoding='utf-8', errors='ignore') as f: full = f.read()
    marker = "--- 🤖 Crypto Bot Run"
    idx = full.rfind(marker)
    if idx == -1: return []
    lines = full[idx:].strip().split('\n')
    signals = []
    reg = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?SIGNAL (BUY|SELL): \$?([\d,.]+) (?:of )?([\w-]+)")
    for i, line in enumerate(lines):
        m = reg.search(line)
        if m:
            ts, stype, amt_str, asset_raw = m.groups()
            asset = asset_raw.split("-")[0]
            price = None
            for j in range(i-1, max(0, i-40), -1):
                if f"{asset}-USDC: Price=" in lines[j]:
                    try:
                        price = float(lines[j].split("Price=$")[-1].split(", MA")[0].replace(",", "").strip())
                        break
                    except: continue
            if price: signals.append({"asset":asset, "type":stype, "amount":float(amt_str.replace(",","")), "price":price, "ts":ts})
    return signals



def get_all_balances():
    all_accounts = []
    path = "/api/v3/brokerage/accounts"
    while True:
        data = coinbase_request("GET", path)
        if not data: break
        all_accounts.extend(data["accounts"])
        if not data.get("has_next"): break
        path = f"/api/v3/brokerage/accounts?cursor={data["cursor"]}"
    
    balances = {"cash": {"USD": 0.0, "USDC": 0.0}, "crypto": {}}
    for acc in all_accounts:
        cur, val = acc["currency"], float(acc["available_balance"]["value"])
        if cur in balances["cash"]: balances["cash"][cur] = val
        elif val > 0: balances["crypto"][cur] = val
    return balances


def generate_report(signals):
    report = ["--- 🤖 Trading Bot Report ---"]
    status = "Unknown"
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            lines_log = f.readlines()
            for line in reversed(lines_log[-100:]):
                # Look for "Market Regime:" in log - supports both dual-signal and single-asset formats
                # Dual-signal: "Market Regime: STRONG_BULL (BTC: BULL | Rotation: ETH_LEADING)"
                # Single-asset: "Market Regime (BTC): BULL"
                if "Market Regime:" in line and "Portfolio" not in line and "===" not in line:
                    # Extract everything after "Market Regime:"
                    status = line.split("Market Regime:", 1)[1].strip()
                    break
    report.append(f"Market Status: {status}\n")
    
    balances = get_all_balances()
    cash = balances["cash"]
    held = balances["crypto"]
    
    # Known manual offset for Coinbase Stocks (not in API)
    stock_value = 2500.0
    total_usdc_value = cash["USD"] + cash["USDC"] + stock_value
    crypto_details = []
    
    for cur, amt in held.items():
        price_data = get_current_price(f"{cur}-USDC")
        if price_data:
            val = amt * price_data
            total_usdc_value += val
            crypto_details.append(f"{cur}: {amt:.4f} (${val:,.2f})")
    
    report.append(f"TOTAL PORTFOLIO VALUE: {total_usdc_value:,.2f} USDC (includes ~$2,500 in Stocks)")
    report.append(f"  - Cash: ${(cash["USD"] + cash["USDC"]):,.2f} (USD: ${cash["USD"]:,.2f}, USDC: ${cash["USDC"]:,.2f})")
    if crypto_details:
        report.append(f"  - Crypto: " + ", ".join(crypto_details))
    report.append("")
    
    if not signals:
        report.append("No new trading signals in the last run.")
    else:
        total = 0.0
        for s in signals:
            cur = get_current_price(f"{s["asset"]}-USDC")
            if cur:
                pct = ((cur - s["price"]) / s["price"]) * 100
                qty = s["amount"] / s["price"]
                dpl = (cur - s["price"]) * qty
                if s["type"] == "SELL": pct, dpl = -pct, -dpl
                total += dpl
                report.append(f"  - {s["type"]} {s["asset"]}: Signal ${s["price"]:,.2f}, Now ${cur:,.2f}. P/L: {"📈" if dpl>=0 else "📉"} {pct:+.2f}% (${dpl:+.2f})")
        if len(signals) > 1:
            report.append(f"\nTotal Run P/L: {"✅" if total>=0 else "❌"} ${total:+.2f}")
            
    with open(REPORT_FILE, "w") as f:
        f.write("\n".join(report))

if __name__ == "__main__": generate_report(parse_logs_for_signals())
