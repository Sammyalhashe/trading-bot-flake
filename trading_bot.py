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
from core import StateManager, TechnicalAnalysis, RegimeDetector, RiskManager, send_telegram_message, TradeLog, DerivativesDataProvider

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
trade_log = TradeLog(exec_config.state_file.with_suffix('.trades.db'))
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
risk_manager = RiskManager(config, initial_capital=10000, state_manager=state_manager)
derivatives_provider = DerivativesDataProvider(config) if config.enable_derivatives_signals else None

# --- Logging Configuration ---
from core.logging_config import setup_logging
setup_logging(exec_config.log_file)

# Log configuration
logging.info(f"Using {config.trend_asset} for market regime detection")
logging.info(f"BTC bear-market exemption: {'ENABLED' if config.allow_btc_in_bear else 'DISABLED'}")
logging.info(f"Dual-signal regime detection: {'ENABLED' if config.enable_dual_regime else 'DISABLED'}")
logging.info(f"Bitcoin dominance tracking: {'ENABLED' if config.enable_btc_dominance else 'DISABLED'}")
logging.info(f"Derivatives signals (OKX): {'ENABLED' if config.enable_derivatives_signals else 'DISABLED'}")
logging.info(f"Trading mode: {exec_config.trading_mode}")

# Import strategy system
from strategies import create_strategy

# Cache both strategy instances for dynamic switching
_strategies = {
    "trend_following": create_strategy("trend_following", technical_analysis, config),
    "mean_reversion": create_strategy("mean_reversion", technical_analysis, config),
}

def select_strategy_for_regime(full_regime):
    """Select the best strategy for the current market regime."""
    if full_regime in ("STRONG_BULL", "BULL"):
        return _strategies["trend_following"]
    elif full_regime == "NEUTRAL":
        return _strategies["mean_reversion"]
    else:  # BEAR, STRONG_BEAR
        return _strategies["trend_following"]

if config.strategy == "auto":
    strategy = _strategies["trend_following"]  # default until first regime detection
    logging.info("Strategy: auto (dynamic regime-adaptive switching)")
else:
    strategy = _strategies[config.strategy]
    logging.info(f"Strategy: {strategy.name} (fixed)")

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
SHORT_WINDOW = config.ma_short_window
LONG_WINDOW = config.ma_long_window
ASSET_BLACKLIST = config.asset_blacklist
MOMENTUM_WINDOW_HOURS = config.momentum_window_hours
TOP_MOMENTUM_COUNT = config.top_momentum_count
TRAILING_STOP_PCT = float(config.trailing_stop_pct)
MIN_24H_VOLUME_USD = float(config.min_24h_volume_usd)
RSI_OVERBOUGHT = float(config.rsi_overbought)
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

def update_entry_price(executor_id, product_id, price, existing_qty=0.0, new_qty=0.0):
    """Wrapper for backward compatibility"""
    # Update state_manager's state_file in case STATE_FILE was patched (for tests)
    if state_manager.state_file != STATE_FILE:
        state_manager.state_file = STATE_FILE
        state_manager.lock_file = STATE_FILE.with_suffix('.lock')
    return state_manager.update_entry_price(executor_id, product_id, price, existing_qty, new_qty)

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


# --- Telegram Trade Notifications ---
def _notify_buy(ex_id, asset, price, buy_size, regime, candidate):
    rsi = candidate.get("rsi")
    momentum = candidate.get("momentum")
    rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"
    mom_str = f"{momentum:+.1f}%" if momentum is not None else "N/A"
    msg = (
        f"<b>BUY {asset}</b>\n"
        f"Price: ${price:,.2f}\n"
        f"Size: ${buy_size:,.2f}\n"
        f"Regime: {regime}\n"
        f"RSI: {rsi_str} | Momentum: {mom_str}\n"
        f"Executor: {ex_id}"
    )
    send_telegram_message(msg)


def _notify_sell(ex_id, asset, exit_price, entry, sell_amount, reason, pnl):
    pnl_emoji = "\U0001f4b0" if pnl >= 0 else "\U0001f534"
    msg = (
        f"<b>SELL {asset}</b> {pnl_emoji}\n"
        f"Exit: ${exit_price:,.2f}\n"
        f"Entry: ${entry:,.2f}\n"
        f"Amount: {sell_amount:.6f}\n"
        f"PnL: ${pnl:+.2f}\n"
        f"Reason: {reason}\n"
        f"Executor: {ex_id}"
    )
    send_telegram_message(msg)


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

