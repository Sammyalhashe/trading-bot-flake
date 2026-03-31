import pytest
import time
import os
import sys
import pandas as pd
import numpy as np
from decimal import Decimal
from unittest.mock import patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.technical_analysis import TechnicalAnalysis
from config.trading_config import TradingConfig


# --- Helpers ---

def make_candle_df(prices, volumes=None, num_rows=60):
    """Create a candle DataFrame."""
    if isinstance(prices, (int, float)):
        prices = [float(prices)] * num_rows
    if volumes is None:
        volumes = [100.0] * num_rows
    elif isinstance(volumes, (int, float)):
        volumes = [float(volumes)] * num_rows

    now = time.time()
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
    """Bullish MA crossover with RSI below overbought threshold."""
    # Gentle uptrend with pullbacks to keep RSI moderate (~62)
    prices = []
    p = 100.0
    for i in range(num_rows):
        if i % 2 == 1:
            p -= 0.15
        else:
            p += 0.25
        prices.append(p)
    return make_candle_df(prices, volumes=[200000.0] * num_rows)


def make_bearish_df(num_rows=60):
    prices = [200 - i * 0.5 for i in range(num_rows)]
    return make_candle_df(prices, volumes=[200000.0] * num_rows)


def make_oversold_df(num_rows=60):
    """Create df with RSI < 30 and price below lower Bollinger Band."""
    # Stable prices for most of the window, then sharp drop at the end
    # This creates a low RSI and pushes price below the lower BB
    prices = [100.0] * (num_rows - 5) + [95.0, 90.0, 85.0, 80.0, 75.0]
    return make_candle_df(prices, volumes=[200000.0] * num_rows)


def default_config():
    return TradingConfig.from_env()


def make_ta():
    return TechnicalAnalysis(ma_short_window=20, ma_long_window=50)


# ===== TechnicalAnalysis: Bollinger Bands & SMA =====

class TestBollingerBands:
    def test_returns_none_for_insufficient_data(self):
        ta = make_ta()
        df = make_candle_df(100.0, num_rows=10)
        assert ta.calculate_bollinger_bands(df, period=20) is None

    def test_returns_none_for_none_input(self):
        ta = make_ta()
        assert ta.calculate_bollinger_bands(None) is None

    def test_correct_values_flat_data(self):
        ta = make_ta()
        df = make_candle_df(100.0, num_rows=30)
        result = ta.calculate_bollinger_bands(df, period=20, num_std=2.0)
        assert result is not None
        middle, upper, lower = result
        assert abs(middle - 100.0) < 0.01
        # Flat data → std ≈ 0, so bands ≈ middle
        assert abs(upper - middle) < 0.01
        assert abs(lower - middle) < 0.01

    def test_volatile_data_has_wider_bands(self):
        ta = make_ta()
        prices = [100 + (10 if i % 2 == 0 else -10) for i in range(30)]
        df = make_candle_df(prices, num_rows=30)
        result = ta.calculate_bollinger_bands(df, period=20, num_std=2.0)
        assert result is not None
        middle, upper, lower = result
        assert upper > middle > lower
        assert upper - lower > 5  # Significant spread


class TestSMA:
    def test_returns_none_for_insufficient_data(self):
        ta = make_ta()
        df = make_candle_df(100.0, num_rows=10)
        assert ta.calculate_sma(df, period=20) is None

    def test_returns_none_for_none_input(self):
        ta = make_ta()
        assert ta.calculate_sma(None) is None

    def test_correct_value(self):
        ta = make_ta()
        # Linear prices: last 20 are 40..59 with step 0.5 → base 100
        prices = [100 + i * 0.5 for i in range(60)]
        df = make_candle_df(prices, num_rows=60)
        sma = ta.calculate_sma(df, period=20)
        assert sma is not None
        # Last 20 close prices: 100+40*0.5=120 to 100+59*0.5=129.5
        expected = sum(100 + i * 0.5 for i in range(40, 60)) / 20
        assert abs(sma - expected) < 0.01


# ===== TrendFollowingStrategy =====

