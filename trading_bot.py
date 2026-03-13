#!/usr/bin/env python3
import os
import time
import datetime
import pandas as pd
import json
import logging
import sys
import signal
from pathlib import Path
from decimal import Decimal

# Graceful shutdown
shutdown_requested = False
def handle_shutdown(signum, frame):
    global shutdown_requested
    logging.info("Shutdown signal received, finishing current cycle...")
    shutdown_requested = True
signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)

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
PORTFOLIO_RISK_PERCENTAGE = float(os.environ.get("PORTFOLIO_RISK_PERCENTAGE", "0.15"))
RISK_PER_TRADE_PCT = float(os.environ.get("RISK_PER_TRADE_PCT", "0.95"))
STOP_LOSS_PCT = 0.05  # Kept for backward compatibility reference

# Dynamic Risk Limits
MAX_POSITION_USD = float(os.environ.get("MAX_POSITION_USD", "5000"))
MAX_DRAWDOWN_PCT = float(os.environ.get("MAX_DRAWDOWN_PCT", "10"))
MIN_ORDER_USD = float(os.environ.get("MIN_ORDER_USD", "10"))
ASSET_BLACKLIST = ["DOGE", "SHLD", "SHIB"]
MOMENTUM_WINDOW_HOURS = 24
TOP_MOMENTUM_COUNT = 3

# Trailing Stop-Loss
TRAILING_STOP_PCT = float(os.environ.get("TRAILING_STOP_PCT", "0.05"))

# Take-Profit Levels
TAKE_PROFIT_1_PCT = float(os.environ.get("TAKE_PROFIT_1_PCT", "0.10"))
TAKE_PROFIT_1_SELL_RATIO = float(os.environ.get("TAKE_PROFIT_1_SELL_RATIO", "0.33"))
TAKE_PROFIT_2_PCT = float(os.environ.get("TAKE_PROFIT_2_PCT", "0.20"))
TAKE_PROFIT_2_SELL_RATIO = float(os.environ.get("TAKE_PROFIT_2_SELL_RATIO", "0.50"))

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
    default_state = {"entry_prices": {}, "high_water_marks": {}, "take_profit_flags": {}}
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
            # Ensure new keys exist for backward compatibility
            state.setdefault("entry_prices", {})
            state.setdefault("high_water_marks", {})
            state.setdefault("take_profit_flags", {})
            return state
        except:
            return default_state
    return default_state

def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=2)
    except: pass

def update_entry_price(executor_id, product_id, price):
    state = load_state()
    key = f"{executor_id}:{product_id}"
    state.setdefault("entry_prices", {})[key] = price
    # Initialize high water mark to entry price
    state.setdefault("high_water_marks", {})[key] = price
    # Reset take-profit flags for new entry
    state.setdefault("take_profit_flags", {})[key] = {"tp1_hit": False, "tp2_hit": False}
    save_state(state)

def clear_entry_price(executor_id, product_id):
    state = load_state()
    key = f"{executor_id}:{product_id}"
    if key in state.get("entry_prices", {}):
        del state["entry_prices"][key]
    # Also clear high water mark
    if key in state.get("high_water_marks", {}):
        del state["high_water_marks"][key]
    # Also clear take-profit flags
    if key in state.get("take_profit_flags", {}):
        del state["take_profit_flags"][key]
    save_state(state)

def load_peak_value():
    state = load_state()
    return state.get("peak_portfolio_value", 0.0)

def save_peak_value(value):
    state = load_state()
    state["peak_portfolio_value"] = value
    save_state(state)

def record_trade(is_win, pnl):
    state = load_state()
    perf = state.setdefault("performance", {"total_trades": 0, "winning_trades": 0, "losing_trades": 0, "total_pnl": 0.0, "run_count": 0})
    perf["total_trades"] += 1
    if is_win:
        perf["winning_trades"] += 1
    else:
        perf["losing_trades"] += 1
    perf["total_pnl"] += pnl
    perf["last_run_time"] = datetime.datetime.now().isoformat()
    save_state(state)

def increment_run_count():
    state = load_state()
    perf = state.setdefault("performance", {"total_trades": 0, "winning_trades": 0, "losing_trades": 0, "total_pnl": 0.0, "run_count": 0})
    perf["run_count"] += 1
    perf["last_run_time"] = datetime.datetime.now().isoformat()
    save_state(state)

