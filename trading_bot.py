#!/usr/bin/env python3
import os
import time
import secrets
import jwt
import requests
import pandas as pd
from cryptography.hazmat.primitives import serialization
import uuid
import json
import logging
import sys
from pathlib import Path
from decimal import Decimal, ROUND_DOWN

# --- Logging Configuration ---
LOG_FILE = "/home/salhashemi2/.openclaw/workspace/trading-bot/trading.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)

# --- Configuration ---
API_JSON_FILE = os.environ.get("COINBASE_API_JSON", "/home/salhashemi2/cdb_api_key.json")
STATE_FILE = Path("/home/salhashemi2/trading-bot-flake/trading_state.json")
TRADING_MODE = os.environ.get("TRADING_MODE", "paper").lower()

# Strategy & Risk
SHORT_WINDOW = 20
LONG_WINDOW = 50
PORTFOLIO_RISK_PERCENTAGE = 0.15
RISK_PER_TRADE_PCT = 0.95 
STOP_LOSS_PCT = 0.05 # 5% Stop Loss

# --- Globals ---
PRODUCT_DETAILS_CACHE = {}

# --- State Management ---
def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, 'r') as f: return json.load(f)
        except json.JSONDecodeError:
            return {"entry_prices": {}}
    return {"entry_prices": {}}

def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=2)
    except Exception as e:
        logging.error(f"Failed to save state: {e}")

def update_entry_price(product_id, price):
    state = load_state()
    if "entry_prices" not in state: state["entry_prices"] = {}
    state["entry_prices"][product_id] = price
    save_state(state)

def clear_entry_price(product_id):
    state = load_state()
    if "entry_prices" in state and product_id in state["entry_prices"]:
        del state["entry_prices"][product_id]
        save_state(state)

# --- Authentication & API Requests ---
def get_credentials():
    with open(API_JSON_FILE, 'r') as f: data = json.load(f)
    return data.get('name'), data.get('privateKey')

def build_jwt(api_key_name, private_key_pem, service, uri):
    private_key = serialization.load_pem_private_key(private_key_pem.encode('utf-8'), password=None)
    jwt_payload = {
        "iss": "cdp", "nbf": int(time.time()), "exp": int(time.time()) + 120,
        "sub": api_key_name, "uri": f"{service} {uri}"
    }
    return jwt.encode(jwt_payload, private_key, algorithm="ES256", headers={"kid": api_key_name, "nonce": secrets.token_hex()})

import urllib.parse

def coinbase_request(method, path, body=None):
    try:
        api_key_name, private_key = get_credentials()
        host = "api.coinbase.com"
        full_request_uri = f"https://{host}{path}"
        path_for_jwt = urllib.parse.urlparse(path).path
        jwt_uri_suffix = f"{host}{path_for_jwt}"
        token = build_jwt(api_key_name, private_key, method, jwt_uri_suffix)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        response = requests.request(method.upper(), full_request_uri, headers=headers, json=body, timeout=15)

        if response.status_code >= 400:
            logging.error(f"API Error on {method} {path}: {response.status_code} - Response: {response.text}")
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"Request failed: {e}", exc_info=True)
    return None

# --- Precision & Product Details ---
def get_product_details(product_id):
    if product_id in PRODUCT_DETAILS_CACHE:
        return PRODUCT_DETAILS_CACHE[product_id]
    
    path = f"/api/v3/brokerage/products/{product_id}"
    data = coinbase_request("GET", path)
    if data:
        PRODUCT_DETAILS_CACHE[product_id] = data
        return data
    return None

