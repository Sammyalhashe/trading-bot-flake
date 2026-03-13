#!/usr/bin/env python3
import os
import time
import datetime
import pandas as pd
import json
import logging
import sys
from pathlib import Path
from decimal import Decimal

# Import specialized executors
from coinbase_executor import CoinbaseExecutor
from ethereum_executor import EthereumExecutor

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

ASSET_MAPPING = {
    "MATIC": "POL",      # MATIC rebranded to POL on Coinbase
    "ETH_NATIVE": "ETH", # For pricing native ETH
    "USDC.e": "USDC",    # For pricing bridged USDC
}

# --- Helpers ---
def get_data_product_id(asset):
    mapped = ASSET_MAPPING.get(asset.upper(), asset.upper())
    return f"{mapped}-USDC"

def round_to_increment(amount, increment):
    inc = Decimal(str(increment))
    amt = Decimal(str(amount))
    return (amt // inc) * inc

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

def update_entry_price(executor_id, product_id, price):
    state = load_state()
    key = f"{executor_id}:{product_id}"
    state.setdefault("entry_prices", {})[key] = price
    save_state(state)

def clear_entry_price(executor_id, product_id):
    state = load_state()
    key = f"{executor_id}:{product_id}"
    if key in state.get("entry_prices", {}):
        del state["entry_prices"][key]
        save_state(state)

def is_asset_blacklisted(asset):
    return asset.upper() in [a.upper() for a in ASSET_BLACKLIST]

# --- Strategy Logic ---
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

def run_executor_strategy(executor, data_provider, btc_trend, reset_to_usdc=False):
    ex_id = executor.__class__.__name__
    if hasattr(executor, 'account') and executor.account:
        ex_id = f"{ex_id}_{executor.account.address[:6]}"
        
    bal = executor.get_balances()
    cash = bal["cash"].get("USDC", 0.0)
    held = bal["crypto"]
    state = load_state()
    
    # Calculate local value
    ex_value = cash
    for asset, amt in held.items():
        details = data_provider.get_product_details(get_data_product_id(asset))
        if details: ex_value += amt * float(details['price'])
    
    logging.info(f"[{ex_id}] Sub-Portfolio Value: ${ex_value:,.2f} | USDC: ${cash:,.2f}")
    
    if reset_to_usdc:
        for asset, amount in held.items():
            if asset in ["USD", "USDC"] or is_asset_blacklisted(asset): continue
            executor.place_market_order(get_data_product_id(asset), 'SELL', amount_base_currency=amount)
            clear_entry_price(ex_id, get_data_product_id(asset))
        return ex_value

    # Scan for buys
    trade_limit = ex_value * PORTFOLIO_RISK_PERCENTAGE
    asset_candidates = []
    # Major assets supported
    for asset in ["BTC", "ETH", "MATIC"]:
        if is_asset_blacklisted(asset): continue
        product_id = get_data_product_id(asset)
        try:
            df = data_provider.get_market_data(product_id, LONG_WINDOW)
            ma_s, ma_l = analyze_trend(df)
            if ma_s and ma_l and ma_s > ma_l * 1.002 and (btc_trend == "BULL" or asset == "BTC"):
                asset_candidates.append({"asset": asset, "product_id": product_id, "momentum": get_momentum_ranking(df)})
        except Exception as e: logging.error(f"[{ex_id}] Error analyzing {asset}: {e}")
    
    asset_candidates.sort(key=lambda x: x["momentum"], reverse=True)
    
    available_usdc = cash
    for candidate in asset_candidates[:TOP_MOMENTUM_COUNT]:
        asset, product_id = candidate["asset"], candidate["product_id"]
        try:
            price_data = data_provider.get_product_details(product_id)
            price = float(price_data['price'])
            buy_size = min(available_usdc * RISK_PER_TRADE_PCT / TOP_MOMENTUM_COUNT, trade_limit / TOP_MOMENTUM_COUNT)
            if buy_size > 10:
                if executor.place_limit_order(product_id, 'BUY', price, amount_quote_currency=buy_size):
                    update_entry_price(ex_id, product_id, price)
                    available_usdc -= buy_size
        except: pass

    # Manage Sells
    for asset, amt in held.items():
        if asset in ["USD", "USDC"]: continue
        product_id = get_data_product_id(asset)
        try:
            price_data = data_provider.get_product_details(product_id)
            if not price_data: continue
            price = float(price_data['price'])
            
            entry_key = f"{ex_id}:{product_id}"
            entry = state.get("entry_prices", {}).get(entry_key)
            
            sell_trigger = False
            sell_ratio = 1.0
            
            if entry and price < entry * (1 - STOP_LOSS_PCT):
                sell_trigger = True
                logging.info(f"[{ex_id}] Stop-loss triggered for {asset}")
            else:
                df = data_provider.get_market_data(product_id, LONG_WINDOW)
                ma_s, ma_l = analyze_trend(df)
                if ma_s and ma_l and ma_s < ma_l * 0.998:
                    sell_trigger = True
                    sell_ratio = 0.5
                    logging.info(f"[{ex_id}] Trend-exit (50%) triggered for {asset}")
            
            if sell_trigger:
                if executor.place_limit_order(product_id, 'SELL', price, amount_base_currency=amt * sell_ratio):
                    if sell_ratio == 1.0:
                        clear_entry_price(ex_id, product_id)
        except: pass
        
    return ex_value

def run_bot(reset_to_usdc=False):
    cb_executor = CoinbaseExecutor(API_JSON_FILE, TRADING_MODE)
    active_executors = [cb_executor]
    
    if ENABLE_ETHEREUM:
        # Prioritise BASE_RPC_URL for Base network
        rpc_url = os.environ.get("BASE_RPC_URL") or os.environ.get("ETH_RPC_URL")
        private_key = os.environ.get("ETH_PRIVATE_KEY")
        
        # If not in env, check secrets
        if not rpc_url:
            for path in ["/run/secrets/base_rpc_url", "/run/secrets/eth_rpc_url"]:
                if os.path.exists(path):
                    with open(path, 'r') as f: rpc_url = f.read().strip(); break
        
        # Absolute fallback to Base Mainnet if still not set or if it's pointing to something invalid
        if not rpc_url or "gashawk" in rpc_url:
            rpc_url = "https://mainnet.base.org"
            
        if not private_key:
            for path in ["/run/secrets/eth_private_key"]:
                if os.path.exists(path):
                    with open(path, 'r') as f: private_key = f.read().strip(); break
        
        if rpc_url and private_key:
            try:
                active_executors.append(EthereumExecutor(rpc_url, private_key, TRADING_MODE))
            except Exception as e: logging.error(f"Failed to init EthereumExecutor: {e}")

    logging.info(f"--- 🤖 Crypto Bot Run ({TRADING_MODE.upper()}) ---")
    data_provider = cb_executor
    
    # Global Trend
    btc_df = data_provider.get_market_data(get_data_product_id("BTC"), LONG_WINDOW)
    btc_s, btc_l = analyze_trend(btc_df)
    btc_trend = "BEAR" if btc_s and btc_l and btc_s < btc_l else "BULL"
    logging.info(f"Market Regime: {btc_trend}")

    aggregate_value = 0
    for ex in active_executors:
        try:
            aggregate_value += run_executor_strategy(ex, data_provider, btc_trend, reset_to_usdc)
        except Exception as e:
            logging.error(f"Strategy failed for {ex.__class__.__name__}: {e}")

    logging.info(f"--- Aggregate Portfolio Total: ${aggregate_value:,.2f} ---")

if __name__ == "__main__":
    run_bot(reset_to_usdc="--reset" in sys.argv)
