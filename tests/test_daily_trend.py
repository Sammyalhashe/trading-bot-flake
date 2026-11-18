import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backtesting"))

from strategies import create_strategy  # noqa: E402
from strategies.daily_trend import DailyTrendStrategy  # noqa: E402

DAY = 86400


def daily_df(closes, last_day_start=None, column="start"):
    """Daily candles ending with the day that started at last_day_start (default: yesterday, closed)."""
    if last_day_start is None:
        last_day_start = (int(time.time()) // DAY - 1) * DAY
    starts = [last_day_start - (len(closes) - 1 - i) * DAY for i in range(len(closes))]
    return pd.DataFrame({column: pd.to_datetime(starts, unit="s"), "open": closes, "high": closes,
                         "low": closes, "close": closes, "volume": [1.0] * len(closes)})


def cfg(**overrides):
    base = dict(daily_trend_assets="BTC", daily_trend_filter="sma:5", daily_trend_buffer_pct=0.0,
                daily_trend_confirm_days=1, daily_trend_stop_pct=0.0)
    base.update(overrides)
    return SimpleNamespace(**base)


def strat(**overrides):
    return DailyTrendStrategy(MagicMock(), cfg(**overrides))


RISING = [100.0] * 10 + [110.0]
FALLING = [100.0] * 10 + [90.0]


class TestSignal:
    def test_enters_when_close_above_average(self):
        candidate = strat().scan_entry("BTC", "BTC-USDC", {"1d": daily_df(RISING)}, "BEAR", "STRONG_BEAR")
        assert candidate and candidate["asset"] == "BTC"

    def test_no_entry_below_average(self):
        assert strat().scan_entry("BTC", "BTC-USDC", {"1d": daily_df(FALLING)}, "BULL", "BULL") is None

    def test_ignores_assets_outside_universe(self):
        assert strat().scan_entry("ETH", "ETH-USDC", {"1d": daily_df(RISING)}, "BULL", "BULL") is None

    def test_buffer_requires_clearance(self):
        barely_above = [100.0] * 10 + [101.0]
        s = strat(daily_trend_buffer_pct=0.02)
        assert s.scan_entry("BTC", "BTC-USDC", {"1d": daily_df(barely_above)}, "BULL", "BULL") is None

    def test_confirmation_needs_consecutive_closes(self):
        one_day = [100.0] * 10 + [90.0, 110.0]
        two_days = [100.0] * 10 + [110.0, 112.0]
        s = strat(daily_trend_confirm_days=2)
        assert s.scan_entry("BTC", "BTC-USDC", {"1d": daily_df(one_day)}, "BULL", "BULL") is None
        assert s.scan_entry("BTC", "BTC-USDC", {"1d": daily_df(two_days)}, "BULL", "BULL")

    def test_in_progress_candle_is_ignored(self):
        today = int(time.time()) // DAY * DAY
        df = daily_df(FALLING + [200.0], last_day_start=today)  # today's live candle spikes above
        assert strat().scan_entry("BTC", "BTC-USDC", {"1d": df}, "BULL", "BULL") is None

    def test_accepts_backtest_timestamp_column(self):
        df = daily_df(RISING, column="timestamp")
        assert strat().scan_entry("BTC", "BTC-USDC", {"1d": df}, "BULL", "BULL")

    def test_ema_filter_and_window(self):
        s = strat(daily_trend_filter="ema:21:55", daily_trend_confirm_days=3)
        assert s.required_timeframes["1d"] >= 55 + 3
        assert s.uses_hourly_regime is False and s.should_skip_regime("BEAR", "STRONG_BEAR") is False

    def test_registered_with_factory(self):
        assert create_strategy("daily_trend", MagicMock(), cfg()).name == "daily_trend"


class TestExit:
    def _exit(self, closes, price=100.0, hwm=100.0, **overrides):
        return strat(**overrides).check_exit("BTC", "BTC-USDC", {"1d": daily_df(closes)}, price, 100.0, hwm,
                                             {}, {}, "X:BTC-USDC")

    def test_sells_everything_when_trend_turns_down(self):
        sell, ratio, reason, _ = self._exit(FALLING)
        assert sell and ratio == 1.0 and "trend" in reason.lower() and "stop" not in reason.lower()

    def test_holds_while_trend_up(self):
        assert self._exit(RISING)[0] is False

    def test_catastrophe_stop_from_hwm(self):
        sell, ratio, reason, _ = self._exit(RISING, price=80.0, hwm=100.0, daily_trend_stop_pct=0.15)
        assert sell and ratio == 1.0 and "stop" in reason.lower()

    def test_never_exits_assets_outside_universe(self):
        s = strat()
        assert s.check_exit("TIA", "TIA-USDC", {"1d": daily_df(FALLING)}, 1.0, 2.0, 2.0, {}, {}, "X:TIA-USDC")[0] is False


class TestBotIntegration:
    def test_bear_regime_does_not_shrink_daily_trend_buys(self, tmp_path):
        import trading_bot
        executor = MagicMock()
        executor.__class__.__name__ = "TestExecutor"
        del executor.account
        executor.get_balances.return_value = {"cash": {"USDC": 10000.0}, "crypto": {}}
        executor.get_supported_assets.return_value = ["BTC"]
        executor.place_limit_order.return_value = {"success": True}
        provider = MagicMock()
        provider.get_product_details.return_value = {"price": "110.0", "quote_increment": "0.01", "base_increment": "0.001"}
        provider.get_market_data.return_value = daily_df(RISING)
        daily = DailyTrendStrategy(trading_bot.technical_analysis, cfg())
        with patch.object(trading_bot, "STATE_FILE", tmp_path / "state.json"), \
             patch.object(trading_bot, "strategy", daily), \
             patch.object(trading_bot.config, "bear_position_scale", 0.25), \
             patch.object(trading_bot.config, "max_concurrent_positions", 1):
            trading_bot.run_executor_strategy(executor, provider, "BEAR", "STRONG_BEAR")
        buys = [c.kwargs["amount_quote_currency"] for c in executor.place_limit_order.call_args_list if c.args[1] == "BUY"]
        assert buys and buys[0] > 0.25 * 10000.0


class TestBacktesterDaily:
    def _engine(self, tmp_path, closes_by_day):
        from backtest import BacktestEngine
        from config import TradingConfig
        hourly_dir, daily_dir = tmp_path / "1h", tmp_path / "1d"
        hourly_dir.mkdir()
        daily_dir.mkdir()
        start = pd.Timestamp("2026-01-01")
        for asset, scale in (("BTC", 1.0), ("ETH", 0.05)):
            rows = []
            for d, close in enumerate(closes_by_day):
                for h in range(24):
                    ts = start + pd.Timedelta(days=d, hours=h)
                    rows.append({"timestamp": ts, "open": close * scale, "high": close * scale,
                                 "low": close * scale, "close": close * scale, "volume": 1e6})
            pd.DataFrame(rows).to_csv(hourly_dir / f"{asset}_USD_1h_full.csv", index=False)
        c = TradingConfig.from_env()
        c.daily_trend_assets, c.daily_trend_filter = "BTC", "sma:5"
        c.daily_trend_buffer_pct, c.daily_trend_confirm_days, c.daily_trend_stop_pct = 0.0, 1, 0.0
        c.max_concurrent_positions = 1
        files = [str(hourly_dir / "BTC_USD_1h_full.csv"), str(hourly_dir / "ETH_USD_1h_full.csv")]
        return BacktestEngine(c, 10_000), files

    def test_daily_slice_has_no_lookahead(self, tmp_path):
        engine, files = self._engine(tmp_path, [100.0] * 80)
        engine.run_backtest(files, "daily_trend")
        ts = pd.Timestamp("2026-02-10 05:00")
        days = engine._daily_slice("BTC", ts)
        assert days["timestamp"].iloc[-1] == pd.Timestamp("2026-02-09")

    def test_daily_trend_takes_a_full_position(self, tmp_path):
        closes = [100.0] * 60 + [100.0 + 2 * i for i in range(1, 21)]
        engine, files = self._engine(tmp_path, closes)
        engine.run_backtest(files, "daily_trend")
        buys = [t for t in engine.trades if t["type"] == "BUY"]
        assert buys and buys[0]["asset"] == "BTC" and buys[0]["cost"] > 9000
