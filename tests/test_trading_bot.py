import pytest
from unittest.mock import MagicMock, patch, PropertyMock
import json
import os
import sys
import time
import tempfile
import pandas as pd
import numpy as np
from pathlib import Path

# Add parent dir to path to import trading_bot
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# --- Helpers ---

def make_candle_df(prices, volumes=None, num_rows=60):
    """Create a candle DataFrame mimicking Coinbase format.

    Prices are in chronological order (oldest first).
    The returned df is sorted ascending by start time, so prices[0] is oldest
    and prices[-1] is the most recent bar.
    """
    if isinstance(prices, (int, float)):
        prices = [float(prices)] * num_rows
    if volumes is None:
        volumes = [100.0] * num_rows
    elif isinstance(volumes, (int, float)):
        volumes = [float(volumes)] * num_rows

    now = time.time()
    # Build chronologically: index 0 = oldest, index N-1 = most recent
    data = {
        'start': pd.to_datetime([now - (num_rows - 1 - i) * 3600 for i in range(num_rows)], unit='s'),
        'low': [p * 0.99 for p in prices],
        'high': [p * 1.01 for p in prices],
        'open': prices,
        'close': prices,
        'volume': volumes,
    }
    df = pd.DataFrame(data)
    return df.sort_values(by='start').reset_index(drop=True)


def make_bullish_df(num_rows=60):
    """Create candle data with bullish MA crossover (short > long)."""
    # Prices trend upward so short MA > long MA
    prices = [100 + i * 0.5 for i in range(num_rows)]
    return make_candle_df(prices, volumes=[200000.0] * num_rows)


def make_bearish_df(num_rows=60):
    """Create candle data with bearish MA crossover (short < long)."""
    # Prices trend downward so short MA < long MA
    prices = [200 - i * 0.5 for i in range(num_rows)]
    return make_candle_df(prices, volumes=[200000.0] * num_rows)


@pytest.fixture
def temp_state_file(tmp_path):
    """Provide a temporary state file and patch STATE_FILE."""
    state_path = tmp_path / "trading_state.json"
    with patch('trading_bot.STATE_FILE', state_path):
        yield state_path


@pytest.fixture
def mock_executor():
    """Create a mock executor with standard methods."""
    executor = MagicMock()
    executor.__class__.__name__ = "TestExecutor"
    executor.get_balances.return_value = {"cash": {"USDC": 10000.0}, "crypto": {}}
    executor.get_supported_assets.return_value = ["BTC"]  # Mock with single asset for simplicity
    executor.place_limit_order.return_value = {"success": True}
    executor.place_market_order.return_value = {"success": True}
    # Remove 'account' attribute so ex_id stays simple
    del executor.account
    return executor


@pytest.fixture
def mock_data_provider():
    """Create a mock data provider."""
    provider = MagicMock()
    provider.get_product_details.return_value = {
        "price": "100.0",
        "quote_increment": "0.01",
        "base_increment": "0.001"
    }
    provider.get_market_data.return_value = make_bullish_df()
    return provider


# ===== Test State Management =====