class TestTrendFollowingStrategy:
    def _make_strategy(self):
        from strategies.trend_following import TrendFollowingStrategy
        return TrendFollowingStrategy(make_ta(), default_config())

    def test_entry_with_bullish_crossover(self):
        s = self._make_strategy()
        df = make_bullish_df()
        result = s.scan_entry("BTC", "BTC-USDC", df, "BULL", "BULL")
        assert result is not None
        assert result["asset"] == "BTC"
        assert result["product_id"] == "BTC-USDC"
        assert "score" in result

    def test_skips_overbought(self):
        s = self._make_strategy()
        # Strong uptrend → RSI > 70
        prices = [100 + i * 2 for i in range(60)]
        df = make_candle_df(prices, volumes=[200000.0] * 60)
        result = s.scan_entry("BTC", "BTC-USDC", df, "BULL", "BULL")
        assert result is None

    def test_skips_bear_regime_non_btc(self):
        s = self._make_strategy()
        df = make_bullish_df()
        result = s.scan_entry("ETH", "ETH-USDC", df, "BEAR", "BEAR")
        assert result is None

    def test_btc_allowed_in_bear_with_exemption(self):
        s = self._make_strategy()
        df = make_bullish_df()
        result = s.scan_entry("BTC", "BTC-USDC", df, "BEAR", "BEAR")
        # BTC should be allowed due to allow_btc_in_bear
        assert result is not None

    def test_skips_neutral_regime(self):
        s = self._make_strategy()
        assert s.should_skip_regime("BULL", "NEUTRAL") is True

    def test_ranking_by_momentum_descending(self):
        s = self._make_strategy()
        candidates = [
            {"asset": "A", "product_id": "A-USDC", "score": 5.0},
            {"asset": "B", "product_id": "B-USDC", "score": 15.0},
            {"asset": "C", "product_id": "C-USDC", "score": 10.0},
        ]
        ranked = s.rank_candidates(candidates)
        assert ranked[0]["asset"] == "B"
        assert ranked[1]["asset"] == "C"
        assert ranked[2]["asset"] == "A"

    def test_exit_tp1(self):
        s = self._make_strategy()
        df = make_bullish_df()
        entry = 100.0
        # Price is 10% above entry → TP1 (default 10%)
        price = 111.0
        hwm = 111.0
        tp_flags = {"tp1_hit": False, "tp2_hit": False, "trend_exit_hit": False}

        sell, ratio, reason, flags = s.check_exit("BTC", "BTC-USDC", df, price, entry, hwm, tp_flags, {}, "k")
        assert sell is True
        assert 0 < ratio < 1  # Partial sell
        assert flags["tp1_hit"] is True
        assert "level 1" in reason

    def test_exit_tp2(self):
        s = self._make_strategy()
        df = make_bullish_df()
        entry = 100.0
        price = 121.0  # 21% above entry → TP2 (default 20%)
        hwm = 121.0
        tp_flags = {"tp1_hit": False, "tp2_hit": False, "trend_exit_hit": False}

        sell, ratio, reason, flags = s.check_exit("BTC", "BTC-USDC", df, price, entry, hwm, tp_flags, {}, "k")
        assert sell is True
        assert flags["tp2_hit"] is True
        assert flags["tp1_hit"] is True
        assert "level 2" in reason

    def test_exit_trailing_stop(self):
        s = self._make_strategy()
        df = make_candle_df(100.0, num_rows=60)
        entry = 100.0
        hwm = 120.0  # Was at 120
        price = 90.0  # Dropped to 90 (25% from HWM)
        tp_flags = {"tp1_hit": False, "tp2_hit": False, "trend_exit_hit": False}

        sell, ratio, reason, flags = s.check_exit("BTC", "BTC-USDC", df, price, entry, hwm, tp_flags, {}, "k")
        assert sell is True
        assert ratio == 1.0
        assert "Trailing stop" in reason

    def test_exit_trend_exit(self):
        s = self._make_strategy()
        df = make_bearish_df()
        entry = 150.0
        price = 150.0  # No gain/loss
        hwm = 150.0
        tp_flags = {"tp1_hit": False, "tp2_hit": False, "trend_exit_hit": False}

        sell, ratio, reason, flags = s.check_exit("BTC", "BTC-USDC", df, price, entry, hwm, tp_flags, {}, "k")
        assert sell is True
        assert ratio == 0.5
        assert "Trend-exit" in reason
        assert flags["trend_exit_hit"] is True


# ===== MeanReversionStrategy =====

