import os
import sys
import time
from unittest.mock import patch, MagicMock
from decimal import Decimal

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.trading_config import TradingConfig
from core.derivatives_data import (
    DerivativesDataProvider,
    FundingSignal,
    OISignal,
    LSRatioSignal,
)


def make_config(**overrides):
    defaults = dict(
        ma_short_window=21, ma_long_window=55,
        portfolio_risk_pct=Decimal("0.90"), risk_per_trade_pct=Decimal("0.95"),
        max_drawdown_pct=Decimal("15"), drawdown_cooldown_hours=48,
        min_order_usd=Decimal("10"), trend_asset="BTC",
        enable_dual_regime=True, enable_btc_dominance=False,
        allow_btc_in_bear=True, bear_position_scale=0.25,
        enable_derivatives_signals=True,
        derivatives_funding_high=0.0005,
        derivatives_funding_extreme=0.0010,
        derivatives_oi_divergence_pct=-5.0,
        rsi_overbought=Decimal("75"), rsi_overbought_bull=Decimal("82"),
        rsi_overbought_strong_bull=Decimal("88"), rsi_overbought_neutral=Decimal("75"),
        rsi_overbought_bear=Decimal("70"), trailing_stop_pct=Decimal("0.07"),
        min_24h_volume_usd=Decimal("500000"), volume_spike_rsi_bonus=5.0,
        volume_spike_threshold=2.0, round_trip_fee_pct=Decimal("0.0015"),
        take_profit_1_pct=Decimal("0.15"), take_profit_1_sell_ratio=Decimal("0.25"),
        take_profit_2_pct=Decimal("0.40"), take_profit_2_sell_ratio=Decimal("0.35"),
        strategy="trend_following",
        mr_rsi_oversold=Decimal("30"), mr_bollinger_period=20, mr_bollinger_std=2.0,
        mr_trailing_stop_pct=Decimal("0.08"), mr_time_exit_candles=10,
        max_concurrent_positions=3, ws_scan_interval=300,
        asset_blacklist=[], momentum_window_hours=24, top_momentum_count=3,
        asset_mapping={},
    )
    defaults.update(overrides)
    return TradingConfig(**defaults)


# --- Position Modifier Tests ---

class TestPositionModifier:
    def setup_method(self):
        self.provider = DerivativesDataProvider(make_config())

    def test_normal_funding_returns_1(self):
        funding = FundingSignal(avg_rate=0.0001, latest_rate=0.0001, signal="NORMAL")
        mod = self.provider.compute_position_modifier(funding, None)
        assert mod == 1.0

    def test_elevated_funding_scales_down(self):
        funding = FundingSignal(avg_rate=0.0006, latest_rate=0.0006, signal="ELEVATED")
        mod = self.provider.compute_position_modifier(funding, None)
        assert mod == 0.75

    def test_extreme_funding_scales_down_more(self):
        funding = FundingSignal(avg_rate=0.0015, latest_rate=0.0015, signal="EXTREME")
        mod = self.provider.compute_position_modifier(funding, None)
        assert mod == 0.50

    def test_negative_funding_scales_up(self):
        funding = FundingSignal(avg_rate=-0.0002, latest_rate=-0.0002, signal="NEGATIVE")
        mod = self.provider.compute_position_modifier(funding, None)
        assert mod == 1.10

    def test_extreme_negative_funding_scales_up_max(self):
        funding = FundingSignal(avg_rate=-0.001, latest_rate=-0.001, signal="EXTREME_NEGATIVE")
        mod = self.provider.compute_position_modifier(funding, None)
        assert mod == 1.25

    def test_extreme_long_ls_ratio_scales_down(self):
        ls = LSRatioSignal(long_ratio=0.75, signal="EXTREME_LONG")
        mod = self.provider.compute_position_modifier(None, ls)
        assert mod == 0.75

    def test_combined_extreme_funding_and_extreme_long(self):
        funding = FundingSignal(avg_rate=0.0015, latest_rate=0.0015, signal="EXTREME")
        ls = LSRatioSignal(long_ratio=0.75, signal="EXTREME_LONG")
        mod = self.provider.compute_position_modifier(funding, ls)
        # 0.50 * 0.75 = 0.375, clamped to 0.25
        assert mod == 0.375

    def test_clamped_to_min(self):
        # Force an extremely low modifier
        funding = FundingSignal(avg_rate=0.002, latest_rate=0.002, signal="EXTREME")
        ls = LSRatioSignal(long_ratio=0.80, signal="EXTREME_LONG")
        mod = self.provider.compute_position_modifier(funding, ls)
        assert mod >= 0.25

    def test_clamped_to_max(self):
        funding = FundingSignal(avg_rate=-0.002, latest_rate=-0.002, signal="EXTREME_NEGATIVE")
        mod = self.provider.compute_position_modifier(funding, None)
        assert mod <= 1.25

    def test_none_inputs_return_1(self):
        mod = self.provider.compute_position_modifier(None, None)
        assert mod == 1.0


# --- OI Divergence Tests ---

