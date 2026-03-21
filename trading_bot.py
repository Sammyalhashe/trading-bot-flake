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
_HOME = os.path.expanduser("~")
LOG_FILE = os.environ.get("TRADING_LOG_FILE", os.path.join(_HOME, ".openclaw", "workspace", "trading-bot", "trading.log"))
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
API_JSON_FILE = os.environ.get("COINBASE_API_JSON", os.path.join(_HOME, "cdb_api_key.json"))
STATE_FILE = Path(os.environ.get("TRADING_STATE_FILE", os.path.join(_HOME, "trading-bot-flake", "trading_state.json")))
TRADING_MODE = os.environ.get("TRADING_MODE", "paper").lower()

ENABLE_ETHEREUM = os.environ.get("ENABLE_ETHEREUM", "false").lower() == "true"
ETH_RPC_URL = os.environ.get("ETH_RPC_URL")
ETH_PRIVATE_KEY = os.environ.get("ETH_PRIVATE_KEY")

# Enable/disable short selling
ENABLE_SHORT = os.environ.get("ENABLE_SHORT", "true").lower() == "true"

# Strategy & Risk
SHORT_WINDOW = 20
LONG_WINDOW = 50
PORTFOLIO_RISK_PERCENTAGE = float(os.environ.get("PORTFOLIO_RISK_PERCENTAGE", "0.15"))
SHORT_RISK_PERCENTAGE = float(os.environ.get("SHORT_RISK_PERCENTAGE", "0.05"))  # 5% for shorts
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

# Volume & RSI Filters
MIN_24H_VOLUME_USD = float(os.environ.get("MIN_24H_VOLUME_USD", "100000"))
RSI_OVERBOUGHT = float(os.environ.get("RSI_OVERBOUGHT", "70"))

# Fee-aware P&L
ROUND_TRIP_FEE_PCT = float(os.environ.get("ROUND_TRIP_FEE_PCT", "0.006"))

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

# --- Process-level lock (prevents overlapping runs from systemd timer) ---
_RUN_LOCK_FILE = STATE_FILE.with_suffix('.runlock')
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

# --- State Management ---
_STATE_LOCK_FILE = STATE_FILE.with_suffix('.lock')

def _acquire_state_lock():
    """Acquire file lock for state access. Returns lock file handle."""
    os.makedirs(os.path.dirname(_STATE_LOCK_FILE), exist_ok=True)
    lock_fd = open(_STATE_LOCK_FILE, 'w')
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    return lock_fd

def _release_state_lock(lock_fd):
    """Release file lock for state access."""
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
    except Exception:
        pass

def load_state():
    default_state = {"entry_prices": {}, "high_water_marks": {}, "take_profit_flags": {}}
    lock_fd = _acquire_state_lock()
    try:
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                state.setdefault("entry_prices", {})
                state.setdefault("high_water_marks", {})
                state.setdefault("take_profit_flags", {})
                return state
            except Exception as e:
                logging.error(f"Failed to load state: {e}")
                return default_state
        return default_state
    finally:
        _release_state_lock(lock_fd)

def save_state(state):
    lock_fd = _acquire_state_lock()
    try:
        tmp = STATE_FILE.with_suffix('.tmp')
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logging.error(f"Failed to save state: {e}")
    finally:
        _release_state_lock(lock_fd)

def update_entry_price(executor_id, product_id, price):
    state = load_state()
    key = f"{executor_id}:{product_id}"
    state.setdefault("entry_prices", {})[key] = price
    # Initialize high water mark to entry price
    state.setdefault("high_water_marks", {})[key] = price
    # Reset take-profit flags for new entry
    state.setdefault("take_profit_flags", {})[key] = {"tp1_hit": False, "tp2_hit": False, "trend_exit_hit": False}
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

def load_peak_value(executor_id="default"):
    state = load_state()
    peaks = state.get("peak_portfolio_values", {})
    return peaks.get(executor_id, 0.0)

