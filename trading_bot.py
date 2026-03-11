#!/usr/bin/env python3
import os
import time
import secrets
import jwt
import datetime
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

# Ethereum / Uniswap Config
ENABLE_ETHEREUM = os.environ.get("ENABLE_ETHEREUM", "false").lower() == "true"
ETH_RPC_URL = os.environ.get("ETH_RPC_URL")
ETH_PRIVATE_KEY = os.environ.get("ETH_PRIVATE_KEY")

# Strategy & Risk
SHORT_WINDOW = 20
LONG_WINDOW = 50
PORTFOLIO_RISK_PERCENTAGE = 0.15
RISK_PER_TRADE_PCT = 0.95 
STOP_LOSS_PCT = 0.05
ASSET_BLACKLIST = ["DOGE", "SHLD", "SHIB"]
MOMENTUM_WINDOW_HOURS = 24
TOP_MOMENTUM_COUNT = 3

# --- Globals ---
PRODUCT_DETAILS_CACHE = {}

# --- State Management ---
def load_state():
    default_state = {"entry_prices": {}}
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, 'r') as f: return json.load(f)
        except: return default_state
    return default_state

def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=2)
    except: pass

def update_entry_price(product_id, price):
    state = load_state()
    state.setdefault("entry_prices", {})[product_id] = price
    save_state(state)

def clear_entry_price(product_id):
    state = load_state()
    if product_id in state.get("entry_prices", {}):
        del state["entry_prices"][product_id]
        save_state(state)

# --- Authentication & API Requests ---
def get_credentials():
    with open(API_JSON_FILE, 'r') as f: data = json.load(f)
    return data.get('name'), data.get('privateKey')

def build_jwt(api_key_name, private_key_pem, service, uri):
    private_key = serialization.load_pem_private_key(private_key_pem.encode('utf-8'), password=None)
    jwt_payload = {"iss": "cdp", "nbf": int(time.time()), "exp": int(time.time()) + 120, "sub": api_key_name, "uri": f"{service} {uri}"}
    return jwt.encode(jwt_payload, private_key, algorithm="ES256", headers={"kid": api_key_name, "nonce": secrets.token_hex()})

