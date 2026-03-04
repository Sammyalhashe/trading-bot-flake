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

# Ethereum / Uniswap Config
ENABLE_ETHEREUM = os.environ.get("ENABLE_ETHEREUM", "false").lower() == "true"
ETH_RPC_URL = os.environ.get("ETH_RPC_URL") # GasHawk or Alchemy
ETH_PRIVATE_KEY = os.environ.get("ETH_PRIVATE_KEY")
ETH_MAX_GAS_PRICE_GWEI = float(os.environ.get("ETH_MAX_GAS_PRICE_GWEI", 50))
ETH_TRADE_AMOUNT_WEI = int(os.environ.get("ETH_TRADE_AMOUNT_WEI", 10000000000000000)) # Default 0.01 ETH

# Mainnet Defaults
UNISWAP_ROUTER_ADDRESS = os.environ.get("UNISWAP_ROUTER_ADDRESS", "0xE592427A0AEce92De3Edee1F18E0157C05861564")
WETH_ADDRESS = os.environ.get("WETH_ADDRESS", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
USDC_ADDRESS = os.environ.get("USDC_ADDRESS", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")

# Strategy & Risk
SHORT_WINDOW = 20
LONG_WINDOW = 50
PORTFOLIO_RISK_PERCENTAGE = 0.15
RISK_PER_TRADE_PCT = 0.95 
STOP_LOSS_PCT = 0.05 # 5% Stop Loss

# Blacklist assets (won't trade or buy)
ASSET_BLACKLIST = ["DOGE", "SHLD", "SHIB"]  # User doesn't like DOGE

# Momentum Filter Configuration
MOMENTUM_WINDOW_HOURS = 24  # Look back 24 hours for momentum
TOP_MOMENTUM_COUNT = 3      # Only buy top 3 momentum assets

# --- Globals ---
PRODUCT_DETAILS_CACHE = {}

# --- State Management ---
def load_state():
    default_state = {"entry_prices": {}}
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, 'r') as f: 
                raw = json.load(f)
                if raw and "entry_prices" not in raw:
                    new_entry_prices = {k.replace("-USD", "-USDC"): float(v.get("entry_price", v)) if isinstance(v, dict) else float(v) for k, v in raw.items()}
                    return {"entry_prices": new_entry_prices}
                return raw
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

# --- Ethereum Executor (Uniswap V3) ---
class EthereumExecutor:
    def __init__(self, rpc_url, private_key):
        from web3 import Web3
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.private_key = private_key
        self.account = self.w3.eth.account.from_key(private_key) if private_key else None
        self.address = self.account.address if self.account else None
        
        # Minimal ABIs
        self.erc20_abi = '[{"inputs":[{"internalType":"address","name":"spender","type":"address"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"approve","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"owner","type":"address"},{"internalType":"address","name":"spender","type":"address"}],"name":"allowance","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]'
        self.router_abi = '[{"inputs":[{"components":[{"internalType":"address","name":"tokenIn","type":"address"},{"internalType":"address","name":"tokenOut","type":"address"},{"internalType":"uint24","name":"fee","type":"uint24"},{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"},{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMinimum","type":"uint256"},{"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}],"internalType":"struct ISwapRouter.ExactInputSingleParams","name":"params","type":"tuple"}],"name":"exactInputSingle","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"}],"stateMutability":"payable","type":"function"}]'

    def is_connected(self): return self.w3.is_connected()

    def check_gas_price(self):
        gas_price_gwei = self.w3.from_wei(self.w3.eth.gas_price, 'gwei')
        logging.info(f"ETH Gas Price: {gas_price_gwei:.2f} Gwei (Limit: {ETH_MAX_GAS_PRICE_GWEI})")
        return gas_price_gwei <= ETH_MAX_GAS_PRICE_GWEI

    def approve_token(self, token_address, spender_address, amount):
        token_contract = self.w3.eth.contract(address=self.w3.to_checksum_address(token_address), abi=self.erc20_abi)
        allowance = token_contract.functions.allowance(self.address, spender_address).call()
        if allowance < amount:
            logging.info(f"Approving {token_address} for Uniswap Router...")
            tx = token_contract.functions.approve(spender_address, 2**256 - 1).build_transaction({
                'from': self.address, 'nonce': self.w3.eth.get_transaction_count(self.address),
                'gas': 60000, 'gasPrice': self.w3.eth.gas_price
            })
            signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
            self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            logging.info("Approval transaction sent.")

    def simulate_transaction(self, tx_params):
        if "alchemy" not in self.w3.provider.endpoint_uri:
            return True # Skip if not Alchemy
            
        try:
            logging.info("Simulating transaction via Alchemy...")
            # Convert Web3 params to JSON-RPC format
            sim_params = {
                "from": tx_params["from"],
                "to": tx_params["to"],
                "value": hex(tx_params["value"]) if "value" in tx_params else "0x0",
                "data": tx_params.get("data", "0x")
            }
            
            response = self.w3.provider.make_request("alchemy_simulateExecution", [sim_params])
            if "error" in response:
                logging.error(f"Simulation failed: {response["error"]}")
                return False
            logging.info("Simulation successful!")
            return True
        except Exception as e:
            logging.error(f"Simulation error: {e}")
            return False

    def execute_trade(self, asset, side):
        if not self.account: return
        if not self.check_gas_price():
            logging.warning("Skipping ETH trade due to high gas price.")
            return

        logging.info(f"EthereumExecutor: Swapping {side} for {asset} on Uniswap V3")
        if TRADING_MODE != "live":
            logging.info(f"[PAPER ETH] Would have swapped {asset} {side} on Uniswap V3")
            return

        try:
            router = self.w3.eth.contract(address=self.w3.to_checksum_address(UNISWAP_ROUTER_ADDRESS), abi=self.router_abi)
            deadline = int(time.time()) + 600
            
            if side == 'BUY': # ETH -> USDC
                params = {
                    'tokenIn': self.w3.to_checksum_address(WETH_ADDRESS),
                    'tokenOut': self.w3.to_checksum_address(USDC_ADDRESS),
                    'fee': 3000, # 0.3% pool
                    'recipient': self.address,
                    'deadline': deadline,
                    'amountIn': ETH_TRADE_AMOUNT_WEI,
                    'amountOutMinimum': 0,
                    'sqrtPriceLimitX96': 0
                }
                tx = router.functions.exactInputSingle(params).build_transaction({
                    'from': self.address, 'value': ETH_TRADE_AMOUNT_WEI,
                    'nonce': self.w3.eth.get_transaction_count(self.address),
                    'gas': 250000, 'gasPrice': self.w3.eth.gas_price
                })
            else: # USDC -> ETH
                usdc_contract = self.w3.eth.contract(address=self.w3.to_checksum_address(USDC_ADDRESS), abi=self.erc20_abi)
                usdc_balance = usdc_contract.functions.balanceOf(self.address).call()
                if usdc_balance == 0:
                    logging.warning("No USDC balance to swap for ETH.")
                    return
                self.approve_token(USDC_ADDRESS, UNISWAP_ROUTER_ADDRESS, usdc_balance)
                params = {
                    'tokenIn': self.w3.to_checksum_address(USDC_ADDRESS),
                    'tokenOut': self.w3.to_checksum_address(WETH_ADDRESS),
                    'fee': 3000, 'recipient': self.address, 'deadline': deadline,
                    'amountIn': usdc_balance, 'amountOutMinimum': 0, 'sqrtPriceLimitX96': 0
                }
                tx = router.functions.exactInputSingle(params).build_transaction({
                    'from': self.address, 'nonce': self.w3.eth.get_transaction_count(self.address),
                    'gas': 250000, 'gasPrice': self.w3.eth.gas_price
                })

            # Simulate before sending
            if self.simulate_transaction(tx):
                signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
                logging.info(f"Uniswap V3 Swap Sent! Hash: {tx_hash.hex()}")
            else:
                logging.warning("Transaction aborted due to simulation failure.")
        except Exception as e: logging.error(f"Uniswap Swap Failed: {e}")

# --- Trading Logic ---
def place_limit_order(product_id, side, price, amount_quote_currency=None, amount_base_currency=None):
    details = get_product_details(product_id)
    if not details: return None
    order_id = str(uuid.uuid4())
    
    # Calculate base_size if we only have quote_currency (for BUYs)
    if side == 'BUY' and amount_quote_currency:
        base_size = float(amount_quote_currency) / float(price)
    else:
        base_size = amount_base_currency

    # Round base size to product increment
    base_increment = details['base_increment']
    rounded_base = round_to_increment(base_size, base_increment)
    
    # Round price to quote increment
    price_increment = details['quote_increment']
    rounded_price = round_to_increment(price, price_increment)

    config = {
        "limit_limit_gtc": {
            "base_size": str(rounded_base),
            "limit_price": str(rounded_price),
            "post_only": True  # Force MAKER fees
        }
    }
    
    payload = {
        "client_order_id": order_id, 
        "product_id": product_id, 
        "side": side, 
        "order_configuration": config
    }
    
    logging.info(f"Placing LIMIT {side} (Post-Only) for {product_id} at {rounded_price}")

    if TRADING_MODE == "live":
        return coinbase_request("POST", "/api/v3/brokerage/orders", payload)
    return {"success": True}

def place_market_order(product_id, side, amount_quote_currency=None, amount_base_currency=None):
    """Place an immediate-or-cancel (IOC) market order for fast execution."""
    details = get_product_details(product_id)
    if not details: return None
    order_id = str(uuid.uuid4())
    
    # Calculate base_size if we only have quote_currency (for BUYs)
    if side == 'BUY' and amount_quote_currency:
        base_size = float(amount_quote_currency) / float(details['price'])
    else:
        base_size = amount_base_currency

    # Round base size to product increment
    base_increment = details['base_increment']
    rounded_base = round_to_increment(base_size, base_increment)
    
    config = {
        "market_market_ioc": {
            "quote_size": str(amount_quote_currency) if amount_quote_currency else "",
            "base_size": str(rounded_base)
        }
    }
    
    payload = {
        "client_order_id": order_id, 
        "product_id": product_id, 
        "side": side, 
        "order_configuration": config
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
    """
    Calculate 24-hour percentage change from candle data.
    Assumes df has 'close' column with hourly data.
    Returns percentage change over last MOMENTUM_WINDOW_HOURS hours.
    """
    if df is None or len(df) < MOMENTUM_WINDOW_HOURS + 1:
        return 0.0
    current_price = df['close'].iloc[-1]
    historical_price = df['close'].iloc[-(MOMENTUM_WINDOW_HOURS + 1)]
    if historical_price == 0:
        return 0.0
    pct_change = ((current_price - historical_price) / historical_price) * 100
    return pct_change

def is_asset_blacklisted(asset):
    """Check if an asset is in the blacklist."""
    return asset.upper() in [a.upper() for a in ASSET_BLACKLIST]

def run_bot(reset_to_usdc=False):
    logging.info(f"--- 🤖 Crypto Bot Run ({TRADING_MODE.upper()}) ---")
    if reset_to_usdc:
        logging.info("!!! RESET TO USDC MODE ACTIVE !!!")

    eth_executor = EthereumExecutor(ETH_RPC_URL, ETH_PRIVATE_KEY) if ENABLE_ETHEREUM and ETH_RPC_URL and ETH_PRIVATE_KEY else None
    if eth_executor: logging.info(f"ETH Executor Active: {eth_executor.address}")
    
    balances = get_all_balances()
    cash, held, state = balances["cash"], balances["crypto"], load_state()
    
    total_value = sum(cash.values())
    for cur, amt in held.items():
        price = (get_product_details(f"{cur}-USDC") or {}).get('price')
        if price: total_value += amt * float(price)

    trade_limit = total_value * PORTFOLIO_RISK_PERCENTAGE
    btc_df = get_market_data("BTC-USDC")
    btc_s, btc_l = analyze_trend(btc_df)
    btc_trend = "BEAR" if btc_s and btc_l and btc_s < btc_l else "BULL"
    logging.info(f"Market: {btc_trend}. Portfolio: ${total_value:,.2f}")

    # Track available USDC for the loop to avoid greedy over-allocation
    available_usdc = cash["USDC"]

    # If resetting, sell everything in 'held' using MARKET orders
    if reset_to_usdc:
        for asset, amount in held.items():
            if asset in ["USD", "USDC"]: continue
            product_id = f"{asset}-USDC"
            
            # Skip blacklisted assets
            if is_asset_blacklisted(asset):
                logging.info(f"RESET: Skipping blacklisted asset {asset}")
                continue
                
            try:
                details = get_product_details(product_id)
                if not details: continue
                price = float(details['price'])
                logging.info(f"RESET: Selling {amount} {asset} at market price")
                # Use MARKET order for immediate execution
                place_market_order(product_id, 'SELL', amount_base_currency=amount)
                if eth_executor and asset in ["ETH", "WETH"]: eth_executor.execute_trade(asset, "SELL")
                clear_entry_price(product_id)
            except Exception as e: logging.error(f"Error resetting {asset}: {e}")
        logging.info("Reset complete.")
        return

    # === NEW: Momentum Ranking + Trend Filter Combo ===
    
    # Step 1: Collect all assets with their momentum and trend data
    asset_candidates = []
    
    for asset in set(list(held.keys()) + ["BTC", "ETH", "SOL"]):
        if asset in ["USD", "USDC"]: continue
        
        # Skip blacklisted assets
        if is_asset_blacklisted(asset):
            logging.info(f"Skipping blacklisted asset: {asset}")
            continue
            
        product_id = f"{asset}-USDC"
        try:
            df = get_market_data(product_id)
            if df is None or len(df) < LONG_WINDOW:
                continue
            
            ma_s, ma_l = analyze_trend(df)
            if ma_s is None:
                continue
            
            # Calculate momentum (24h % change)
            momentum_pct = get_momentum_ranking(df)
            
            # Check trend filter
            trend_passes = ma_s > ma_l * 1.002
            btc_condition = btc_trend == "BULL" or asset == "BTC"
            
            # Step 2: Apply BOTH filters
            if trend_passes and btc_condition:
                asset_candidates.append({
                    "asset": asset,
                    "product_id": product_id,
                    "momentum": momentum_pct,
                    "ma_s": ma_s,
                    "ma_l": ma_l
                })
                logging.info(f"Momentum candidate: {asset} - {momentum_pct:+.2f}% (24h), Trend: {'BULL' if trend_passes else 'BEAR'}")
        except Exception as e:
            logging.error(f"Error analyzing {asset}: {e}")
    
    # Step 3: Sort by momentum (highest first)
    asset_candidates.sort(key=lambda x: x["momentum"], reverse=True)
    
    # Step 4: Buy only top N momentum assets that pass both filters
    logging.info(f"Top {TOP_MOMENTUM_COUNT} momentum assets prioritized for buying")
    
    for candidate in asset_candidates[:TOP_MOMENTUM_COUNT]:
        asset = candidate["asset"]
        product_id = candidate["product_id"]
        
        try:
            details = get_product_details(product_id)
            if not details: continue
            price = float(details['price'])
            
            entry = state.get("entry_prices", {}).get(product_id)
            if entry and price < entry * (1 - STOP_LOSS_PCT):
                if held.get(asset, 0) * price > 5:
                    place_limit_order(product_id, 'SELL', price, amount_base_currency=held[asset])
                    if eth_executor and asset in ["ETH", "WETH"]: eth_executor.execute_trade(asset, "SELL")
                    clear_entry_price(product_id)
                    continue
            
            # Buy logic - spread available USDC across top momentum assets
            # Use RISK_PER_TRADE_PCT but spread across top candidates
            buy_size = min(available_usdc * RISK_PER_TRADE_PCT / TOP_MOMENTUM_COUNT, trade_limit / TOP_MOMENTUM_COUNT)
            if buy_size > 10:
                if place_limit_order(product_id, 'BUY', price, amount_quote_currency=buy_size):
                    update_entry_price(product_id, price)
                    if eth_executor and asset in ["ETH", "WETH"]: eth_executor.execute_trade(asset, "BUY")
                    available_usdc -= buy_size
                    logging.info(f"Buy {asset}: ${buy_size:,.2f} | Remaining USDC: ${available_usdc:,.2f}")
        except Exception as e:
            logging.error(f"Error {asset}: {e}")
    
    # Also handle sells for held assets (existing logic)
    for asset in set(list(held.keys()) + ["BTC", "ETH", "SOL"]):
        if asset in ["USD", "USDC"]: continue
        product_id = f"{asset}-USDC"
        try:
            details = get_product_details(product_id)
            if not details: continue
            price = float(details['price'])
            
            entry = state.get("entry_prices", {}).get(product_id)
            if entry and price < entry * (1 - STOP_LOSS_PCT):
                if held.get(asset, 0) * price > 5:
                    place_limit_order(product_id, 'SELL', price, amount_base_currency=held[asset])
                    if eth_executor and asset in ["ETH", "WETH"]: eth_executor.execute_trade(asset, "SELL")
                    clear_entry_price(product_id)
                    continue
            
            df = get_market_data(product_id)
            ma_s, ma_l = analyze_trend(df)
            if ma_s is None: continue
            
            # Only SELL if below trend line (existing logic)
            if ma_s < ma_l * 0.998 and held.get(asset, 0) * price > 10:
                place_limit_order(product_id, 'SELL', price, amount_base_currency=held[asset] * 0.5)
                if eth_executor and asset in ["ETH", "WETH"]: eth_executor.execute_trade(asset, "SELL")
        except Exception as e:
            logging.error(f"Error {asset}: {e}")

if __name__ == "__main__":
    reset = "--reset" in sys.argv
    run_bot(reset_to_usdc=reset)