def round_to_increment(amount, increment):
    """Rounds the amount down to the nearest valid increment."""
    increment_dec = Decimal(str(increment))
    amount_dec = Decimal(str(amount))
    return (amount_dec // increment_dec) * increment_dec

# --- Order Placement ---
def place_market_order(product_id, side, amount_quote_currency=None, amount_base_currency=None):
    details = get_product_details(product_id)
    if not details:
        logging.error(f"Could not get product details for {product_id}. Halting order.")
        return None

    order_id = str(uuid.uuid4())
    order_config = {}

    if side == 'BUY':
        quote_increment = details['quote_increment']
        rounded_amount = round_to_increment(amount_quote_currency, quote_increment)
        order_config = {"market_market_ioc": {"quote_size": str(rounded_amount)}}
    elif side == 'SELL':
        base_increment = details['base_increment']
        rounded_amount = round_to_increment(amount_base_currency, base_increment)
        order_config = {"market_market_ioc": {"base_size": str(rounded_amount)}}

    payload = {"client_order_id": order_id, "product_id": product_id, "side": side, "order_configuration": order_config}
    logging.info(f"Placing {side} Order for {product_id} with rounded payload: {json.dumps(payload)}")

    if TRADING_MODE == "live":
        try:
            response = coinbase_request("POST", "/api/v3/brokerage/orders", payload)
            logging.info(f"Order placement response for {product_id}: {response}")
            return response
        except Exception as e:
            logging.error(f"Failed to place order: {e}")
            return None
    else:
        logging.info("[PAPER TRADE] would have placed order.")
        return {"success": True, "order_id": "MOCK_ORDER_ID"}

# --- Account & Trading Logic ---
def get_all_balances():
    all_accounts = []
    path = "/api/v3/brokerage/accounts"
    while True:
        data = coinbase_request("GET", path)
        if data and 'accounts' in data:
            all_accounts.extend(data['accounts'])
            if data.get('has_next'):
                cursor = data.get('cursor')
                path = f"/api/v3/brokerage/accounts?cursor={cursor}"
            else:
                break
        else:
            break

    balances = {"cash": {"USD": 0.0, "USDC": 0.0}, "crypto": {}}
    for acc in all_accounts:
        balance = float(acc['available_balance']['value'])
        currency = acc['currency']
        if currency in balances['cash']: balances['cash'][currency] = balance
        elif balance > 0: balances['crypto'][currency] = balance
    return balances

def get_current_price(product_id):
    data = get_product_details(product_id)
    return float(data['price']) if data else None
    
def get_market_data(product_id):
    path = f"/api/v3/brokerage/products/{product_id}/candles?limit={LONG_WINDOW + 10}&granularity=ONE_HOUR"
    data = coinbase_request("GET", path)
    if data and 'candles' in data:
        df = pd.DataFrame(data['candles'], columns=['start', 'low', 'high', 'open', 'close', 'volume'])
        df['start'] = pd.to_datetime(df['start'], unit='s')
        df[df.columns[1:]] = df[df.columns[1:]].apply(pd.to_numeric)
        return df.sort_values(by='start', ascending=True)
    return None

def analyze_trend(df):
    """Calculates MAs and returns short and long MA values."""
    if df is None or len(df) < LONG_WINDOW: return None, None
    df[f'MA_{SHORT_WINDOW}'] = df['close'].rolling(window=SHORT_WINDOW).mean()
    df[f'MA_{LONG_WINDOW}'] = df['close'].rolling(window=LONG_WINDOW).mean()
    last = df.iloc[-1]
    return last.get(f'MA_{SHORT_WINDOW}'), last.get(f'MA_{LONG_WINDOW}')

def run_bot():
    logging.info(f"--- 🤖 Starting Crypto Bot Run (Mode: {TRADING_MODE.upper()}) ---")
    balances = get_all_balances()
    cash = balances["cash"]
    held = balances["crypto"]
    state = load_state()
    
    usd_balance = cash.get("USD", 0.0)
    if usd_balance > 1.0:
        logging.warning(f"Detected USD balance of \${usd_balance:.2f}. Please convert to USDC in the UI manually.")

    total_value = sum(cash.values())
    for currency, amount in held.items():
        price = get_current_price(f"{currency}-USDC")
        if price: total_value += amount * price

    trade_limit = total_value * PORTFOLIO_RISK_PERCENTAGE
    logging.info(f"Portfolio Value: ~\${total_value:,.2f}. Trade Limit: \${trade_limit:,.2f}")

    # --- Hedging / Market Filter ---
    # Check BTC trend first to determine market regime
    btc_trend = "NEUTRAL"
    try:
        btc_df = get_market_data("BTC-USDC")
        btc_short, btc_long = analyze_trend(btc_df)
        
        if btc_short and btc_long:
            if btc_short < btc_long:
                btc_trend = "BEAR"
                logging.info("📉 Market Regime: BEARISH (BTC MA20 < MA50). Buying restricted.")
            else:
                btc_trend = "BULL"
                logging.info("📈 Market Regime: BULLISH (BTC MA20 > MA50).")
    except Exception as e:
        logging.error(f"Failed to analyze BTC trend: {e}")

    assets = list(held.keys()) + ["BTC", "ETH", "SOL"]
    for asset in set(assets):
        if asset in ["USD", "USDC"]: continue
        
        product_id = f"{asset}-USDC"
        try:
            price = get_current_price(product_id)
            if not price: continue

            # --- Stop Loss Check ---
            entry_price = state["entry_prices"].get(product_id)
            if entry_price and price < entry_price * (1 - STOP_LOSS_PCT):
                held_amount = held.get(asset, 0.0)
                if held_amount * price > 5: # Min sell
                    logging.warning(f"🛑 STOP LOSS TRIGGERED for {product_id}! Entry: {entry_price}, Current: {price}")
                    place_market_order(product_id, 'SELL', amount_base_currency=held_amount)
                    clear_entry_price(product_id)
                    continue # Skip other logic for this asset

            df = get_market_data(product_id)
            ma_short, ma_long = analyze_trend(df)
            
            if ma_short is None or ma_long is None: continue

            logging.info(f"{product_id}: Price=\${price:,.2f}, MA({SHORT_WINDOW})=\${ma_short:.2f}, MA({LONG_WINDOW})=\${ma_long:.2f}")

            if ma_short > ma_long and cash.get("USDC", 0) > 10:
                # HEDGING LOGIC: Don't buy alts if BTC is bearish
                if btc_trend == "BEAR" and asset != "BTC":
                    logging.info(f"Skipping BUY signal for {asset} due to Bearish Market Regime.")
                    continue

                buy_size = min(cash["USDC"] * RISK_PER_TRADE_PCT, trade_limit)
                if buy_size > 10: # Min order check
                    logging.info(f"SIGNAL BUY: \${buy_size:,.2f} of {asset} using USDC")
                    order = place_market_order(product_id, 'BUY', amount_quote_currency=buy_size)
                    if order:
                        update_entry_price(product_id, price)
            
            elif ma_short < ma_long:
                held_amount = held.get(asset, 0.0)
                if held_amount * price > 10: # Min order check
                    sell_size = held_amount * 0.5
                    logging.info(f"SIGNAL SELL: {sell_size} of {asset}")
                    place_market_order(product_id, 'SELL', amount_base_currency=sell_size)
                    # Note: We don't clear entry price on partial sell usually
        except Exception as e:
            logging.error(f"Error processing {asset}: {e}")

    logging.info("--- Run Complete ---")

if __name__ == "__main__":
    run_bot()