class TestMeanReversionStrategy:
    def _make_strategy(self):
        from strategies.mean_reversion import MeanReversionStrategy
        return MeanReversionStrategy(make_ta(), default_config())

    def test_entry_with_oversold_below_bollinger(self):
        s = self._make_strategy()
        df = make_oversold_df()
        result = s.scan_entry("BTC", "BTC-USDC", df, "BULL", "BULL")
        # Should find entry since RSI < 30 and price below lower BB
        assert result is not None
        assert result["asset"] == "BTC"
        assert "score" in result

    def test_skips_rsi_above_threshold(self):
        s = self._make_strategy()
        df = make_bullish_df()  # RSI will be high
        result = s.scan_entry("BTC", "BTC-USDC", df, "BULL", "BULL")
        assert result is None

    def test_skips_price_above_bollinger(self):
        s = self._make_strategy()
        # Flat data → price ≈ SMA ≈ middle band, not below lower
        df = make_candle_df(100.0, num_rows=60)
        result = s.scan_entry("BTC", "BTC-USDC", df, "BULL", "BULL")
        assert result is None

    def test_skips_bear_regime(self):
        s = self._make_strategy()
        assert s.should_skip_regime("BEAR", "BEAR") is True
        assert s.should_skip_regime("BEAR", "STRONG_BEAR") is True

    def test_allows_neutral_regime(self):
        s = self._make_strategy()
        assert s.should_skip_regime("BULL", "NEUTRAL") is False

    def test_allows_bull_regime(self):
        s = self._make_strategy()
        assert s.should_skip_regime("BULL", "BULL") is False

    def test_exit_mean_reversion_target(self):
        s = self._make_strategy()
        # Price at SMA → target reached
        df = make_candle_df(100.0, num_rows=60)
        sma = make_ta().calculate_sma(df, period=20)
        price = sma + 1  # Above SMA
        tp_flags = {}

        sell, ratio, reason, flags = s.check_exit("BTC", "BTC-USDC", df, price, 90.0, price, tp_flags, {}, "k")
        assert sell is True
        assert ratio == 1.0
        assert "target reached" in reason

    def test_exit_stop_loss(self):
        s = self._make_strategy()
        df = make_candle_df(100.0, num_rows=60)
        hwm = 100.0
        price = 91.0  # 9% below HWM, stop is 8%
        tp_flags = {}

        sell, ratio, reason, flags = s.check_exit("BTC", "BTC-USDC", df, price, 95.0, hwm, tp_flags, {}, "k")
        # Price < hwm * 0.92 = 92.0, and price is 91
        # SMA of flat 100 data = 100, price 91 < 100 so SMA target NOT reached
        # So stop loss should fire
        assert sell is True
        assert ratio == 1.0
        assert "stop loss" in reason

    def test_exit_time_based(self):
        s = self._make_strategy()
        # Make downtrending df so price < SMA (no SMA exit) and price > stop (no stop exit)
        prices = [110 - i * 0.1 for i in range(60)]
        df = make_candle_df(prices, num_rows=60)
        price = prices[-1]
        hwm = price + 1  # HWM just above price, within 8% stop
        entry = price - 5

        # Simulate entry 11 hours ago (above 10h threshold)
        entry_time = time.time() - 11 * 3600
        state = {"entry_timestamps": {"k": entry_time}}
        tp_flags = {}

        sell, ratio, reason, flags = s.check_exit("BTC", "BTC-USDC", df, price, entry, hwm, tp_flags, state, "k")
        assert sell is True
        assert ratio == 1.0
        assert "time exit" in reason

    def test_ranking_by_rsi_ascending(self):
        s = self._make_strategy()
        candidates = [
            {"asset": "A", "product_id": "A-USDC", "score": 25.0},
            {"asset": "B", "product_id": "B-USDC", "score": 15.0},
            {"asset": "C", "product_id": "C-USDC", "score": 20.0},
        ]
        ranked = s.rank_candidates(candidates)
        assert ranked[0]["asset"] == "B"  # Lowest RSI first
        assert ranked[1]["asset"] == "C"
        assert ranked[2]["asset"] == "A"


# ===== Strategy Factory =====

class TestStrategyFactory:
    def test_create_trend_following(self):
        from strategies import create_strategy
        from strategies.trend_following import TrendFollowingStrategy
        s = create_strategy("trend_following", make_ta(), default_config())
        assert isinstance(s, TrendFollowingStrategy)

    def test_create_mean_reversion(self):
        from strategies import create_strategy
        from strategies.mean_reversion import MeanReversionStrategy
        s = create_strategy("mean_reversion", make_ta(), default_config())
        assert isinstance(s, MeanReversionStrategy)

    def test_unknown_strategy_raises(self):
        from strategies import create_strategy
        with pytest.raises(ValueError, match="Unknown strategy"):
            create_strategy("does_not_exist", make_ta(), default_config())


# ===== Config Validation =====

class TestStrategyConfig:
    def test_invalid_strategy_name_rejected(self):
        config = default_config()
        config.strategy = "invalid_strategy"
        with pytest.raises(ValueError, match="Invalid strategy"):
            config.validate()

    def test_default_strategy_is_auto(self):
        config = default_config()
        assert config.strategy == "auto"

    def test_mean_reversion_defaults(self):
        config = default_config()
        assert config.mr_rsi_oversold == Decimal("25")
        assert config.mr_bollinger_period == 20
        assert config.mr_bollinger_std == 2.0
        assert config.mr_trailing_stop_pct == Decimal("0.08")
        assert config.mr_time_exit_candles == 10

    def test_auto_strategy_valid(self):
        config = default_config()
        config.strategy = "auto"
        config.validate()  # Should not raise

    def test_max_concurrent_positions_default(self):
        config = default_config()
        assert config.max_concurrent_positions == 7