def save_peak_value(value, executor_id="default"):
    state = load_state()
    peaks = state.setdefault("peak_portfolio_values", {})
    peaks[executor_id] = value
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
    """Compute short and long moving averages for trend detection.

    The bot uses MA crossover to determine market direction:
      - short_MA > long_MA * 1.002 → uptrend (BUY signal, 0.2% buffer avoids noise)
      - short_MA < long_MA * 0.998 → downtrend (SELL/SHORT signal)
    The 0.2% buffer prevents whipsawing on flat markets.
    """
    if df is None or len(df) < LONG_WINDOW: return None, None
    s_ma = df['close'].rolling(window=SHORT_WINDOW).mean().iloc[-1]
    l_ma = df['close'].rolling(window=LONG_WINDOW).mean().iloc[-1]
    return s_ma, l_ma

def calculate_rsi(df, period=14):
    """Calculate RSI (Relative Strength Index) from candle close prices.

    RSI measures momentum on a 0–100 scale:
      - RSI > 70 → overbought (price rose too fast, likely to pull back)
      - RSI < 30 → oversold  (price dropped too fast, likely to bounce)

    Formula:
      RS  = avg_gain / avg_loss   (over `period` bars)
      RSI = 100 - 100/(1 + RS)

    When gains dominate, RS is large → RSI approaches 100.
    When losses dominate, RS is small → RSI approaches 0.
    """
    if df is None or len(df) < period + 1:
        return None
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]