def get_performance():
    state = load_state()
    return state.get("performance", {"total_trades": 0, "winning_trades": 0, "losing_trades": 0, "total_pnl": 0.0, "run_count": 0})

def log_performance_summary():
    perf = get_performance()
    total = perf.get("total_trades", 0)
    wins = perf.get("winning_trades", 0)
    win_rate = (wins / total * 100) if total > 0 else 0
    logging.info(f"[Performance] Trades: {total} | Wins: {wins} ({win_rate:.0f}%) | Total PnL: ${perf.get('total_pnl', 0):+.2f}")

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
    
    # Update peak value and check drawdown
    peak = load_peak_value()
    if ex_value > peak:
        save_peak_value(ex_value)
        peak = ex_value
    drawdown_pct = ((peak - ex_value) / peak * 100) if peak > 0 else 0
    
    # Drawdown guard: skip buys if drawdown exceeds limit
    if drawdown_pct >= MAX_DRAWDOWN_PCT:
        logging.warning(f"[{ex_id}] Drawdown {drawdown_pct:.1f}% exceeds limit {MAX_DRAWDOWN_PCT}%. Pausing new buys.")
    
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
            # Enforce MAX_POSITION_USD per-asset position cap
            current_asset_value = held.get(asset, 0) * price
            if current_asset_value + buy_size > MAX_POSITION_USD:
                # Cap buy_size to stay within limit
                buy_size = max(0, MAX_POSITION_USD - current_asset_value)
                if buy_size < MIN_ORDER_USD:
                    logging.info(f"[{ex_id}] Skipping {asset}: position at ${current_asset_value:,.0f} already at/exceeds MAX_POSITION_USD (${MAX_POSITION_USD:,.0f})")
                    continue
                logging.info(f"[{ex_id}] Capped {asset} buy to ${buy_size:,.2f} to stay within MAX_POSITION_USD")
            if drawdown_pct >= MAX_DRAWDOWN_PCT:
                continue  # Skip buys during drawdown
            if buy_size > MIN_ORDER_USD:
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
            
            if not entry:
                continue
            
            # --- Update high water mark ---
            hwm = state.get("high_water_marks", {}).get(entry_key, entry)
            if price > hwm:
                hwm = price
                state.setdefault("high_water_marks", {})[entry_key] = hwm
                save_state(state)
                logging.info(f"[{ex_id}] New high water mark for {asset}: ${hwm:,.2f}")
            
            # --- Get take-profit flags ---
            tp_flags = state.get("take_profit_flags", {}).get(entry_key, {"tp1_hit": False, "tp2_hit": False})
            
            sell_trigger = False
            sell_ratio = 1.0
            reason = ""
            
            # Priority 1: Take-Profit Level 2 (higher gain threshold)
            if not tp_flags.get("tp2_hit", False) and price >= entry * (1 + TAKE_PROFIT_2_PCT):
                sell_trigger = True
                sell_ratio = TAKE_PROFIT_2_SELL_RATIO
                reason = f"Take-profit level 2 triggered for {asset} (price ${price:,.2f} >= ${entry * (1 + TAKE_PROFIT_2_PCT):,.2f}, selling {TAKE_PROFIT_2_SELL_RATIO*100:.0f}%)"
                tp_flags["tp2_hit"] = True
                # Also mark TP1 as hit since we passed it
                tp_flags["tp1_hit"] = True
                state.setdefault("take_profit_flags", {})[entry_key] = tp_flags
                save_state(state)
            
            # Priority 2: Take-Profit Level 1 (lower gain threshold)
            elif not tp_flags.get("tp1_hit", False) and price >= entry * (1 + TAKE_PROFIT_1_PCT):
                sell_trigger = True
                sell_ratio = TAKE_PROFIT_1_SELL_RATIO
                reason = f"Take-profit level 1 triggered for {asset} (price ${price:,.2f} >= ${entry * (1 + TAKE_PROFIT_1_PCT):,.2f}, selling {TAKE_PROFIT_1_SELL_RATIO*100:.0f}%)"
                tp_flags["tp1_hit"] = True
                state.setdefault("take_profit_flags", {})[entry_key] = tp_flags
                save_state(state)
            
            # Priority 3: Trailing Stop-Loss
            elif price < hwm * (1 - TRAILING_STOP_PCT):
                sell_trigger = True
                sell_ratio = 1.0
                reason = f"Trailing stop-loss triggered for {asset} (price ${price:,.2f} < high water mark ${hwm:,.2f} * {1 - TRAILING_STOP_PCT:.2f})"
            
            # Priority 4: Trend-exit (MA cross) — fallback
            if not sell_trigger:
                df = data_provider.get_market_data(product_id, LONG_WINDOW)
                ma_s, ma_l = analyze_trend(df)
                if ma_s and ma_l and ma_s < ma_l * 0.998:
                    sell_trigger = True
                    sell_ratio = 0.5
                    reason = f"Trend-exit (50%) triggered for {asset}"
            
            if sell_trigger:
                logging.info(f"[{ex_id}] 🚨 {reason}")
                sell_amount = amt * sell_ratio
                if executor.place_limit_order(product_id, 'SELL', price, amount_base_currency=sell_amount):
                    logging.info(f"[{ex_id}] ✅ Sold {sell_amount:.6f} {asset} at ${price:,.2f}")
                    # Track PnL
                    if entry:
                        pnl = (price - entry) * sell_amount
                        is_win = pnl > 0
                        record_trade(is_win, pnl)
                        logging.info(f"[{ex_id}] 📊 PnL: ${pnl:+.2f} (entry=${entry:,.2f} -> exit=${price:,.2f})")
                    if sell_ratio == 1.0:
                        clear_entry_price(ex_id, product_id)
                    else:
                        # Partial sell — save updated TP flags (already saved above)
                        pass
        except Exception as e:
            logging.error(f"[{ex_id}] Error managing sell for {asset}: {e}")
        
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
    logging.info(f"[Risk] max_position=${MAX_POSITION_USD:,.0f} | max_drawdown={MAX_DRAWDOWN_PCT}% | min_order=${MIN_ORDER_USD:.0f}")
    logging.info(f"[Risk] portfolio_risk={PORTFOLIO_RISK_PERCENTAGE*100:.0f}% | risk_per_trade={RISK_PER_TRADE_PCT*100:.0f}%")
    logging.info(f"Trailing Stop: {TRAILING_STOP_PCT*100:.1f}% | TP1: {TAKE_PROFIT_1_PCT*100:.1f}% ({TAKE_PROFIT_1_SELL_RATIO*100:.0f}% sell) | TP2: {TAKE_PROFIT_2_PCT*100:.1f}% ({TAKE_PROFIT_2_SELL_RATIO*100:.0f}% sell)")
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
    
    # Run summary
    increment_run_count()
    log_performance_summary()
    logging.info(f"=== Run Summary ===")
    logging.info(f"Market Regime: {btc_trend}")
    logging.info(f"Portfolio Value: ${aggregate_value:,.2f}")
    logging.info(f"Risk Params: max_pos=${MAX_POSITION_USD:,.0f} | max_dd={MAX_DRAWDOWN_PCT}% | trailing_stop={TRAILING_STOP_PCT*100:.0f}%")
    logging.info(f"Take-Profit: TP1={TAKE_PROFIT_1_PCT*100:.0f}%/{TAKE_PROFIT_1_SELL_RATIO*100:.0f}% | TP2={TAKE_PROFIT_2_PCT*100:.0f}%/{TAKE_PROFIT_2_SELL_RATIO*100:.0f}%")
    logging.info(f"==================")

if __name__ == "__main__":
    if "--report" in sys.argv:
        perf = get_performance()
        total = perf.get("total_trades", 0)
        wins = perf.get("winning_trades", 0)
        losses = perf.get("losing_trades", 0)
        win_rate = (wins / total * 100) if total > 0 else 0
        print(f"=== Trading Bot Performance ===")
        print(f"Total Trades: {total}")
        print(f"Winning: {wins} | Losing: {losses}")
        print(f"Win Rate: {win_rate:.1f}%")
        print(f"Total PnL: ${perf.get('total_pnl', 0):+.2f}")
        print(f"Runs: {perf.get('run_count', 0)}")
        print(f"Last Run: {perf.get('last_run_time', 'N/A')}")
    else:
        run_bot(reset_to_usdc="--reset" in sys.argv)