import urllib.parse
def coinbase_request(method, path, body=None):
    try:
        api_key_name, private_key = get_credentials()
        host = "api.coinbase.com"
        path_for_jwt = urllib.parse.urlparse(path).path
        token = build_jwt(api_key_name, private_key, method, f"{host}{path_for_jwt}")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        response = requests.request(method.upper(), f"https://{host}{path}", headers=headers, json=body, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as e: logging.error(f"Coinbase request failed: {e}")
    return None

def get_product_details(product_id):
    if product_id in PRODUCT_DETAILS_CACHE: return PRODUCT_DETAILS_CACHE[product_id]
    data = coinbase_request("GET", f"/api/v3/brokerage/products/{product_id}")
    if data: PRODUCT_DETAILS_CACHE[product_id] = data
    return data

def round_to_increment(amount, increment):
    inc = Decimal(str(increment))
    amt = Decimal(str(amount))
    return (amt // inc) * inc

def cancel_open_orders(product_id):
    """Cancel all open orders for a specific product to free up balance."""
    logging.info(f"Cancelling open orders for {product_id}...")
    path = f"/api/v3/brokerage/orders/historical/batch?order_status=OPEN&product_id={product_id}"
    orders = coinbase_request("GET", path)
    if orders and "orders" in orders:
        order_ids = [o["order_id"] for o in orders["orders"]]
        if order_ids:
            logging.info(f"Cancelling {len(order_ids)} orders: {order_ids}")
            coinbase_request("POST", "/api/v3/brokerage/orders/batch_cancel", {"order_ids": order_ids})
            time.sleep(1)

def get_best_bid_ask(product_id):
    """Fetch the best bid and ask to ensure we place 'Maker' orders."""
    book = coinbase_request("GET", f"/api/v3/brokerage/product_book?product_id={product_id}&limit=1")
    if book and "pricebook" in book and book["pricebook"]["bids"] and book["pricebook"]["asks"]:
        best_bid = float(book["pricebook"]["bids"][0]["price"])
        best_ask = float(book["pricebook"]["asks"][0]["price"])
        return best_bid, best_ask
    return None, None

# --- Trading Logic ---
def place_limit_order(product_id, side, price, amount_quote_currency=None, amount_base_currency=None):
    if TRADING_MODE == "live":
        cancel_open_orders(product_id)
        
    details = get_product_details(product_id)
    if not details: return None
    
    best_bid, best_ask = get_best_bid_ask(product_id)
    tick = float(details['quote_increment'])
    
    # Maker-Optimized Pricing
    if side == 'BUY':
        price = min(price, best_ask - tick if best_ask else price)
    else:
        price = max(price, best_bid + tick if best_bid else price)

    order_id = str(uuid.uuid4())
    if side == 'BUY' and amount_quote_currency:
        base_size = float(amount_quote_currency) / float(price)
    else:
        base_size = amount_base_currency

    rounded_base = round_to_increment(base_size, details['base_increment'])
    rounded_price = round_to_increment(price, details['quote_increment'])

    payload = {
        "client_order_id": order_id, 
        "product_id": product_id, 
        "side": side, 
        "order_configuration": {
            "limit_limit_gtc": {
                "base_size": str(rounded_base),
                "limit_price": str(rounded_price),
                "post_only": True
            }
        }
    }
    
    logging.info(f"Placing LIMIT {side} (Post-Only) for {product_id} at {rounded_price}")
    if TRADING_MODE == "live":
        return coinbase_request("POST", "/api/v3/brokerage/orders", payload)
    return {"success": True}

def place_market_order(product_id, side, amount_quote_currency=None, amount_base_currency=None):
    if TRADING_MODE == "live":
        cancel_open_orders(product_id)
        
    details = get_product_details(product_id)
    if not details: return None
    order_id = str(uuid.uuid4())
    
    if side == 'BUY' and amount_quote_currency:
        base_size = float(amount_quote_currency) / float(details['price'])
    else:
        base_size = amount_base_currency

    rounded_base = round_to_increment(base_size, details['base_increment'])
    payload = {
        "client_order_id": order_id, 
        "product_id": product_id, 
        "side": side, 
        "order_configuration": {
            "market_market_ioc": {
                "quote_size": str(amount_quote_currency) if amount_quote_currency else "",
                "base_size": str(rounded_base)
            }
        }
    }
    
    logging.info(f"Placing MARKET {side} (IOC) for {product_id}")
    if TRADING_MODE == "live":
        return coinbase_request("POST", "/api/v3/brokerage/orders", payload)
    return {"success": True}

def get_all_balances():
    all_accounts = []
    path = "/api/v3/brokerage/accounts"
    while True:
        data = coinbase_request("GET", path)
        if not data: break
        all_accounts.extend(data['accounts'])
        if not data.get('has_next'): break
        path = f"/api/v3/brokerage/accounts?cursor={data['cursor']}"
    balances = {"cash": {"USD": 0.0, "USDC": 0.0}, "crypto": {}}
    for acc in all_accounts:
        cur, val = acc['currency'], float(acc['available_balance']['value'])
        if cur in balances['cash']: balances['cash'][cur] = val
        elif val > 0: balances['crypto'][cur] = val
    return balances

def get_market_data(product_id):
    path = f"/api/v3/brokerage/products/{product_id}/candles?limit={LONG_WINDOW + 10}&granularity=ONE_HOUR"
    data = coinbase_request("GET", path)
    if data and 'candles' in data:
        df = pd.DataFrame(data['candles'], columns=['start', 'low', 'high', 'open', 'close', 'volume'])
        df['start'] = pd.to_datetime(df['start'], unit='s')
        df[df.columns[1:]] = df[df.columns[1:]].apply(pd.to_numeric)
        return df.sort_values(by='start')
    return None

def analyze_trend(df):
    if df is None or len(df) < LONG_WINDOW: return None, None
    s_ma = df['close'].rolling(window=SHORT_WINDOW).mean().iloc[-1]
    l_ma = df['close'].rolling(window=LONG_WINDOW).mean().iloc[-1]
    return s_ma, l_ma

def get_momentum_ranking(df):
    if df is None or len(df) < MOMENTUM_WINDOW_HOURS + 1: return 0.0
    curr = df['close'].iloc[-1]
    hist = df['close'].iloc[-(MOMENTUM_WINDOW_HOURS + 1)]
    return ((curr - hist) / hist) * 100 if hist != 0 else 0.0

def is_asset_blacklisted(asset):
    return asset.upper() in [a.upper() for a in ASSET_BLACKLIST]

def run_bot(reset_to_usdc=False):
    logging.info(f"--- 🤖 Crypto Bot Run ({TRADING_MODE.upper()}) ---")
    LAST_RUN_FILE = "/home/salhashemi2/.openclaw/workspace/trading-bot/last_run.txt"
    os.makedirs(os.path.dirname(LAST_RUN_FILE), exist_ok=True)
    with open(LAST_RUN_FILE, "w") as f: f.write(datetime.datetime.now().isoformat())

    balances = get_all_balances()
    cash, held, state = balances["cash"], balances["crypto"], load_state()
    
    total_value = sum(cash.values())
    for cur, amt in held.items():
        details = get_product_details(f"{cur}-USDC")
        if details: total_value += amt * float(details['price'])

    trade_limit = total_value * PORTFOLIO_RISK_PERCENTAGE
    btc_df = get_market_data("BTC-USDC")
    btc_s, btc_l = analyze_trend(btc_df)
    btc_trend = "BEAR" if btc_s and btc_l and btc_s < btc_l else "BULL"
    logging.info(f"Market: {btc_trend}. Portfolio: ${total_value:,.2f}")

    available_usdc = cash["USDC"]

    if reset_to_usdc:
        for asset, amount in held.items():
            if asset in ["USD", "USDC"]: continue
            if is_asset_blacklisted(asset): continue
            place_market_order(f"{asset}-USDC", 'SELL', amount_base_currency=amount)
            clear_entry_price(f"{asset}-USDC")
        return

    asset_candidates = []
    for asset in set(list(held.keys()) + ["BTC", "ETH", "SOL"]):
        if asset in ["USD", "USDC"] or is_asset_blacklisted(asset): continue
        product_id = f"{asset}-USDC"
        try:
            df = get_market_data(product_id)
            ma_s, ma_l = analyze_trend(df)
            if ma_s and ma_l and ma_s > ma_l * 1.002 and (btc_trend == "BULL" or asset == "BTC"):
                asset_candidates.append({"asset": asset, "product_id": product_id, "momentum": get_momentum_ranking(df)})
        except Exception as e: logging.error(f"Error analyzing {asset}: {e}")
    
    asset_candidates.sort(key=lambda x: x["momentum"], reverse=True)
    
    for candidate in asset_candidates[:TOP_MOMENTUM_COUNT]:
        asset, product_id = candidate["asset"], candidate["product_id"]
        try:
            price = float(get_product_details(product_id)['price'])
            buy_size = min(available_usdc * RISK_PER_TRADE_PCT / TOP_MOMENTUM_COUNT, trade_limit / TOP_MOMENTUM_COUNT)
            if buy_size > 10:
                if place_limit_order(product_id, 'BUY', price, amount_quote_currency=buy_size):
                    update_entry_price(product_id, price)
                    available_usdc -= buy_size
        except: pass
    
    for asset in list(held.keys()):
        if asset in ["USD", "USDC"]: continue
        product_id = f"{asset}-USDC"
        try:
            price = float(get_product_details(product_id)['price'])
            entry = state.get("entry_prices", {}).get(product_id)
            if entry and price < entry * (1 - STOP_LOSS_PCT):
                place_limit_order(product_id, 'SELL', price, amount_base_currency=held[asset])
                clear_entry_price(product_id)
                continue
            
            df = get_market_data(product_id)
            ma_s, ma_l = analyze_trend(df)
            if ma_s and ma_l and ma_s < ma_l * 0.998:
                place_limit_order(product_id, 'SELL', price, amount_base_currency=held[asset] * 0.5)
        except: pass

if __name__ == "__main__":
    run_bot(reset_to_usdc="--reset" in sys.argv)