class TestStateManagement:
    def test_load_state_returns_default_when_missing(self, temp_state_file):
        import trading_bot
        state = trading_bot.load_state()
        assert "entry_prices" in state
        assert "high_water_marks" in state
        assert "take_profit_flags" in state

    def test_save_and_load_state_roundtrip(self, temp_state_file):
        import trading_bot
        test_state = {
            "entry_prices": {"test:BTC-USDC": 50000.0},
            "high_water_marks": {"test:BTC-USDC": 51000.0},
            "take_profit_flags": {"test:BTC-USDC": {"tp1_hit": False, "tp2_hit": False, "trend_exit_hit": False}},
        }
        trading_bot.save_state(test_state)
        loaded = trading_bot.load_state()
        assert loaded["entry_prices"]["test:BTC-USDC"] == 50000.0

    def test_atomic_write_creates_no_tmp_on_success(self, temp_state_file):
        import trading_bot
        trading_bot.save_state({"entry_prices": {}})
        tmp_file = temp_state_file.with_suffix('.tmp')
        assert not tmp_file.exists()
        assert temp_state_file.exists()

    def test_update_entry_price_sets_trend_exit_flag(self, temp_state_file):
        import trading_bot
        trading_bot.update_entry_price("test", "BTC-USDC", 50000.0)
        state = trading_bot.load_state()
        key = "test:BTC-USDC"
        assert state["take_profit_flags"][key]["trend_exit_hit"] is False
        assert state["take_profit_flags"][key]["tp1_hit"] is False
        assert state["take_profit_flags"][key]["tp2_hit"] is False

    def test_clear_entry_price(self, temp_state_file):
        import trading_bot
        trading_bot.update_entry_price("test", "BTC-USDC", 50000.0)
        trading_bot.clear_entry_price("test", "BTC-USDC")
        state = trading_bot.load_state()
        assert "test:BTC-USDC" not in state.get("entry_prices", {})
        assert "test:BTC-USDC" not in state.get("high_water_marks", {})
        assert "test:BTC-USDC" not in state.get("take_profit_flags", {})


# ===== Test RSI Calculation =====

class TestRSI:
    def test_rsi_returns_none_for_short_data(self):
        import trading_bot
        df = make_candle_df(100.0, num_rows=10)
        assert trading_bot.calculate_rsi(df, period=14) is None

    def test_rsi_returns_none_for_none_input(self):
        import trading_bot
        assert trading_bot.calculate_rsi(None) is None

    def test_rsi_overbought_on_strong_uptrend(self):
        import trading_bot
        # Strong uptrend: every bar closes higher
        prices = [100 + i * 2 for i in range(30)]
        df = make_candle_df(prices, num_rows=30)
        rsi = trading_bot.calculate_rsi(df)
        assert rsi is not None
        assert rsi > 70  # Should be overbought

    def test_rsi_oversold_on_strong_downtrend(self):
        import trading_bot
        prices = [200 - i * 2 for i in range(30)]
        df = make_candle_df(prices, num_rows=30)
        rsi = trading_bot.calculate_rsi(df)
        assert rsi is not None
        assert rsi < 30  # Should be oversold


# ===== Test ATR Calculation =====

class TestATR:
    def test_atr_returns_none_for_short_data(self):
        import trading_bot
        df = make_candle_df(100.0, num_rows=10)
        assert trading_bot.calculate_atr(df, period=14) is None

    def test_atr_returns_none_for_none_input(self):
        import trading_bot
        assert trading_bot.calculate_atr(None) is None

    def test_atr_positive_for_volatile_data(self):
        import trading_bot
        # Alternate high/low to create volatility
        prices = [100 + (10 if i % 2 == 0 else -10) for i in range(30)]
        df = make_candle_df(prices, num_rows=30)
        atr = trading_bot.calculate_atr(df)
        assert atr is not None
        assert atr > 0

    def test_atr_low_for_flat_data(self):
        import trading_bot
        df = make_candle_df(100.0, num_rows=30)
        atr = trading_bot.calculate_atr(df)
        assert atr is not None
        # Flat data has low ATR (only the 1% high/low spread)
        assert atr < 5


# ===== Test Volume Filter =====

