#!/usr/bin/env python3
import os
import time
import datetime
import pandas as pd
import json
import logging
import sys
import signal
import fcntl
import requests
from pathlib import Path
from decimal import Decimal

# Import configuration
from config import TradingConfig, ExecutorConfig

# Import core business logic
from core import StateManager, TechnicalAnalysis, RegimeDetector

# Graceful shutdown
shutdown_requested = False
def handle_shutdown(signum, frame):
    global shutdown_requested
    logging.info("Shutdown signal received, finishing current cycle...")
    shutdown_requested = True
signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)

# Load and validate configuration
config = TradingConfig.from_env()
config.validate()

exec_config = ExecutorConfig.from_env()
exec_config.validate()

# Initialize core components
state_manager = StateManager(exec_config.state_file)
technical_analysis = TechnicalAnalysis(
    ma_short_window=config.ma_short_window,
    ma_long_window=config.ma_long_window
)
regime_detector = RegimeDetector(
    technical_analysis=technical_analysis,
    ma_short_window=config.ma_short_window,
    ma_long_window=config.ma_long_window,
    enable_btc_dominance=config.enable_btc_dominance
)

# --- Logging Configuration ---
os.makedirs(os.path.dirname(exec_config.log_file), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(exec_config.log_file),
        logging.StreamHandler(sys.stdout)
    ]
)

# Log configuration
logging.info(f"Using {config.trend_asset} for market regime detection")
logging.info(f"BTC bear-market exemption: {'ENABLED' if config.allow_btc_in_bear else 'DISABLED'}")
logging.info(f"Dual-signal regime detection: {'ENABLED' if config.enable_dual_regime else 'DISABLED'}")
logging.info(f"Bitcoin dominance tracking: {'ENABLED' if config.enable_btc_dominance else 'DISABLED'}")
logging.info(f"Trading mode: {exec_config.trading_mode}")

# Import strategy system
from strategies import create_strategy
strategy = create_strategy(config.strategy, technical_analysis, config)
logging.info(f"Strategy: {strategy.name}")

# Import specialized executors (after config loaded)
from executors import CoinbaseExecutor, EthereumExecutor, validate_executor

# Backward compatibility: expose config values as module-level constants
# TODO: Remove these after refactoring is complete
API_JSON_FILE = exec_config.api_json_file
STATE_FILE = exec_config.state_file
TRADING_MODE = exec_config.trading_mode
ENABLE_ETHEREUM = exec_config.ethereum_enabled
ETH_RPC_URL = exec_config.eth_rpc_url
ETH_PRIVATE_KEY = exec_config.eth_private_key
ENABLE_SHORT = config.enable_short
SHORT_WINDOW = config.ma_short_window
LONG_WINDOW = config.ma_long_window
PORTFOLIO_RISK_PERCENTAGE = float(config.portfolio_risk_pct)
SHORT_RISK_PERCENTAGE = float(config.short_risk_pct)
RISK_PER_TRADE_PCT = float(config.risk_per_trade_pct)
STOP_LOSS_PCT = 0.05  # Kept for backward compatibility reference
MAX_POSITION_USD = float(config.max_position_usd)
MAX_DRAWDOWN_PCT = float(config.max_drawdown_pct)
MIN_ORDER_USD = float(config.min_order_usd)
ASSET_BLACKLIST = config.asset_blacklist
MOMENTUM_WINDOW_HOURS = config.momentum_window_hours
TOP_MOMENTUM_COUNT = config.top_momentum_count
TRAILING_STOP_PCT = float(config.trailing_stop_pct)
MIN_24H_VOLUME_USD = float(config.min_24h_volume_usd)
RSI_OVERBOUGHT = float(config.rsi_overbought)
ROUND_TRIP_FEE_PCT = float(config.round_trip_fee_pct)
TREND_ASSET = config.trend_asset
ALLOW_BTC_IN_BEAR = config.allow_btc_in_bear
ENABLE_DUAL_REGIME = config.enable_dual_regime
ENABLE_BTC_DOMINANCE = config.enable_btc_dominance
TAKE_PROFIT_1_PCT = float(config.take_profit_1_pct)
TAKE_PROFIT_1_SELL_RATIO = float(config.take_profit_1_sell_ratio)
TAKE_PROFIT_2_PCT = float(config.take_profit_2_pct)
TAKE_PROFIT_2_SELL_RATIO = float(config.take_profit_2_sell_ratio)
ASSET_MAPPING = config.asset_mapping