def calculate_atr(df, period=14):
    """Calculate ATR (Average True Range) from candle OHLC data.

    ATR measures volatility — the average size of recent price swings.
    Used to set trailing stops that adapt to current market conditions:
      - High ATR → wider stop (volatile market, avoid getting stopped out by noise)
      - Low ATR  → tighter stop (calm market, protect gains more aggressively)

    True Range for each bar is the largest of:
      1. high - low                (intra-bar range)
      2. |high - previous close|   (gap up)
      3. |low  - previous close|   (gap down)

    ATR = simple moving average of True Range over `period` bars.
    """
    if df is None or len(df) < period + 1:
        return None
    high = df['high']
    low = df['low']
    prev_close = df['close'].shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean().iloc[-1]
    return atr

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

    # Scan for buys
    trade_limit = ex_value * PORTFOLIO_RISK_PERCENTAGE
    short_limit = ex_value * SHORT_RISK_PERCENTAGE
    asset_candidates = []
    # Major assets supported
    for asset in ["BTC", "ETH", "MATIC", "AVAX", "SUI"]:
        if is_asset_blacklisted(asset): continue
        product_id = get_data_product_id(asset)
        try:
            df = data_provider.get_market_data(product_id, LONG_WINDOW)
            ma_s, ma_l = analyze_trend(df)
            if ma_s and ma_l and ma_s > ma_l * 1.002 and (btc_trend == "BULL" or asset == "BTC"):
                # Volume filter: check 24h USD volume
                if df is not None and len(df) >= 24:
                    volume_24h = df['volume'].iloc[-24:].sum()
                    close_price = df['close'].iloc[-1]
                    usd_volume_24h = volume_24h * close_price
                    if usd_volume_24h < MIN_24H_VOLUME_USD:
                        logging.info(f"[{ex_id}] Skipping {asset}: 24h USD volume ${usd_volume_24h:,.0f} below minimum ${MIN_24H_VOLUME_USD:,.0f}")
                        continue
                # RSI filter: skip overbought assets
                rsi = calculate_rsi(df)
                if rsi is not None:
                    logging.info(f"[{ex_id}] {asset} RSI: {rsi:.1f}")
                    if rsi > RSI_OVERBOUGHT:
                        logging.info(f"[{ex_id}] Skipping {asset}: RSI {rsi:.1f} > {RSI_OVERBOUGHT} (overbought)")
                        continue
                asset_candidates.append({"asset": asset, "product_id": product_id, "momentum": get_momentum_ranking(df)})
        except Exception as e: logging.error(f"[{ex_id}] Error analyzing {asset}: {e}")

    # Short candidates in BEAR market (momentum is inverted for shorts)
    short_candidates = []
    if btc_trend == "BEAR":
        for asset in ["BTC", "ETH", "MATIC", "AVAX", "SUI"]:
            if is_asset_blacklisted(asset): continue
            product_id = get_data_product_id(asset)
            try:
                df = data_provider.get_market_data(product_id, LONG_WINDOW)
                ma_s, ma_l = analyze_trend(df)
                if ma_s and ma_l and ma_s < ma_l * 0.998:  # Downtrend
                    # Volume filter
                    if df is not None and len(df) >= 24:
                        volume_24h = df['volume'].iloc[-24:].sum()
                        close_price = df['close'].iloc[-1]
                        usd_volume_24h = volume_24h * close_price
                        if usd_volume_24h < MIN_24H_VOLUME_USD:
                            continue
                    short_candidates.append({"asset": asset, "product_id": product_id, "momentum": get_momentum_ranking(df)})
            except Exception as e: logging.error(f"[{ex_id}] Error analyzing short {asset}: {e}")

        short_candidates.sort(key=lambda x: x["momentum"])  # Most negative momentum first

    asset_candidates.sort(key=lambda x: x["momentum"], reverse=True)

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
            if drawdown_pct >= MAX_DRAWDOWN_PCT and btc_trend == "BULL":
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
    if ENABLE_SHORT:
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
        logging.info(f"[{ex_id}] Short selling disabled via ENABLE_SHORT=False")

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

            # Priority 3: Trailing Stop-Loss (ATR-based dynamic stop)
            #
            # Instead of a fixed % stop, we use ATR to adapt to volatility:
            #   atr_stop = 2 * ATR / price
            # This gives the stop as a fraction of current price. Multiplying ATR
            # by 2 means we tolerate ~2 average bars of adverse movement before
            # triggering. The result is clamped to [2%, 15%] to avoid extremes.
            #
            # The stop trails the High Water Mark (HWM), not the entry price.
            # As price rises, HWM rises with it, ratcheting the stop upward.
            # Sell triggers when: price < HWM * (1 - atr_stop)
            elif True:
                atr = calculate_atr(df)
                if atr is not None and price > 0:
                    atr_stop = 2 * atr / price
                    atr_stop = max(0.02, min(0.15, atr_stop))
                    effective_trailing_stop = atr_stop
                else:
                    effective_trailing_stop = TRAILING_STOP_PCT
                if price < hwm * (1 - effective_trailing_stop):
                    sell_trigger = True
                    sell_ratio = 1.0
                    reason = f"Trailing stop-loss triggered for {asset} (price ${price:,.2f} < high water mark ${hwm:,.2f} * {1 - effective_trailing_stop:.2f}, stop={effective_trailing_stop*100:.1f}%)"

            # Priority 4: Trend-exit (MA cross) — fallback (only fires once per entry)
            if not sell_trigger and not tp_flags.get("trend_exit_hit", False):
                ma_s, ma_l = analyze_trend(df)
                if ma_s and ma_l and ma_s < ma_l * 0.998:
                    sell_trigger = True
                    sell_ratio = 0.5
                    reason = f"Trend-exit (50%) triggered for {asset}"
                    tp_flags["trend_exit_hit"] = True
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
    if ENABLE_SHORT:
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
                    # Buy back to close the short — use the original short size
                    # We don't track short quantity separately, so we close the full position
                    short_value = short_entry  # approximate — this is USD-denominated entry
                    result = executor.place_limit_order(product_id, 'BUY', price, amount_quote_currency=short_value)
                    if result:
                        if isinstance(result, dict) and result.get("success") is False:
                            logging.warning(f"[{ex_id}] Short close for {asset} failed: {result.get('error')}")
                        else:
                            # Short PnL is inverted: profit when price drops.
                            # qty = short_value / short_entry (how many units we shorted)
                            # PnL = (entry - exit) * qty - fees
                            # Positive when exit < entry (price dropped as expected).
                            pnl = (short_entry - price) * (short_value / short_entry) - short_entry * (short_value / short_entry) * ROUND_TRIP_FEE_PCT
                            is_win = pnl > 0
                            record_trade(is_win, pnl)
                            logging.info(f"[{ex_id}] 📊 Short PnL: ${pnl:+.2f} (entry=${short_entry:,.2f} -> exit=${price:,.2f})")
                            # Clear the short entry
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
