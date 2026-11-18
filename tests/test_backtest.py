import os
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backtesting"))

from backtest import BacktestEngine  # noqa: E402
from config import TradingConfig  # noqa: E402
from core.risk_manager import RiskManager  # noqa: E402
from strategies import create_strategy  # noqa: E402

START = pd.Timestamp("2026-01-01 00:00:00")


def write_candles(tmp_path, asset, closes, spread=0.01, volume=1_000_000.0):
    rows = []
    for i, close in enumerate(closes):
        rows.append({
            "timestamp": START + pd.Timedelta(hours=i),
            "open": close, "high": close * (1 + spread), "low": close * (1 - spread),
            "close": close, "volume": volume,
        })
    path = tmp_path / f"{asset}_USD_1h_full.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    return str(path)


def zigzag_trend(start, step_up, step_down, n):
    prices, p = [], start
    for i in range(n):
        p = p + step_up if i % 2 == 0 else p - step_down
        prices.append(p)
    return prices


@pytest.fixture
def config():
    c = TradingConfig.from_env()
    c.max_concurrent_positions = 5
    return c


class TestRegimeSource:
    def test_regime_follows_btc_not_first_file(self, tmp_path, config):
        n = 200
        files = [
            write_candles(tmp_path, "AAVE", [100 + i for i in range(n)]),
            write_candles(tmp_path, "BTC", [100_000 - 300 * i for i in range(n)]),
            write_candles(tmp_path, "ETH", [4_000 - 15 * i for i in range(n)]),
        ]
        engine = BacktestEngine(config, 10_000)
        engine.run_backtest(files, "trend_following")
        regimes = {row["regime"] for row in engine.equity_curve[-50:]}
        assert regimes <= {"BEAR", "STRONG_BEAR"}

    def test_missing_btc_data_is_an_error(self, tmp_path, config):
        files = [write_candles(tmp_path, "AAVE", [100.0] * 100)]
        with pytest.raises(ValueError, match="BTC"):
            BacktestEngine(config, 10_000).run_backtest(files, "trend_following")


class TestIntrabarStops:
    def _engine_holding(self, config, hwm=100.0):
        engine = BacktestEngine(config, 10_000)
        engine.positions["AAVE"] = {"size": 10.0, "entry": hwm, "hwm": hwm, "tp_flags": {}}
        return engine

    def _df_with_last_bar(self, open_, low, close):
        n = 60
        rows = [{"timestamp": START + pd.Timedelta(hours=i), "open": 100.0, "high": 101.0,
                 "low": 99.0, "close": 100.0, "volume": 1e6} for i in range(n)]
        rows.append({"timestamp": START + pd.Timedelta(hours=n), "open": open_, "high": max(open_, close),
                     "low": low, "close": close, "volume": 1e6})
        return pd.DataFrame(rows)

    def _run_exit(self, config, df):
        engine = self._engine_holding(config)
        strategy = create_strategy("trend_following", engine.ta, config)
        ts = df["timestamp"].iloc[-1]
        engine._check_exits(strategy, {"AAVE": df["close"].iloc[-1]}, {"AAVE": df}, ts)
        return engine

    def test_stop_fills_at_stop_price_when_low_pierces_it(self, config):
        # ATR ~2% → 6% floor stop from HWM 100 → stop at 94
        engine = self._run_exit(config, self._df_with_last_bar(open_=99.0, low=90.0, close=98.0))
        sells = [t for t in engine.trades if t["type"] == "SELL"]
        assert len(sells) == 1
        assert sells[0]["price"] == pytest.approx(94.0, rel=1e-3)
        assert "AAVE" in engine.stopout_times

    def test_gap_below_stop_fills_at_open(self, config):
        engine = self._run_exit(config, self._df_with_last_bar(open_=92.0, low=90.0, close=91.0))
        sells = [t for t in engine.trades if t["type"] == "SELL"]
        assert sells[0]["price"] == pytest.approx(92.0)

    def test_hwm_tracks_bar_high(self, config):
        engine = self._engine_holding(config)
        strategy = create_strategy("trend_following", engine.ta, config)
        df = self._df_with_last_bar(open_=100.0, low=99.5, close=100.0)
        df.loc[df.index[-1], "high"] = 103.0
        engine._check_exits(strategy, {"AAVE": 100.0}, {"AAVE": df}, df["timestamp"].iloc[-1])
        assert engine.positions["AAVE"]["hwm"] == pytest.approx(103.0)


class TestDrawdownGuard:
    def test_risk_manager_cooldown_uses_injected_clock(self, config):
        now = [0.0]
        rm = RiskManager(config, 10_000, clock=lambda: now[0])
        assert rm.check_circuit_breakers(10_000, "x")[0]
        assert not rm.check_circuit_breakers(8_000, "x")[0]
        now[0] = (float(config.drawdown_cooldown_hours) - 1) * 3600
        assert not rm.check_circuit_breakers(8_000, "x")[0]
        now[0] = (float(config.drawdown_cooldown_hours) + 1) * 3600
        assert rm.check_circuit_breakers(8_000, "x")[0]

    def _bullish_files(self, tmp_path):
        n = 200
        return [
            write_candles(tmp_path, "BTC", zigzag_trend(50_000, 260, 150, n)),
            write_candles(tmp_path, "ETH", zigzag_trend(3_000, 20, 10, n)),
            write_candles(tmp_path, "AAVE", zigzag_trend(100, 0.6, 0.35, n)),
        ]

    def test_backtest_buys_without_drawdown(self, tmp_path, config):
        engine = BacktestEngine(config, 10_000)
        engine.run_backtest(self._bullish_files(tmp_path), "trend_following")
        assert any(t["type"] == "BUY" for t in engine.trades)

    def test_drawdown_pause_blocks_entries_until_cooldown_in_bar_time(self, tmp_path, config):
        engine = BacktestEngine(config, 10_000)
        engine.risk_manager._set_peak_value(1e9, BacktestEngine.EXECUTOR_ID)
        engine.run_backtest(self._bullish_files(tmp_path), "trend_following")
        paused_from = engine.equity_curve[0]["timestamp"]
        resumes_at = paused_from + pd.Timedelta(hours=float(config.drawdown_cooldown_hours))
        buys = [t["timestamp"] for t in engine.trades if t["type"] == "BUY"]
        assert buys and min(buys) >= resumes_at


class TestWarmup:
    def test_start_keeps_indicator_warmup(self, tmp_path, config):
        n = 200
        files = [
            write_candles(tmp_path, "BTC", [100_000.0] * n),
            write_candles(tmp_path, "ETH", [4_000.0] * n),
        ]
        start = START + pd.Timedelta(hours=100)
        engine = BacktestEngine(config, 10_000)
        engine.run_backtest(files, "trend_following", start=start)
        assert engine.equity_curve[0]["timestamp"] == start