def run_executor_strategy(executor, data_provider, market_regime, full_regime="BULL", reset_to_usdc=False, derivatives_signals=None):
    ex_id = executor.__class__.__name__
    if hasattr(executor, 'account') and executor.account:
        ex_id = f"{ex_id}_{executor.account.address[:6]}"

    # Pre-load state to discover tokens we hold in state but might not scan by default
    state = load_state()
    extra_tokens = []
    for key in state.get("entry_prices", {}):
        if not key.startswith(f"{ex_id}:"):
            continue
        # Extract asset symbol from "ExecutorId:ASSET-USDC"
        asset_sym = key[len(f"{ex_id}:"):].split("-")[0]
        if asset_sym not in ("USD", "USDC"):
            extra_tokens.append(asset_sym)

    # EthereumExecutor only scans USDC/WETH by default — pass extra tokens
    # so it also checks balances for assets we hold in state.
    try:
        bal = executor.get_balances(extra_tokens=extra_tokens) if extra_tokens else executor.get_balances()
    except TypeError:
        # Executor doesn't support extra_tokens (e.g. CoinbaseExecutor scans all)
        bal = executor.get_balances()

    # Handle separate available and total balances
    if "available" in bal and "total" in bal:
        cash_avail = bal["available"]["cash"].get("USDC", 0.0)
        held_avail = bal["available"]["crypto"]
        cash_total = bal["total"]["cash"].get("USDC", 0.0) + bal["total"]["cash"].get("USD", 0.0)
        held_total = bal["total"]["crypto"]
    else:
        # Fallback for other executors (legacy simple dict)
        cash_avail = cash_total = bal["cash"].get("USDC", 0.0)
        held_avail = held_total = bal["crypto"]

    state = load_state()

    # Reconcile state: clean up entry_prices for assets we no longer hold
    stale_keys = []
    for key in list(state.get("entry_prices", {}).keys()):
        if not key.startswith(f"{ex_id}:"):
            continue
        # Extract asset from key format "ExecutorId:ASSET-USDC"
        product_id_part = key[len(f"{ex_id}:"):]
        asset_part = product_id_part.split("-")[0]
        # Use total balance for state reconciliation (don't clear if held in order)
        balance = held_total.get(asset_part, 0.0)
        if balance <= 0:
            stale_keys.append(key)
    if stale_keys:
        for key in stale_keys:
            logging.warning(f"[{ex_id}] Removing orphaned state entry '{key}' — asset no longer held (manual sell?)")
            clear_entry_price(ex_id, key[len(f"{ex_id}:"):])
        state = load_state()  # reload after cleanup

    # Calculate local value using TOTAL balances
    ex_value = cash_total
    for asset, amt in held_total.items():
        details = data_provider.get_product_details(get_data_product_id(asset))
        if details:
            ex_value += amt * float(details['price'])
        else:
            logging.warning(f"[{ex_id}] Could not price {asset} (product_id={get_data_product_id(asset)}), excluding from portfolio value")

    logging.info(f"[{ex_id}] Sub-Portfolio Value: ${ex_value:,.2f} | USDC: ${cash_avail:,.2f}")

    # Guard against API failures reporting $0 portfolio (false 100% drawdown).
    # If ex_value is near-zero but we expect holdings, skip drawdown check.
    peak = risk_manager._get_peak_value(ex_id)
    if peak > 0 and ex_value < peak * 0.10:
        logging.error(
            f"[{ex_id}] Portfolio value ${ex_value:,.2f} is <10% of peak ${peak:,.2f} — "
            f"likely API failure, skipping drawdown check this cycle"
        )
        return ex_value

    # Update peak value and check drawdown (per-executor)
    trading_allowed, dd_reason = risk_manager.check_circuit_breakers(ex_value, ex_id)
    drawdown_pct = risk_manager.get_current_drawdown(ex_value, ex_id)

    if reset_to_usdc:
        for asset, amount in held_avail.items():
            if asset in ["USD", "USDC"] or is_asset_blacklisted(asset): continue
            executor.place_market_order(get_data_product_id(asset), 'SELL', amount_base_currency=amount)
            clear_entry_price(ex_id, get_data_product_id(asset))
        return ex_value

    # Skip new entries if strategy says so for this regime
    if strategy.should_skip_regime(market_regime, full_regime):
        logging.info(f"[{ex_id}] {full_regime} regime — {strategy.name} skipping new entries")

    # Scan for buys
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

    asset_candidates = strategy.rank_candidates(asset_candidates)

    # Derivatives: skip entries on OI bearish divergence
    if derivatives_signals and not derivatives_signals.entry_allowed:
        if asset_candidates:
            logging.info(f"[{ex_id}] Derivatives: OI divergence — skipping {len(asset_candidates)} entry candidates")
            asset_candidates = []

    # sort by assets already held so we can potentially top-up held positions and not
    # add to the max concurrency count.
    asset_candidates = sorted(asset_candidates[:TOP_MOMENTUM_COUNT], key=lambda x : held_total.get(x["asset"], 0) > 0, reverse=True)

    # Concurrent position guard: count non-stablecoin holdings for this executor
    current_positions = sum(1 for a in held_total if a not in ("USD", "USDC") and held_total[a] > 0)
    max_positions = config.max_concurrent_positions

    available_usdc = cash_avail
    for candidate in asset_candidates[:TOP_MOMENTUM_COUNT]:
        asset, product_id = candidate["asset"], candidate["product_id"]
        already_holds = held_total.get(asset, 0) > 0
        if current_positions >= max_positions and not already_holds:
            logging.info(f"[{ex_id}] Skipping {asset}: at max concurrent positions ({current_positions}/{max_positions})")
            continue
        try:
            price_data = data_provider.get_product_details(product_id)
            price = float(price_data['price'])
            current_asset_value = held_total.get(asset, 0) * price

            # Calculate ATR for volatility-adjusted position sizing
            atr = None
            if hasattr(data_provider, 'get_market_data'):
                df = data_provider.get_market_data(product_id, 20)
                if df is not None:
                    atr = technical_analysis.calculate_atr(df)

            buy_size, size_msg = risk_manager.calculate_position_with_existing(
                portfolio_value=ex_value,
                price=price,
                existing_positions=current_positions,
                max_positions=max_positions,
                current_asset_value=current_asset_value,
                available_cash=available_usdc,
                executor_value=ex_value,
                atr=atr
            )
            # Scale down position in BEAR regime
            if market_regime == "BEAR" and config.bear_position_scale < 1.0:
                buy_size *= config.bear_position_scale
                if buy_size >= float(config.min_order_usd):
                    logging.info(f"[{ex_id}] Bear scaling: {asset} position reduced to {config.bear_position_scale:.0%}")
            # Derivatives position modifier (funding rate + L/S ratio)
            if derivatives_signals and derivatives_signals.position_modifier != 1.0:
                deriv_mod = derivatives_signals.position_modifier
                buy_size *= deriv_mod
                logging.info(f"[{ex_id}] Derivatives modifier: {asset} position scaled by {deriv_mod:.2f}x")
            if buy_size < float(config.min_order_usd):
                if size_msg != "Normal position sizing":
                    logging.info(f"[{ex_id}] Skipping {asset}: {size_msg}")
                continue
            if not trading_allowed:
                continue  # Skip buys when circuit breakers are active
            if buy_size >= float(config.min_order_usd):
                existing_qty = held_total.get(asset, 0)
                new_qty = buy_size / price
                result = executor.place_limit_order(product_id, 'BUY', price, amount_quote_currency=buy_size)
                if result:
                    # Confirm fill before updating entry price
                    order_id = None
                    if isinstance(result, dict):
                        # Check if on-chain swap was confirmed via receipt
                        if result.get("confirmed"):
                            update_entry_price(ex_id, product_id, price, existing_qty, new_qty)
                            available_usdc -= buy_size
                            if not already_holds:
                                current_positions += 1
                            logging.info(f"[{ex_id}] Buy {asset} confirmed on-chain at ${price:,.2f}")
                            _notify_buy(ex_id, asset, price, buy_size, full_regime, candidate)
                            trade_log.record_buy(
                                timestamp=datetime.datetime.now().isoformat(),
                                executor_id=ex_id, asset=asset, product_id=product_id,
                                price=price, quantity=new_qty, usd_value=buy_size,
                                market_regime=full_regime,
                                rsi=candidate.get("rsi"), momentum=candidate.get("momentum"))
                        elif result.get("success") is False:
                            logging.warning(f"[{ex_id}] Buy {asset} failed: {result.get('error', 'unknown')}")
                        else:
                            # Coinbase path: check order_id for fill confirmation
                            order_id = result.get("order_id") or result.get("success_response", {}).get("order_id") if isinstance(result.get("success_response"), dict) else None
                            if order_id and hasattr(executor, 'check_order_filled'):
                                fill_info = executor.check_order_filled(order_id)
                                if fill_info:
                                    filled_price = fill_info["price"]
                                    new_qty_filled = buy_size / filled_price
                                    update_entry_price(ex_id, product_id, filled_price, existing_qty, new_qty_filled)
                                    available_usdc -= buy_size
                                    if not already_holds:
                                        current_positions += 1
                                    logging.info(f"[{ex_id}] Buy {asset} confirmed at ${filled_price:,.2f}")
                                    _notify_buy(ex_id, asset, filled_price, buy_size, full_regime, candidate)
                                    trade_log.record_buy(
                                        timestamp=datetime.datetime.now().isoformat(),
                                        executor_id=ex_id, asset=asset, product_id=product_id,
                                        price=filled_price, quantity=new_qty_filled, usd_value=buy_size,
                                        market_regime=full_regime,
                                        rsi=candidate.get("rsi"), momentum=candidate.get("momentum"))
                                else:
                                    logging.warning(f"[{ex_id}] Buy {asset} order {order_id} not confirmed filled, skipping entry update")
                            else:
                                # Paper mode — use requested price
                                update_entry_price(ex_id, product_id, price, existing_qty, new_qty)
                                available_usdc -= buy_size
                                if not already_holds:
                                    current_positions += 1
                                _notify_buy(ex_id, asset, price, buy_size, full_regime, candidate)
                                trade_log.record_buy(
                                    timestamp=datetime.datetime.now().isoformat(),
                                    executor_id=ex_id, asset=asset, product_id=product_id,
                                    price=price, quantity=new_qty, usd_value=buy_size,
                                    market_regime=full_regime,
                                    rsi=candidate.get("rsi"), momentum=candidate.get("momentum"))
        except Exception as e:
            logging.error(f"[{ex_id}] Error evaluating {asset} for buy: {e}")

    # Manage Sells
    for asset, amt in held_avail.items():
        if asset in ["USD", "USDC"]: continue
        product_id = get_data_product_id(asset)
        try:
            price_data = data_provider.get_product_details(product_id)
            if not price_data: continue
            price = float(price_data['price'])

            # Skip dust balances too small to trade
            if amt * price < float(config.min_order_usd):
                continue

            entry_key = f"{ex_id}:{product_id}"
            entry = state.get("entry_prices", {}).get(entry_key)

            if not entry:
                # Auto-adopt: set current price as entry so the position gets managed
                logging.warning(f"[{ex_id}] Auto-adopting unmanaged {asset} ({amt:.6f}) at current price ${price:,.2f}")
                update_entry_price(ex_id, product_id, price)
                state = load_state()
                entry = price
                # Skip exit checks this cycle — let it establish a HWM first
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
                is_stop_loss = "stop" in reason.lower()
                if is_stop_loss and hasattr(executor, 'place_aggressive_limit_order'):
                    # Stop-losses use aggressive limit (no post_only) for immediate fill with price protection
                    result = executor.place_aggressive_limit_order(product_id, 'SELL', price, amount_base_currency=sell_amount)
                else:
                    result = executor.place_limit_order(product_id, 'SELL', price, amount_base_currency=sell_amount)
                # Fallback to market order for stop losses if limit fails
                if not result and is_stop_loss:
                    logging.warning(f"[{ex_id}] Aggressive limit sell failed for stop loss on {asset}, falling back to market order")
                    result = executor.place_market_order(product_id, 'SELL', amount_base_currency=sell_amount)
                if result:
                    # Handle dust_skip — position is effectively gone, clean up state
                    if isinstance(result, dict) and result.get("tx_hash") == "dust_skip":
                        logging.warning(f"[{ex_id}] {asset} sell was dust-skipped (amount too small). Clearing entry price.")
                        clear_entry_price(ex_id, product_id)
                        state = load_state()
                        continue
                    # Confirm fill and get actual exit price + fee
                    exit_price = price  # default to requested price
                    actual_sell_fee = None  # set by check_order_filled if available
                    if isinstance(result, dict) and result.get("success") is False:
                        # Last resort: market order fallback for stop losses
                        if is_stop_loss:
                            logging.warning(f"[{ex_id}] Limit sell rejected for stop loss on {asset}: {result.get('error', 'unknown')}. Last-resort market order.")
                            result = executor.place_market_order(product_id, 'SELL', amount_base_currency=sell_amount)
                            if not result or (isinstance(result, dict) and result.get("success") is False):
                                logging.error(f"[{ex_id}] Market order fallback also failed for {asset}")
                                continue
                        else:
                            logging.warning(f"[{ex_id}] Sell {asset} failed: {result.get('error', 'unknown')}")
                            continue
                    if isinstance(result, dict) and result.get("confirmed"):
                        logging.info(f"[{ex_id}] Sell {asset} confirmed on-chain at ${price:,.2f}")
                    elif isinstance(result, dict):
                        order_id = result.get("order_id") or result.get("success_response", {}).get("order_id") if isinstance(result.get("success_response"), dict) else None
                        if order_id and hasattr(executor, 'check_order_filled'):
                            fill_info = executor.check_order_filled(order_id)
                            if fill_info:
                                exit_price = fill_info["price"]
                                actual_sell_fee = fill_info["fee"]
                                logging.info(f"[{ex_id}] Sell {asset} confirmed at ${exit_price:,.2f}")
                            else:
                                logging.warning(f"[{ex_id}] Sell {asset} order {order_id} not confirmed filled, using requested price for PnL")

                    logging.info(f"[{ex_id}] ✅ Sold {sell_amount:.6f} {asset} at ${exit_price:,.2f}")
                    # Track PnL (fee-aware)
                    # Uses actual fees from Coinbase API when available,
                    # falls back to estimated round-trip fee for on-chain/paper trades.
                    if entry:
                        if actual_sell_fee is not None:
                            # Actual sell fee from API; estimate buy-side fee (not tracked at buy time)
                            buy_fee_est = risk_manager.calculate_fees(entry * sell_amount, is_round_trip=False)
                            fee_cost = buy_fee_est + actual_sell_fee
                        else:
                            fee_cost = risk_manager.calculate_fees(entry * sell_amount, is_round_trip=True)
                        pnl = (exit_price - entry) * sell_amount - fee_cost
                        is_win = pnl > 0
                        record_trade(is_win, pnl)
                        logging.info(f"[{ex_id}] 📊 PnL: ${pnl:+.2f} (entry=${entry:,.2f} -> exit=${exit_price:,.2f}, fees=${fee_cost:.2f})")
                        _notify_sell(ex_id, asset, exit_price, entry, sell_amount, reason, pnl)
                        trade_log.record_sell(
                            timestamp=datetime.datetime.now().isoformat(),
                            executor_id=ex_id, asset=asset, product_id=product_id,
                            price=exit_price, quantity=sell_amount,
                            entry_price=entry, pnl=pnl, fee=fee_cost,
                            reason=reason, market_regime=full_regime, hwm=hwm)
                    if sell_ratio == 1.0:
                        clear_entry_price(ex_id, product_id)
                    else:
                        # Partial sell — save updated TP flags (already saved above)
                        pass
        except Exception as e:
            logging.error(f"[{ex_id}] Error managing sell for {asset}: {e}")

    return ex_value