class TestOIDivergence:
    def setup_method(self):
        self.provider = DerivativesDataProvider(make_config())

    def test_price_up_oi_falling_is_divergence(self):
        oi = OISignal(change_pct=-8.0, latest_oi=1e9, signal="FALLING")
        assert self.provider.detect_oi_divergence(oi, 3.0) is True

    def test_price_up_oi_stable_no_divergence(self):
        oi = OISignal(change_pct=-2.0, latest_oi=1e9, signal="STABLE")
        assert self.provider.detect_oi_divergence(oi, 3.0) is False

    def test_price_down_oi_falling_no_divergence(self):
        oi = OISignal(change_pct=-8.0, latest_oi=1e9, signal="FALLING")
        assert self.provider.detect_oi_divergence(oi, -1.0) is False

    def test_price_flat_no_divergence(self):
        oi = OISignal(change_pct=-8.0, latest_oi=1e9, signal="FALLING")
        assert self.provider.detect_oi_divergence(oi, 0.5) is False

    def test_none_oi_no_divergence(self):
        assert self.provider.detect_oi_divergence(None, 5.0) is False


# --- Funding Signal Classification ---

class TestFundingClassification:
    def setup_method(self):
        self.provider = DerivativesDataProvider(make_config())

    def test_normal(self):
        assert self.provider._classify_funding(0.0001) == "NORMAL"

    def test_elevated(self):
        assert self.provider._classify_funding(0.0004) == "ELEVATED"

    def test_extreme(self):
        assert self.provider._classify_funding(0.0008) == "EXTREME"

    def test_negative(self):
        assert self.provider._classify_funding(-0.0002) == "NEGATIVE"

    def test_extreme_negative(self):
        assert self.provider._classify_funding(-0.0008) == "EXTREME_NEGATIVE"


# --- Cache Tests ---

class TestCache:
    def setup_method(self):
        self.provider = DerivativesDataProvider(make_config())

    def test_cache_hit(self):
        self.provider._set_cache("test", {"value": 42})
        result = self.provider._get_cached("test", ttl=3600)
        assert result == {"value": 42}

    def test_cache_miss_expired(self):
        self.provider._cache["test"] = (time.time() - 7200, {"value": 42})
        result = self.provider._get_cached("test", ttl=3600)
        assert result is None

    def test_cache_miss_no_entry(self):
        result = self.provider._get_cached("nonexistent", ttl=3600)
        assert result is None


# --- API Failure Tests ---

class TestAPIFailure:
    def setup_method(self):
        self.provider = DerivativesDataProvider(make_config())

    @patch("core.derivatives_data.requests.get")
    def test_funding_rate_api_failure_returns_none(self, mock_get):
        mock_get.side_effect = Exception("Connection failed")
        result = self.provider.get_funding_rate()
        assert result is None

    @patch("core.derivatives_data.requests.get")
    def test_oi_api_failure_returns_none(self, mock_get):
        mock_get.side_effect = Exception("Timeout")
        result = self.provider.get_open_interest()
        assert result is None

    @patch("core.derivatives_data.requests.get")
    def test_ls_ratio_api_failure_returns_none(self, mock_get):
        mock_get.side_effect = Exception("DNS failure")
        result = self.provider.get_long_short_ratio()
        assert result is None

    @patch("core.derivatives_data.requests.get")
    def test_full_signals_on_api_failure(self, mock_get):
        """Even with all APIs failing, get_derivatives_signals returns a valid result."""
        mock_get.side_effect = Exception("Network down")
        result = self.provider.get_derivatives_signals(price_change_pct=2.0)
        assert result.position_modifier == 1.0
        assert result.entry_allowed is True
        assert result.caution_flags == []

    @patch("core.derivatives_data.requests.get")
    def test_funding_uses_cache_on_second_call(self, mock_get):
        """Second call within TTL should not hit the API."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": "0",
            "data": [
                {"fundingRate": "0.0001", "fundingTime": "1"},
                {"fundingRate": "0.0002", "fundingTime": "2"},
                {"fundingRate": "0.0001", "fundingTime": "3"},
            ],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result1 = self.provider.get_funding_rate()
        result2 = self.provider.get_funding_rate()

        assert result1 is not None
        assert result2 is not None
        assert mock_get.call_count == 1  # Only 1 API call, second was cached


# --- Integration-style test for get_derivatives_signals ---

class TestGetDerivativesSignals:
    def setup_method(self):
        self.provider = DerivativesDataProvider(make_config())

    @patch("core.derivatives_data.requests.get")
    def test_full_signals_with_mock_data(self, mock_get):
        """Test the full pipeline with mocked OKX responses."""
        def side_effect(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if "funding-rate-history" in url:
                resp.json.return_value = {
                    "code": "0",
                    "data": [
                        {"fundingRate": "0.0008", "fundingTime": "1"},
                        {"fundingRate": "0.0007", "fundingTime": "2"},
                        {"fundingRate": "0.0009", "fundingTime": "3"},
                    ],
                }
            elif "open-interest-volume" in url:
                # 24 entries, OI falling from 4B to 3.5B
                entries = []
                for i in range(24):
                    oi = 4e9 - (i * 20e6)  # latest = 4B, oldest ~3.54B
                    entries.append([str(i), str(oi), "100000"])
                resp.json.return_value = {"code": "0", "data": entries}
            elif "long-short-account-ratio" in url:
                resp.json.return_value = {
                    "code": "0",
                    "data": [["1", "0.72"]],
                }
            return resp

        mock_get.side_effect = side_effect

        result = self.provider.get_derivatives_signals(price_change_pct=3.0)

        assert result.funding is not None
        assert result.funding.signal == "EXTREME"
        assert result.ls_ratio is not None
        assert result.ls_ratio.signal == "EXTREME_LONG"
        assert result.position_modifier < 1.0
        assert len(result.caution_flags) >= 1