# --- Helpers ---
def get_data_product_id(asset):
    mapped = ASSET_MAPPING.get(asset.upper(), asset.upper())
    return f"{mapped}-USDC"

def round_to_increment(amount, increment):
    inc = Decimal(str(increment))
    amt = Decimal(str(amount))
    return (amt // inc) * inc

# --- Process-level lock (prevents overlapping runs from systemd timer) ---
_RUN_LOCK_FILE = exec_config.state_file.with_suffix('.runlock')
_run_lock_fd = None

def acquire_run_lock():
    """Acquire exclusive process-level lock. Returns True if acquired, False if another instance is running."""
    global _run_lock_fd
    os.makedirs(os.path.dirname(_RUN_LOCK_FILE), exist_ok=True)
    _run_lock_fd = open(_RUN_LOCK_FILE, 'w')
    try:
        fcntl.flock(_run_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _run_lock_fd.write(str(os.getpid()))
        _run_lock_fd.flush()
        return True
    except (IOError, OSError):
        _run_lock_fd.close()
        _run_lock_fd = None
        return False

def release_run_lock():
    """Release process-level lock."""
    global _run_lock_fd
    if _run_lock_fd:
        try:
            fcntl.flock(_run_lock_fd, fcntl.LOCK_UN)
            _run_lock_fd.close()
        except Exception:
            pass
        _run_lock_fd = None

# --- State Management (wrappers for backward compatibility) ---
def load_state():
    """Wrapper for backward compatibility"""
    # Update state_manager's state_file in case STATE_FILE was patched (for tests)
    if state_manager.state_file != STATE_FILE:
        state_manager.state_file = STATE_FILE
        state_manager.lock_file = STATE_FILE.with_suffix('.lock')
    return state_manager.load_state()

def save_state(state):
    """Wrapper for backward compatibility"""
    # Update state_manager's state_file in case STATE_FILE was patched (for tests)
    if state_manager.state_file != STATE_FILE:
        state_manager.state_file = STATE_FILE
        state_manager.lock_file = STATE_FILE.with_suffix('.lock')
    return state_manager.save_state(state)

def update_entry_price(executor_id, product_id, price):
    """Wrapper for backward compatibility"""
    # Update state_manager's state_file in case STATE_FILE was patched (for tests)
    if state_manager.state_file != STATE_FILE:
        state_manager.state_file = STATE_FILE
        state_manager.lock_file = STATE_FILE.with_suffix('.lock')
    return state_manager.update_entry_price(executor_id, product_id, price)

def clear_entry_price(executor_id, product_id):
    """Wrapper for backward compatibility"""
    # Update state_manager's state_file in case STATE_FILE was patched (for tests)
    if state_manager.state_file != STATE_FILE:
        state_manager.state_file = STATE_FILE
        state_manager.lock_file = STATE_FILE.with_suffix('.lock')
    return state_manager.clear_entry_price(executor_id, product_id)

def load_peak_value(executor_id="default"):
    """Wrapper for backward compatibility"""
    if state_manager.state_file != STATE_FILE:
        state_manager.state_file = STATE_FILE
        state_manager.lock_file = STATE_FILE.with_suffix('.lock')
    return state_manager.load_peak_value(executor_id)

def save_peak_value(value, executor_id="default"):
    """Wrapper for backward compatibility"""
    if state_manager.state_file != STATE_FILE:
        state_manager.state_file = STATE_FILE
        state_manager.lock_file = STATE_FILE.with_suffix('.lock')
    return state_manager.save_peak_value(value, executor_id)

def record_trade(is_win, pnl):
    """Wrapper for backward compatibility"""
    if state_manager.state_file != STATE_FILE:
        state_manager.state_file = STATE_FILE
        state_manager.lock_file = STATE_FILE.with_suffix('.lock')
    return state_manager.record_trade(is_win, pnl)

def increment_run_count():
    """Wrapper for backward compatibility"""
    if state_manager.state_file != STATE_FILE:
        state_manager.state_file = STATE_FILE
        state_manager.lock_file = STATE_FILE.with_suffix('.lock')
    return state_manager.increment_run_count()

def get_performance():
    """Wrapper for backward compatibility"""
    if state_manager.state_file != STATE_FILE:
        state_manager.state_file = STATE_FILE
        state_manager.lock_file = STATE_FILE.with_suffix('.lock')
    return state_manager.get_performance()

def log_performance_summary():
    """Wrapper for backward compatibility"""
    if state_manager.state_file != STATE_FILE:
        state_manager.state_file = STATE_FILE
        state_manager.lock_file = STATE_FILE.with_suffix('.lock')
    return state_manager.log_performance_summary()

def is_asset_blacklisted(asset):
    return asset.upper() in [a.upper() for a in ASSET_BLACKLIST]

# --- Strategy Logic (wrappers for backward compatibility) ---
def analyze_trend(df):
    """Wrapper for backward compatibility"""
    return technical_analysis.analyze_trend(df)

def compute_eth_btc_ratio(data_provider):
    """Wrapper for backward compatibility"""
    return regime_detector.compute_eth_btc_ratio(data_provider)

def resolve_regime(btc_macro, rotation_signal, btc_dominance=None):
    """Wrapper for backward compatibility"""
    return regime_detector.resolve_regime(btc_macro, rotation_signal, btc_dominance)

def regime_to_legacy(regime):
    """Wrapper for backward compatibility"""
    return regime_detector.regime_to_legacy(regime)

def get_btc_dominance():
    """Wrapper for backward compatibility"""
    return regime_detector.get_btc_dominance()

def calculate_rsi(df, period=14):
    """Wrapper for backward compatibility"""
    return technical_analysis.calculate_rsi(df, period)

def calculate_atr(df, period=14):
    """Wrapper for backward compatibility"""
    return technical_analysis.calculate_atr(df, period)

def get_momentum_ranking(df):
    """Wrapper for backward compatibility"""
    return technical_analysis.get_momentum_ranking(df, MOMENTUM_WINDOW_HOURS)

def is_crossover_confirmed(df, direction="bull"):
    """Wrapper for backward compatibility"""
    return technical_analysis.is_crossover_confirmed(df, direction)

def run_executor_strategy(executor, data_provider, market_regime, full_regime="BULL", reset_to_usdc=False):
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
        if details:
            ex_value += amt * float(details['price'])
        else:
            logging.warning(f"[{ex_id}] Could not price {asset} (product_id={get_data_product_id(asset)}), excluding from portfolio value")

    logging.info(f"[{ex_id}] Sub-Portfolio Value: ${ex_value:,.2f} | USDC: ${cash:,.2f}")

    # Update peak value and check drawdown (per-executor)
    peak = load_peak_value(ex_id)
    if peak == 0.0:
        # First run — initialize peak to current value
        save_peak_value(ex_value, ex_id)
        peak = ex_value
    elif ex_value > peak:
        save_peak_value(ex_value, ex_id)
        peak = ex_value
    # Drawdown = how far the portfolio has fallen from its all-time high (per-executor).
    # drawdown% = (peak - current) / peak * 100
    # e.g. peak=$10k, current=$9k → drawdown = 10%
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

    # Skip new entries if strategy says so for this regime
    if strategy.should_skip_regime(market_regime, full_regime):
        logging.info(f"[{ex_id}] {full_regime} regime — {strategy.name} skipping new entries")

    # Scan for buys
    trade_limit = ex_value * PORTFOLIO_RISK_PERCENTAGE
    short_limit = ex_value * SHORT_RISK_PERCENTAGE
    asset_candidates = []
    # Get assets supported by this executor
    supported_assets = executor.get_supported_assets()
    for asset in supported_assets:
        if is_asset_blacklisted(asset): continue
        product_id = get_data_product_id(asset)
        try:
            df = data_provider.get_market_data(product_id, LONG_WINDOW)
            candidate = strategy.scan_entry(asset, product_id, df, market_regime, full_regime)
            if candidate is not None:
                asset_candidates.append(candidate)
        except Exception as e: logging.error(f"[{ex_id}] Error analyzing {asset}: {e}")

    # Short candidates in BEAR market
    short_candidates = []
    if strategy.enables_short and market_regime == "BEAR":
        for asset in supported_assets:
            if is_asset_blacklisted(asset): continue
            product_id = get_data_product_id(asset)
            try:
                df = data_provider.get_market_data(product_id, LONG_WINDOW)
                candidate = strategy.scan_short_entry(asset, product_id, df, market_regime, full_regime)
                if candidate is not None:
                    short_candidates.append(candidate)
            except Exception as e: logging.error(f"[{ex_id}] Error analyzing short {asset}: {e}")

        short_candidates = strategy.rank_short_candidates(short_candidates)

    asset_candidates = strategy.rank_candidates(asset_candidates)

    available_usdc = cash
    available_short_usdc = cash  # Separate pool for short positions
    for candidate in asset_candidates[:TOP_MOMENTUM_COUNT]:
        asset, product_id = candidate["asset"], candidate["product_id"]
        try:
            price_data = data_provider.get_product_details(product_id)
            price = float(price_data['price'])
            # Buy size = min of two caps, split across TOP_MOMENTUM_COUNT candidates:
            #   1. available_usdc * RISK_PER_TRADE_PCT / N  (don't spend more than we have)
            #   2. trade_limit / N  (don't exceed portfolio risk allocation)
            # trade_limit = portfolio_value * PORTFOLIO_RISK_PERCENTAGE (e.g. 15%)
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
            if drawdown_pct >= MAX_DRAWDOWN_PCT and market_regime == "BULL":
                continue  # Skip long buys during drawdown
            if buy_size > MIN_ORDER_USD:
                result = executor.place_limit_order(product_id, 'BUY', price, amount_quote_currency=buy_size)
                if result:
                    # Confirm fill before updating entry price
                    order_id = None
                    if isinstance(result, dict):
                        # Check if on-chain swap was confirmed via receipt
                        if result.get("confirmed"):
                            update_entry_price(ex_id, product_id, price)
                            available_usdc -= buy_size
                            logging.info(f"[{ex_id}] Buy {asset} confirmed on-chain at ${price:,.2f}")
                        elif result.get("success") is False:
                            logging.warning(f"[{ex_id}] Buy {asset} failed: {result.get('error', 'unknown')}")
                        else:
                            # Coinbase path: check order_id for fill confirmation
                            order_id = result.get("order_id") or result.get("success_response", {}).get("order_id") if isinstance(result.get("success_response"), dict) else None
                            if order_id and hasattr(executor, 'check_order_filled'):
                                filled_price = executor.check_order_filled(order_id)
                                if filled_price:
                                    update_entry_price(ex_id, product_id, filled_price)
                                    available_usdc -= buy_size
                                    logging.info(f"[{ex_id}] Buy {asset} confirmed at ${filled_price:,.2f}")
                                else:
                                    logging.warning(f"[{ex_id}] Buy {asset} order {order_id} not confirmed filled, skipping entry update")
                            else:
                                # Paper mode — use requested price
                                update_entry_price(ex_id, product_id, price)
                                available_usdc -= buy_size
        except Exception as e:
            logging.error(f"[{ex_id}] Error evaluating {asset} for buy: {e}")

    # Short selling in BEAR market (5% of portfolio limit)
    if strategy.enables_short:
        for candidate in short_candidates[:TOP_MOMENTUM_COUNT]:
            asset, product_id = candidate["asset"], candidate["product_id"]
            try:
                price_data = data_provider.get_product_details(product_id)
                price = float(price_data['price'])
                short_size = min(available_short_usdc * SHORT_RISK_PERCENTAGE, short_limit)
                if short_size < MIN_ORDER_USD:
                    logging.info(f"[{ex_id}] Skipping short {asset}: size ${short_size:,.2f} below minimum ${MIN_ORDER_USD:.0f}")
                    continue
                # Execute short sell (SELL order opens short position)
                result = executor.place_limit_order(product_id, 'SELL', price, amount_quote_currency=short_size)
                if result:
                    # Record entry price for short position (inverted for PnL)
                    entry_key = f"{ex_id}:{product_id}:SHORT"
                    state.setdefault("entry_prices", {})[entry_key] = price
                    available_short_usdc -= short_size
                    logging.info(f"[{ex_id}] Short {asset} at ${price:,.2f} (size: ${short_size:,.2f})")
            except Exception as e:
                logging.error(f"[{ex_id}] Error evaluating short {asset}: {e}")
    else:
        logging.info(f"[{ex_id}] Short selling disabled (strategy={strategy.name})")

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
                logging.warning(f"[{ex_id}] Holding {asset} ({amt:.6f}) has no entry price in state — position is unmanaged. Add entry manually or sell.")
                continue

            # --- Fetch candle data once for trailing stop ATR and trend exit ---
            df = data_provider.get_market_data(product_id, LONG_WINDOW)

            # --- Update high water mark ---
            hwm = state.get("high_water_marks", {}).get(entry_key, entry)
            if price > hwm:
                hwm = price
                state.setdefault("high_water_marks", {})[entry_key] = hwm
                save_state(state)
                logging.info(f"[{ex_id}] New high water mark for {asset}: ${hwm:,.2f}")

            # --- Get take-profit flags ---
            tp_flags = state.get("take_profit_flags", {}).get(entry_key, {"tp1_hit": False, "tp2_hit": False, "trend_exit_hit": False})

            sell_trigger, sell_ratio, reason, tp_flags = strategy.check_exit(
                asset, product_id, df, price, entry, hwm, tp_flags, state, entry_key
            )

            # Persist updated tp_flags if they changed
            if sell_trigger:
                state.setdefault("take_profit_flags", {})[entry_key] = tp_flags
                save_state(state)

            if sell_trigger:
                logging.info(f"[{ex_id}] 🚨 {reason}")
                sell_amount = amt * sell_ratio
                result = executor.place_limit_order(product_id, 'SELL', price, amount_base_currency=sell_amount)
                if result:
                    # Confirm fill and get actual exit price
                    exit_price = price  # default to requested price
                    if isinstance(result, dict) and result.get("success") is False:
                        logging.warning(f"[{ex_id}] Sell {asset} failed: {result.get('error', 'unknown')}")
                        continue
                    if isinstance(result, dict) and result.get("confirmed"):
                        logging.info(f"[{ex_id}] Sell {asset} confirmed on-chain at ${price:,.2f}")
                    elif isinstance(result, dict):
                        order_id = result.get("order_id") or result.get("success_response", {}).get("order_id") if isinstance(result.get("success_response"), dict) else None
                        if order_id and hasattr(executor, 'check_order_filled'):
                            filled_price = executor.check_order_filled(order_id)
                            if filled_price:
                                exit_price = filled_price
                                logging.info(f"[{ex_id}] Sell {asset} confirmed at ${filled_price:,.2f}")
                            else:
                                logging.warning(f"[{ex_id}] Sell {asset} order {order_id} not confirmed filled, using requested price for PnL")

                    logging.info(f"[{ex_id}] ✅ Sold {sell_amount:.6f} {asset} at ${exit_price:,.2f}")
                    # Track PnL (fee-aware)
                    # PnL = (exit - entry) * qty - fees
                    # Fee estimate: entry_price * qty * ROUND_TRIP_FEE_PCT
                    # This accounts for both the buy and sell side fees (~0.3% each
                    # on Uniswap V3 0.3% pools, totaling ~0.6% round-trip).
                    if entry:
                        fee_cost = entry * sell_amount * ROUND_TRIP_FEE_PCT
                        pnl = (exit_price - entry) * sell_amount - fee_cost
                        is_win = pnl > 0
                        record_trade(is_win, pnl)
                        logging.info(f"[{ex_id}] 📊 PnL: ${pnl:+.2f} (entry=${entry:,.2f} -> exit=${exit_price:,.2f}, fees=${fee_cost:.2f})")
                    if sell_ratio == 1.0:
                        clear_entry_price(ex_id, product_id)
                    else:
                        # Partial sell — save updated TP flags (already saved above)
                        pass
        except Exception as e:
            logging.error(f"[{ex_id}] Error managing sell for {asset}: {e}")

    # Manage Short Position Closes (buy-back when trend reverses or stop hit)
    if strategy.enables_short:
        short_entries = {k: v for k, v in state.get("entry_prices", {}).items()
                        if k.startswith(f"{ex_id}:") and k.endswith(":SHORT")}
        for short_key, short_entry in short_entries.items():
            # Extract product_id from key format "{ex_id}:{product_id}:SHORT"
            parts = short_key.split(":")
            if len(parts) < 3:
                continue
            product_id = parts[1]
            asset = product_id.split("-")[0]
            try:
                price_data = data_provider.get_product_details(product_id)
                if not price_data:
                    continue
                price = float(price_data['price'])

                df = data_provider.get_market_data(product_id, LONG_WINDOW)
                close_short = False
                reason = ""

                # Close short if trend reverses to bullish
                ma_s, ma_l = analyze_trend(df)
                if ma_s and ma_l and ma_s > ma_l * 1.002:
                    close_short = True
                    reason = f"Short close: trend reversed bullish for {asset}"

                # Close short on trailing stop (price rose too much from entry)
                elif price > short_entry * (1 + TRAILING_STOP_PCT):
                    close_short = True
                    reason = f"Short stop-loss: {asset} price ${price:,.2f} > entry ${short_entry:,.2f} * {1 + TRAILING_STOP_PCT:.2f}"

                # Close short on take-profit (price dropped enough)
                elif price <= short_entry * (1 - TAKE_PROFIT_1_PCT):
                    close_short = True
                    reason = f"Short take-profit: {asset} price ${price:,.2f} dropped to target"

                if close_short:
                    logging.info(f"[{ex_id}] 🚨 {reason}")
                    short_value = short_entry

                    # Try limit order first
                    result = executor.place_limit_order(product_id, 'BUY', price, amount_quote_currency=short_value)

                    # Detect failure: None = API exception, or Coinbase rejection
                    order_failed = (
                        result is None
                        or (isinstance(result, dict) and result.get("success") is False)
                        or (isinstance(result, dict) and "error_response" in result)
                    )

                    if order_failed:
                        fail_count = state.get("short_close_failures", {}).get(short_key, 0) + 1
                        state.setdefault("short_close_failures", {})[short_key] = fail_count

                        # Extract actual error from Coinbase response
                        if result is None:
                            error_detail = "API request returned None"
                        elif isinstance(result, dict):
                            error_detail = (
                                result.get("failure_reason")
                                or result.get("error_response", {}).get("message")
                                or result.get("error")
                                or str(result)
                            )
                        else:
                            error_detail = str(result)

                        logging.warning(
                            f"[{ex_id}] Short close limit order for {asset} failed "
                            f"(attempt {fail_count}): {error_detail}. "
                            f"Falling back to market order."
                        )

                        # Fallback: IOC market order for emergency exit
                        result = executor.place_market_order(
                            product_id, 'BUY',
                            amount_quote_currency=short_value
                        )

                        if result is None or (isinstance(result, dict) and result.get("success") is False):
                            market_error = str(result) if result else "API request returned None"
                            if fail_count >= 3:
                                logging.critical(
                                    f"[{ex_id}] CRITICAL: Cannot close short for {asset} "
                                    f"after {fail_count} attempts. Market order also failed: "
                                    f"{market_error}. Position is TRAPPED."
                                )
                            else:
                                logging.error(
                                    f"[{ex_id}] Market order fallback for {asset} also failed: "
                                    f"{market_error}. Will retry next cycle."
                                )
                            save_state(state)
                            continue

                        logging.info(f"[{ex_id}] Market order fallback succeeded for {asset}")
                        order_failed = False

                    if not order_failed:
                        # Clear failure counter
                        if "short_close_failures" in state and short_key in state.get("short_close_failures", {}):
                            del state["short_close_failures"][short_key]

                        pnl = (short_entry - price) * (short_value / short_entry) - short_entry * (short_value / short_entry) * ROUND_TRIP_FEE_PCT
                        is_win = pnl > 0
                        record_trade(is_win, pnl)
                        logging.info(f"[{ex_id}] 📊 Short PnL: ${pnl:+.2f} (entry=${short_entry:,.2f} -> exit=${price:,.2f})")
                        if short_key in state.get("entry_prices", {}):
                            del state["entry_prices"][short_key]
                            save_state(state)
            except Exception as e:
                logging.error(f"[{ex_id}] Error managing short close for {asset}: {e}")

    return ex_value

def run_bot(reset_to_usdc=False):
    if not acquire_run_lock():
        logging.warning("Another bot instance is already running, exiting.")
        return
    try:
        _run_bot(reset_to_usdc)
    finally:
        release_run_lock()

def _run_bot(reset_to_usdc=False):
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
                eth_executor = EthereumExecutor(rpc_url, private_key, TRADING_MODE)
                active_executors.append(eth_executor)
            except Exception as e: logging.error(f"Failed to init EthereumExecutor: {e}")

    # Validate all executors implement required interface
    for executor in active_executors:
        try:
            validate_executor(executor)
            logging.info(f"✓ {executor.__class__.__name__} validated")
        except TypeError as e:
            logging.error(f"✗ Executor validation failed: {e}")
            raise

    logging.info(f"--- 🤖 Crypto Bot Run ({TRADING_MODE.upper()}) ---")
    logging.info(f"[Risk] max_position=${MAX_POSITION_USD:,.0f} | max_drawdown={MAX_DRAWDOWN_PCT}% | min_order=${MIN_ORDER_USD:.0f}")
    logging.info(f"[Risk] portfolio_risk={PORTFOLIO_RISK_PERCENTAGE*100:.0f}% | risk_per_trade={RISK_PER_TRADE_PCT*100:.0f}%")
    logging.info(f"Trailing Stop: {TRAILING_STOP_PCT*100:.1f}% | TP1: {TAKE_PROFIT_1_PCT*100:.1f}% ({TAKE_PROFIT_1_SELL_RATIO*100:.0f}% sell) | TP2: {TAKE_PROFIT_2_PCT*100:.1f}% ({TAKE_PROFIT_2_SELL_RATIO*100:.0f}% sell)")
    data_provider = cb_executor

    # Global Trend Detection
    if ENABLE_DUAL_REGIME:
        # Dual-signal regime: BTC macro + ETH/BTC ratio → 5-state regime
        logging.info("Computing dual-signal market regime...")

        # 1. BTC Macro Trend
        btc_product = get_data_product_id("BTC")
        btc_df = data_provider.get_market_data(btc_product, LONG_WINDOW)
        btc_s, btc_l = analyze_trend(btc_df)

        if btc_s and btc_l:
            if btc_s > btc_l * 1.002:
                btc_macro = "BULL"
            elif btc_s < btc_l * 0.998:
                btc_macro = "BEAR"
            else:
                btc_macro = "FLAT"
        else:
            btc_macro = "BULL"  # Default when insufficient data

        # 2. ETH/BTC Rotation Signal
        rotation_signal = compute_eth_btc_ratio(data_provider)
        if rotation_signal is None:
            logging.warning("ETH/BTC ratio unavailable, falling back to BTC-only regime")
            rotation_signal = "NEUTRAL_RATIO"

        # 2.5. Bitcoin Dominance (optional)
        btc_dominance = None
        if ENABLE_BTC_DOMINANCE:
            btc_dominance = get_btc_dominance()
            if btc_dominance is None:
                logging.info("BTC dominance unavailable, continuing without it")

        # 3. Resolve Composite Regime
        full_regime = resolve_regime(btc_macro, rotation_signal, btc_dominance)

        # 4. Map to legacy BULL/BEAR for backward compatibility
        market_regime = regime_to_legacy(full_regime)

        logging.info(f"Market Regime: {full_regime} (BTC: {btc_macro} | Rotation: {rotation_signal})")
        logging.info(f"Legacy regime (passed to strategy): {market_regime}")

    else:
        # Single-asset regime (legacy behavior)
        trend_product = get_data_product_id(TREND_ASSET)
        trend_df = data_provider.get_market_data(trend_product, LONG_WINDOW)
        trend_s, trend_l = analyze_trend(trend_df)

        if trend_s and trend_l:
            if trend_s > trend_l * 1.002:
                market_regime = "BULL"
            elif trend_s < trend_l * 0.998:
                market_regime = "BEAR"
            else:
                market_regime = "BULL"  # Neutral zone defaults to BULL
        else:
            market_regime = "BULL"  # Default when insufficient data

        full_regime = market_regime  # Same as market_regime in single-asset mode
        logging.info(f"Market Regime ({TREND_ASSET}): {market_regime}")

    aggregate_value = 0
    for ex in active_executors:
        try:
            aggregate_value += run_executor_strategy(ex, data_provider, market_regime, full_regime, reset_to_usdc)
        except Exception as e:
            logging.error(f"Strategy failed for {ex.__class__.__name__}: {e}")

    logging.info(f"--- Aggregate Portfolio Total: ${aggregate_value:,.2f} ---")

    # Run summary
    increment_run_count()
    log_performance_summary()
    logging.info(f"=== Run Summary ===")
    if ENABLE_DUAL_REGIME:
        logging.info(f"Market Regime: {full_regime}")
    else:
        logging.info(f"Market Regime ({TREND_ASSET}): {market_regime}")
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