PERIODIC_STATUS_INTERVAL = 4 * 3600  # 4 hours in seconds


def _build_status_message(full_regime, data_provider, aggregate_value, state, derivatives_signals=None):
    """Build a periodic status summary message."""
    lines = [f"<b>Status Update</b>", f"Regime: {full_regime}"]

    if derivatives_signals and derivatives_signals.funding:
        f = derivatives_signals.funding
        lines.append(f"Funding: {f.avg_rate*100:.4f}% ({f.signal}) | Pos modifier: {derivatives_signals.position_modifier:.2f}x")
        for flag in derivatives_signals.caution_flags:
            lines.append(f"  * {flag}")

    # Price + indicator info for each supported asset
    for asset in ["BTC", "ETH"]:
        product_id = get_data_product_id(asset)
        try:
            price_data = data_provider.get_product_details(product_id)
            if not price_data:
                continue
            price = float(price_data['price'])
            df = data_provider.get_market_data(product_id, LONG_WINDOW)
            ma_s, ma_l = analyze_trend(df)
            rsi = calculate_rsi(df) if df is not None else None
            momentum = get_momentum_ranking(df) if df is not None else None

            line = f"\n<b>{asset}</b>: ${price:,.0f}"
            if ma_s and ma_l:
                line += f" | MA{SHORT_WINDOW}: ${ma_s:,.0f} | MA{LONG_WINDOW}: ${ma_l:,.0f}"
            if rsi is not None:
                line += f" | RSI: {rsi:.0f}"
            if momentum is not None:
                line += f" | Mom: {momentum:+.1f}%"
            lines.append(line)
        except Exception:
            pass

    lines.append(f"\nPortfolio: ${aggregate_value:,.2f}")

    # Open positions
    entry_prices = state.get("entry_prices", {})
    if entry_prices:
        lines.append("\n<b>Open Positions:</b>")
        for key, entry in entry_prices.items():
            asset_sym = key.split(":")[-1].split("-")[0]
            try:
                pd_id = get_data_product_id(asset_sym)
                pd_data = data_provider.get_product_details(pd_id)
                if pd_data:
                    cur_price = float(pd_data['price'])
                    upnl = (cur_price - entry) / entry * 100
                    lines.append(f"  {asset_sym}: entry=${entry:,.2f} now=${cur_price:,.2f} ({upnl:+.1f}%)")
            except Exception:
                lines.append(f"  {asset_sym}: entry=${entry:,.2f}")

    return "\n".join(lines)