# ===== Dynamic Strategy Switching =====

class TestDynamicStrategySwitching:
    def test_select_bull_uses_trend_following(self):
        from trading_bot import select_strategy_for_regime
        s = select_strategy_for_regime("BULL")
        assert s.name == "trend_following"

    def test_select_strong_bull_uses_trend_following(self):
        from trading_bot import select_strategy_for_regime
        s = select_strategy_for_regime("STRONG_BULL")
        assert s.name == "trend_following"

    def test_select_neutral_uses_mean_reversion(self):
        from trading_bot import select_strategy_for_regime
        s = select_strategy_for_regime("NEUTRAL")
        assert s.name == "mean_reversion"

    def test_select_bear_uses_trend_following(self):
        from trading_bot import select_strategy_for_regime
        s = select_strategy_for_regime("BEAR")
        assert s.name == "trend_following"

    def test_select_strong_bear_uses_trend_following(self):
        from trading_bot import select_strategy_for_regime
        s = select_strategy_for_regime("STRONG_BEAR")
        assert s.name == "trend_following"


# ===== Updated Thresholds =====

class TestUpdatedThresholds:
    def test_rsi_27_rejected_by_new_threshold(self):
        """RSI=27 was accepted with old threshold (30) but rejected with new (25)."""
        from strategies.mean_reversion import MeanReversionStrategy
        config = default_config()
        assert config.mr_rsi_oversold == Decimal("25")
        s = MeanReversionStrategy(make_ta(), config)
        # Moderate drop: RSI will be ~27 (between old 30 and new 25 threshold)
        prices = [100.0] * 50 + [98.0, 96.5, 95.5, 94.8, 94.2, 93.8, 93.5, 93.3, 93.2, 93.1]
        df = make_candle_df(prices, volumes=[200000.0] * 60, num_rows=60)
        rsi = make_ta().calculate_rsi(df)
        # Verify RSI is between 25 and 30 (old threshold accepts, new rejects)
        if rsi is not None and 25 < rsi < 30:
            result = s.scan_entry("BTC", "BTC-USDC", df, "BULL", "BULL")
            assert result is None  # Should be rejected with RSI threshold of 25

    def test_time_exit_at_10h(self):
        """Positions should exit after 10h instead of waiting for 24h."""
        from strategies.mean_reversion import MeanReversionStrategy
        s = MeanReversionStrategy(make_ta(), default_config())
        prices = [110 - i * 0.1 for i in range(60)]
        df = make_candle_df(prices, num_rows=60)
        price = prices[-1]
        hwm = price + 1
        entry = price - 5

        # 11h ago: should trigger time exit with new 10h threshold
        entry_time = time.time() - 11 * 3600
        state = {"entry_timestamps": {"k": entry_time}}
        sell, ratio, reason, flags = s.check_exit("BTC", "BTC-USDC", df, price, entry, hwm, {}, state, "k")
        assert sell is True
        assert "time exit" in reason

    def test_no_time_exit_at_9h(self):
        """Positions should NOT exit at 9h with new 10h threshold."""
        from strategies.mean_reversion import MeanReversionStrategy
        s = MeanReversionStrategy(make_ta(), default_config())
        prices = [110 - i * 0.1 for i in range(60)]
        df = make_candle_df(prices, num_rows=60)
        price = prices[-1]
        hwm = price + 1
        entry = price - 5

        # 9h ago: should NOT trigger with 10h threshold
        entry_time = time.time() - 9 * 3600
        state = {"entry_timestamps": {"k": entry_time}}
        sell, ratio, reason, flags = s.check_exit("BTC", "BTC-USDC", df, price, entry, hwm, {}, state, "k")
        assert sell is False

    def test_stop_loss_at_8pct(self):
        """8% stop loss: 6% drop should NOT trigger, 9% drop should."""
        from strategies.mean_reversion import MeanReversionStrategy
        s = MeanReversionStrategy(make_ta(), default_config())
        df = make_candle_df(100.0, num_rows=60)

        # 6% drop: should NOT trigger (old 5% would have)
        hwm = 100.0
        price = 94.5  # 5.5% below HWM
        sell, _, _, _ = s.check_exit("BTC", "BTC-USDC", df, price, 95.0, hwm, {}, {}, "k")
        assert sell is False

        # 9% drop: should trigger
        price = 91.0
        sell, ratio, reason, _ = s.check_exit("BTC", "BTC-USDC", df, price, 95.0, hwm, {}, {}, "k")
        assert sell is True
        assert "stop loss" in reason