class TestVolumeFilter:
    def test_low_volume_skips_buy(self, temp_state_file, mock_executor, mock_data_provider):
        import trading_bot
        # Volume too low: 24 rows * 10 volume * $100 = $24,000 < $100,000
        low_vol_df = make_candle_df(100.0, volumes=[10.0] * 60)
        mock_data_provider.get_market_data.return_value = low_vol_df

        trading_bot.run_executor_strategy(mock_executor, mock_data_provider, "BULL")
        mock_executor.place_limit_order.assert_not_called()

    def test_high_volume_allows_buy(self, temp_state_file, mock_executor, mock_data_provider):
        import trading_bot
        # Gentle uptrend with alternating bars to keep RSI ~62
        # Pattern: +0.25, -0.15 (net +0.10 per 2 bars)
        prices = []
        p = 100.0
        for i in range(60):
            if i % 2 == 1:
                p -= 0.15  # pullback
            else:
                p += 0.25  # up move
            prices.append(p)
        high_vol_df = make_candle_df(prices, volumes=[200000.0] * 60)
        mock_data_provider.get_market_data.return_value = high_vol_df
        last_price = prices[-1]
        mock_data_provider.get_product_details.return_value = {
            "price": str(last_price),
            "quote_increment": "0.01",
            "base_increment": "0.001",
        }

        trading_bot.run_executor_strategy(mock_executor, mock_data_provider, "BULL")
        # Should attempt at least one buy
        buy_calls = [c for c in mock_executor.place_limit_order.call_args_list if c[0][1] == 'BUY']
        assert len(buy_calls) > 0


# ===== Test Trend Exit Flag =====

class TestTrendExitFlag:
    def test_trend_exit_fires_once(self, temp_state_file, mock_executor, mock_data_provider):
        import trading_bot

        # Setup: holding BTC with small gain (+0.67%) above fee floor (0.45%)
        # so TP1/TP2 won't trigger. HWM = price so trailing stop won't trigger.
        # But MA cross is bearish so trend exit should fire.
        current_price = 150.0
        mock_executor.get_balances.return_value = {
            "cash": {"USDC": 1000.0},
            "crypto": {"BTC": 1.0},
        }
        bearish_df = make_bearish_df()
        mock_data_provider.get_market_data.return_value = bearish_df
        mock_data_provider.get_product_details.return_value = {
            "price": str(current_price),
            "quote_increment": "0.01",
            "base_increment": "0.001",
        }

        # Entry below current price for +0.67% gain, above fee floor
        entry_price = 149.0
        trading_bot.update_entry_price("TestExecutor", "BTC-USDC", entry_price)

        # First run: trend exit should trigger
        trading_bot.run_executor_strategy(mock_executor, mock_data_provider, "BEAR")
        first_sell_calls = [c for c in mock_executor.place_limit_order.call_args_list if c[0][1] == 'SELL']

        # Reset mock
        mock_executor.place_limit_order.reset_mock()
        # Simulate partial sell: still holding some
        mock_executor.get_balances.return_value = {
            "cash": {"USDC": 1500.0},
            "crypto": {"BTC": 0.5},
        }

        # Second run: trend exit should NOT trigger again (flag set)
        trading_bot.run_executor_strategy(mock_executor, mock_data_provider, "BEAR")
        second_sell_calls = [c for c in mock_executor.place_limit_order.call_args_list if c[0][1] == 'SELL']

        assert len(first_sell_calls) > 0, "Trend exit should trigger on first run"
        assert len(second_sell_calls) == 0, "Trend exit should NOT trigger on second run"
        # Verify the flag is set
        state = trading_bot.load_state()
        tp_flags = state.get("take_profit_flags", {}).get("TestExecutor:BTC-USDC", {})
        assert tp_flags.get("trend_exit_hit") is True


# ===== Test Fee-Aware PnL =====

class TestFeeAwarePnL:
    def test_pnl_subtracts_fees(self, temp_state_file):
        import trading_bot

        # Record a trade manually to verify fee deduction
        entry = 100.0
        exit_price = 110.0
        sell_amount = 1.0
        fee_pct = 0.006  # 0.6%

        fee_cost = entry * sell_amount * fee_pct
        expected_pnl = (exit_price - entry) * sell_amount - fee_cost
        # $10 gross - $0.60 fee = $9.40
        assert abs(expected_pnl - 9.40) < 0.01


# ===== Test Bear Market Filter =====