def _maybe_send_periodic_status(full_regime, data_provider, aggregate_value, derivatives_signals=None):
    """Send a Telegram status summary every 4 hours."""
    state = load_state()
    now = time.time()
    last_ts = state.get("last_periodic_status_ts", 0)
    if now - last_ts < PERIODIC_STATUS_INTERVAL:
        return
    msg = _build_status_message(full_regime, data_provider, aggregate_value, state, derivatives_signals=derivatives_signals)
    if send_telegram_message(msg):
        state["last_periodic_status_ts"] = now
        save_state(state)
        logging.info("Periodic status sent via Telegram")


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
    logging.info(f"[Risk] max_drawdown={config.max_drawdown_pct}% | min_order=${float(config.min_order_usd):.0f}")
    logging.info(f"[Risk] portfolio_risk={float(config.portfolio_risk_pct)*100:.0f}% | risk_per_trade={float(config.risk_per_trade_pct)*100:.0f}%")
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
            btc_price = btc_df['close'].iloc[-1] if len(btc_df) > 0 else None
            ma_spread_pct = ((btc_s / btc_l) - 1) * 100
            if btc_s > btc_l * 1.002 and btc_price > btc_l:
                btc_macro = "BULL"
            elif btc_s < btc_l * 0.998 or btc_price < btc_l * 0.996:
                btc_macro = "BEAR"
            else:
                btc_macro = "FLAT"
            logging.info(f"BTC Macro: ${btc_price:,.0f} | MA{SHORT_WINDOW}: ${btc_s:,.0f} | MA{LONG_WINDOW}: ${btc_l:,.0f} | Spread: {ma_spread_pct:+.2f}% → {btc_macro}")
        else:
            btc_macro = "BULL"  # Default when insufficient data
            logging.warning("BTC Macro: Insufficient data, defaulting to BULL")

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

        # Enhanced regime logging with reasoning
        regime_explanation = {
            "STRONG_BULL": "BTC trending up + Alts outperforming (ETH leading)",
            "BULL": "BTC trending up + BTC outperforming (capital in BTC)",
            "NEUTRAL": "BTC sideways or conflicting signals",
            "BEAR": "BTC trending down + Alts holding up (ETH outperforming)",
            "STRONG_BEAR": "BTC trending down + Alts dumping harder (high risk)"
        }
        explanation = regime_explanation.get(full_regime, "Unknown regime")

        dom_info = ""
        if btc_dominance:
            dom_info = f" | BTC.D: {btc_dominance['btc_dominance']:.1f}% ({btc_dominance['regime']})"

        logging.info(f"━━━ Market Regime: {full_regime} ━━━")
        logging.info(f"    Reason: {explanation}")
        logging.info(f"    Signals: BTC {btc_macro} | Rotation {rotation_signal}{dom_info}")
        logging.info(f"    Strategy input: {market_regime}")

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

    # Regime hysteresis: require N consecutive confirmations before switching
    # This prevents whipsawing between regimes on noisy signals.
    REGIME_CONFIRM_COUNT = 3
    state = load_state()
    prev_regime = state.get("confirmed_regime", full_regime)
    if full_regime != prev_regime:
        streak = state.get("regime_streak", 0) + 1
        state["regime_streak"] = streak
        if streak >= REGIME_CONFIRM_COUNT:
            logging.info(f"Regime change confirmed: {prev_regime} → {full_regime} (after {streak} consecutive signals)")
            state["confirmed_regime"] = full_regime
            state["regime_streak"] = 0
            save_state(state)
        else:
            logging.info(f"Regime signal: {full_regime} (pending confirmation {streak}/{REGIME_CONFIRM_COUNT}, using {prev_regime})")
            full_regime = prev_regime  # Keep using previous confirmed regime
            market_regime = regime_to_legacy(full_regime)
            save_state(state)
    else:
        # Same regime — reset streak
        if state.get("regime_streak", 0) != 0:
            state["regime_streak"] = 0
            save_state(state)

    # Dynamic strategy switching based on regime
    global strategy
    if config.strategy == "auto":
        prev = strategy.name
        strategy = select_strategy_for_regime(full_regime)
        if strategy.name != prev:
            logging.info(f"Strategy switched: {prev} → {strategy.name} (regime: {full_regime})")
        else:
            logging.info(f"Strategy: {strategy.name} (regime: {full_regime})")

    # Fetch derivatives signals once per cycle (cached, so cheap)
    deriv_signals = None
    if derivatives_provider:
        try:
            btc_price_change = None
            btc_df = data_provider.get_market_data(get_data_product_id("BTC"), 25)
            if btc_df is not None and len(btc_df) >= 24:
                btc_price_change = ((btc_df['close'].iloc[-1] / btc_df['close'].iloc[-24]) - 1) * 100
            deriv_signals = derivatives_provider.get_derivatives_signals(btc_price_change)
            funding_str = f"{deriv_signals.funding.signal}({deriv_signals.funding.avg_rate*100:.4f}%)" if deriv_signals.funding else "N/A"
            oi_str = f"{deriv_signals.oi.signal}({deriv_signals.oi.change_pct:+.1f}%)" if deriv_signals.oi else "N/A"
            ls_str = f"{deriv_signals.ls_ratio.signal}({deriv_signals.ls_ratio.long_ratio:.0%})" if deriv_signals.ls_ratio else "N/A"
            logging.info(f"Derivatives: funding={funding_str} | OI={oi_str} | L/S={ls_str} | modifier={deriv_signals.position_modifier:.2f}x")
            for flag in deriv_signals.caution_flags:
                logging.warning(f"Derivatives: {flag}")
        except Exception as e:
            logging.warning(f"Derivatives data fetch failed: {e}")

    aggregate_value = 0
    for ex in active_executors:
        try:
            aggregate_value += run_executor_strategy(ex, data_provider, market_regime, full_regime, reset_to_usdc, derivatives_signals=deriv_signals)
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
    logging.info(f"Risk Params: max_dd={config.max_drawdown_pct}% | trailing_stop={TRAILING_STOP_PCT*100:.0f}%")
    logging.info(f"Take-Profit: TP1={TAKE_PROFIT_1_PCT*100:.0f}%/{TAKE_PROFIT_1_SELL_RATIO*100:.0f}% | TP2={TAKE_PROFIT_2_PCT*100:.0f}%/{TAKE_PROFIT_2_SELL_RATIO*100:.0f}%")
    logging.info(f"==================")

    _maybe_send_periodic_status(full_regime, data_provider, aggregate_value, derivatives_signals=deriv_signals)

def run_ws_mode():
    """Run bot in WebSocket mode for real-time exit checks."""
    import asyncio
    from core import CoinbaseWSClient

    if not acquire_run_lock():
        logging.warning("Another bot instance is already running, exiting.")
        return
    try:
        cb_executor = CoinbaseExecutor(API_JSON_FILE, TRADING_MODE)

        # In-memory snapshots refreshed each scan cycle — ticks never hit
        # the filesystem or REST API unless an exit is actually triggered.
        _candle_cache = {}   # product_id -> df
        _state_cache = {}    # full state dict
        _held_entries = {}   # entry_key -> entry_price (fast tick filter)
        _balances = {}       # asset -> amount

        def _refresh_snapshots():
            """Reload state, balances, and candles from source of truth."""
            state = load_state()
            _state_cache.clear()
            _state_cache.update(state)

            bal = cb_executor.get_balances()
            _balances.clear()
            _balances.update(bal.get("crypto", {}))

            _held_entries.clear()
            ex_id = "CoinbaseExecutor"
            for key, entry_price in state.get("entry_prices", {}).items():
                if key.startswith(f"{ex_id}:"):
                    _held_entries[key] = entry_price

            # Pre-populate candle cache for held assets
            _candle_cache.clear()
            for key in _held_entries:
                product_id = key[len(f"{ex_id}:"):]
                df = cb_executor.get_market_data(product_id, LONG_WINDOW)
                if df is not None:
                    _candle_cache[product_id] = df

        def on_tick(product_id, price):
            ex_id = "CoinbaseExecutor"
            entry_key = f"{ex_id}:{product_id}"

            # Fast path: skip products we don't hold (no I/O)
            entry = _held_entries.get(entry_key)
            if entry is None:
                return

            asset = product_id.split("-")[0]
            amt = _balances.get(asset, 0)
            if amt * price < float(config.min_order_usd):
                return  # dust

            df = _candle_cache.get(product_id)
            if df is None:
                return  # no candles yet, wait for next scan cycle

            state = _state_cache
            hwm = state.get("high_water_marks", {}).get(entry_key, entry)
            if price > hwm:
                hwm = price
                state.setdefault("high_water_marks", {})[entry_key] = hwm
                save_state(state)

            tp_flags = state.get("take_profit_flags", {}).get(
                entry_key, {"tp1_hit": False, "tp2_hit": False, "trend_exit_hit": False}
            )

            sell_trigger, sell_ratio, reason, tp_flags = strategy.check_exit(
                asset, product_id, df, price, entry, hwm, tp_flags, state, entry_key
            )

            if not sell_trigger:
                return

            state.setdefault("take_profit_flags", {})[entry_key] = tp_flags
            save_state(state)

            logging.info(f"[WS] {reason}")
            # Fetch fresh balance for the actual sell amount
            fresh_bal = cb_executor.get_balances()
            amt = fresh_bal.get("crypto", {}).get(asset, amt)
            sell_amount = amt * sell_ratio
            is_stop_loss = "stop" in reason.lower()
            result = cb_executor.place_limit_order(
                product_id, 'SELL', price, amount_base_currency=sell_amount
            )
            if not result and is_stop_loss:
                logging.warning(f"[WS] Limit sell failed for stop loss on {asset}, "
                                "falling back to market order")
                result = cb_executor.place_market_order(
                    product_id, 'SELL', amount_base_currency=sell_amount
                )
            if result:
                exit_price = price
                if isinstance(result, dict) and result.get("success") is False:
                    if is_stop_loss:
                        result = cb_executor.place_market_order(
                            product_id, 'SELL', amount_base_currency=sell_amount
                        )
                        if not result or (isinstance(result, dict) and result.get("success") is False):
                            logging.error(f"[WS] Market order fallback also failed for {asset}")
                            return
                    else:
                        return
                logging.info(f"[WS] Sold {sell_amount:.6f} {asset} at ${exit_price:,.2f}")
                if entry:
                    fee_cost = risk_manager.calculate_fees(entry * sell_amount, is_round_trip=True)
                    pnl = (exit_price - entry) * sell_amount - fee_cost
                    record_trade(pnl > 0, pnl)
                    logging.info(f"[WS] PnL: ${pnl:+.2f}")
                if sell_ratio == 1.0:
                    clear_entry_price("CoinbaseExecutor", product_id)
                    del _held_entries[entry_key]
                    _balances.pop(asset, None)

        def on_scan_cycle():
            logging.info("[WS] Running periodic full scan...")
            _run_bot()
            _refresh_snapshots()
            # Update WS subscriptions to match current holdings
            held_products = [k.split(":", 1)[1] for k in _held_entries]
            if held_products:
                client.update_subscriptions(
                    list(set(product_ids + held_products))
                )

        # Build product IDs for subscription
        supported = cb_executor.get_supported_assets()
        product_ids = [get_data_product_id(a) for a in supported
                       if not is_asset_blacklisted(a)]

        # Initial snapshot before WS connects
        _refresh_snapshots()

        shutdown_event = asyncio.Event()

        def _signal_shutdown(signum, frame):
            logging.info("Shutdown signal received, stopping WS mode...")
            shutdown_event.set()

        signal.signal(signal.SIGTERM, _signal_shutdown)
        signal.signal(signal.SIGINT, _signal_shutdown)

        client = CoinbaseWSClient(
            jwt_builder=cb_executor.build_ws_jwt,
            product_ids=product_ids,
            on_tick=on_tick,
            on_scan_cycle=on_scan_cycle,
            scan_interval=config.ws_scan_interval,
            shutdown_event=shutdown_event,
        )

        logging.info(f"Starting WebSocket mode (scan every {config.ws_scan_interval}s)")
        asyncio.run(client.run())
    finally:
        release_run_lock()