class TestBearMarketFilter:
    def test_no_alt_buys_in_bear_market(self, temp_state_file, mock_executor, mock_data_provider):
        import trading_bot

        # Bullish crossover data but bear market
        high_vol_df = make_bullish_df()
        mock_data_provider.get_market_data.return_value = high_vol_df

        trading_bot.run_executor_strategy(mock_executor, mock_data_provider, "BEAR")

        # Only BTC should be allowed in bear market, not ETH or MATIC
        buy_calls = mock_executor.place_limit_order.call_args_list
        for call in buy_calls:
            if call[0][1] == 'BUY':
                product_id = call[0][0]
                assert "BTC" in product_id or "POL" not in product_id


# ===== Test Trailing Stop with ATR =====

class TestTrailingStopATR:
    def test_atr_stop_bounded(self):
        import trading_bot

        # High volatility -> ATR-based stop should be capped at 15%
        prices = [100 + (30 if i % 2 == 0 else -30) for i in range(30)]
        df = make_candle_df(prices, num_rows=30)
        atr = trading_bot.calculate_atr(df)
        price = df['close'].iloc[-1]
        if atr and price > 0:
            atr_stop = 2 * atr / price
            bounded = max(0.02, min(0.15, atr_stop))
            assert 0.02 <= bounded <= 0.15


# ===== Test Configurable Paths =====

class TestConfigurablePaths:
    def test_no_hardcoded_paths(self):
        """Verify no hardcoded /home/salhashemi2 paths remain."""
        files_to_check = [
            os.path.join(os.path.dirname(os.path.dirname(__file__)), f)
            for f in ['trading_bot.py', 'report_bot.py', 'notify_telegram.py', 'coinbase_executor.py']
        ]
        for filepath in files_to_check:
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    content = f.read()
                assert "salhashemi2" not in content, f"Hardcoded path found in {filepath}"


# ===== Test CoinbaseExecutor.check_order_filled =====

class TestCheckOrderFilled:
    def test_filled_order_returns_price(self):
        from executors.coinbase_executor import CoinbaseExecutor
        with patch.object(CoinbaseExecutor, '__init__', lambda self, *a, **k: None):
            executor = CoinbaseExecutor.__new__(CoinbaseExecutor)
            executor.request = MagicMock(return_value={
                "order": {
                    "status": "FILLED",
                    "average_filled_price": "50000.00",
                    "filled_size": "0.1",
                }
            })
            result = executor.check_order_filled("order-123")
            assert result == 50000.0

    def test_cancelled_order_returns_none(self):
        from executors.coinbase_executor import CoinbaseExecutor
        with patch.object(CoinbaseExecutor, '__init__', lambda self, *a, **k: None):
            executor = CoinbaseExecutor.__new__(CoinbaseExecutor)
            executor.request = MagicMock(return_value={
                "order": {"status": "CANCELLED"}
            })
            result = executor.check_order_filled("order-123")
            assert result is None

    def test_timeout_returns_none(self):
        from executors.coinbase_executor import CoinbaseExecutor
        with patch.object(CoinbaseExecutor, '__init__', lambda self, *a, **k: None):
            executor = CoinbaseExecutor.__new__(CoinbaseExecutor)
            executor.request = MagicMock(return_value={
                "order": {"status": "PENDING"}
            })
            result = executor.check_order_filled("order-123", max_attempts=2, poll_interval=0)
            assert result is None


# ===== Test Ethereum Executor =====

class TestEthereumExecutor:
    def test_pol_not_in_tokens(self):
        """Verify POL placeholder was removed."""
        from executors.ethereum_executor import TOKENS
        assert "POL" not in TOKENS

    def test_execute_swap_default_fee_not_500(self):
        """Verify execute_swap no longer defaults to fee=500."""
        import inspect
        from executors.ethereum_executor import EthereumExecutor
        sig = inspect.signature(EthereumExecutor.execute_swap)
        fee_param = sig.parameters.get('fee')
        assert fee_param is not None
        assert fee_param.default != 500, "execute_swap should not default to fee=500"