if __name__ == "__main__":
    if "--ws" in sys.argv:
        run_ws_mode()
    elif "--report" in sys.argv:
        perf = get_performance()
        total = perf.get("total_trades", 0)
        wins = perf.get("winning_trades", 0)
        losses = perf.get("losing_trades", 0)
        win_rate = (wins / total * 100) if total > 0 else 0
        pf = perf.get("profit_factor", 0)
        pf_str = f"{pf:.2f}" if pf != float('inf') else "inf"
        print(f"=== Trading Bot Performance ===")
        print(f"Total Trades: {total}")
        print(f"Winning: {wins} | Losing: {losses}")
        print(f"Win Rate: {win_rate:.1f}%")
        print(f"Profit Factor: {pf_str} (gross_profit / gross_loss, >1.0 = profitable)")
        print(f"Avg Win: ${perf.get('avg_win', 0):+.2f} | Avg Loss: ${perf.get('avg_loss', 0):.2f}")
        print(f"Total PnL: ${perf.get('total_pnl', 0):+.2f}")
        print(f"Gross Profit: ${perf.get('gross_profit', 0):+.2f} | Gross Loss: ${perf.get('gross_loss', 0):.2f}")
        print(f"Runs: {perf.get('run_count', 0)}")
        print(f"Last Run: {perf.get('last_run_time', 'N/A')}")
    elif "--trades" in sys.argv:
        # Parse optional filters from argv
        def _get_arg(flag, default=None):
            try:
                idx = sys.argv.index(flag)
                return sys.argv[idx + 1]
            except (ValueError, IndexError):
                return default

        t_asset = _get_arg("--asset")
        t_regime = _get_arg("--regime")
        t_since = _get_arg("--since")
        t_until = _get_arg("--until")
        t_side = _get_arg("--side")
        t_limit = int(_get_arg("--limit", "50"))

        if "--summary" in sys.argv:
            s = trade_log.summary(asset=t_asset, since=t_since)
            print(f"=== Trade Summary ===")
            print(f"Total Trades: {s['total_trades']}")
            print(f"Wins: {s['wins']} | Losses: {s['losses']}")
            print(f"Win Rate: {s['win_rate']:.1f}%")
            print(f"Avg Win: ${s['avg_win']:+,.2f} | Avg Loss: ${s['avg_loss']:,.2f}")
            print(f"Total PnL: ${s['total_pnl']:+,.2f}")
        else:
            rows = trade_log.query(
                asset=t_asset, side=t_side, regime=t_regime,
                since=t_since, until=t_until, limit=t_limit)
            print(TradeLog.format_table(rows))
    else:
        run_bot(reset_to_usdc="--reset" in sys.argv)
