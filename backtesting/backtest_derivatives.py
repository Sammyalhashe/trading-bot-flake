#!/usr/bin/env python3
"""
Backtest derivatives signals (funding rate + OI + L/S ratio) against historical data.

Compares bot performance WITH vs WITHOUT derivatives modifiers over the window
where we have historical OKX derivatives data.

Usage:
    python backtesting/backtest_derivatives.py

NOTE: This backtest is inherently limited by the ~30-90 day window of available
OKX derivatives history. Run the collector regularly to build more history.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from config import TradingConfig
from core import TechnicalAnalysis, RegimeDetector, RiskManager
from strategies import create_strategy
from backtesting.backtest import BacktestEngine


DERIVATIVES_DIR = Path(__file__).parent.parent / "data" / "derivatives"
OHLCV_DIR = Path(__file__).parent.parent / "data" / "backtest" / "YTD_2025_1h"

# Funding rate thresholds (matching live config defaults)
FUNDING_HIGH = 0.0005
FUNDING_EXTREME = 0.0010


def load_derivatives_data() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load all three derivatives CSV files."""
    funding = pd.read_csv(DERIVATIVES_DIR / "funding_rates.csv")
    funding["timestamp"] = pd.to_datetime(funding["timestamp"])
    funding = funding.sort_values("timestamp").reset_index(drop=True)

    oi = pd.read_csv(DERIVATIVES_DIR / "open_interest.csv")
    oi["timestamp"] = pd.to_datetime(oi["timestamp"])
    oi = oi.sort_values("timestamp").reset_index(drop=True)

    ls = pd.read_csv(DERIVATIVES_DIR / "long_short_ratio.csv")
    ls["timestamp"] = pd.to_datetime(ls["timestamp"])
    ls = ls.sort_values("timestamp").reset_index(drop=True)

    return funding, oi, ls


def build_hourly_derivatives(
    funding: pd.DataFrame,
    oi: pd.DataFrame,
    ls: pd.DataFrame,
) -> pd.DataFrame:
    """
    Merge all three into a single hourly DataFrame.
    Funding rate (8h) is forward-filled to hourly.
    """
    # Create hourly grid from earliest to latest
    start = min(funding["timestamp"].min(), oi["timestamp"].min(), ls["timestamp"].min())
    end = max(funding["timestamp"].max(), oi["timestamp"].max(), ls["timestamp"].max())
    hourly = pd.date_range(start=start, end=end, freq="h")
    df = pd.DataFrame({"timestamp": hourly})

    # Merge funding (forward-fill from 8h settlements)
    funding_indexed = funding.set_index("timestamp")["funding_rate"]
    df = df.set_index("timestamp")
    df["funding_rate"] = funding_indexed.reindex(df.index).ffill()

    # Merge OI (already 1h)
    oi_indexed = oi.set_index("timestamp")["open_interest_usd"]
    df["open_interest_usd"] = oi_indexed.reindex(df.index).ffill()

    # Merge L/S ratio
    ls_indexed = ls.set_index("timestamp")["long_ratio"]
    df["long_ratio"] = ls_indexed.reindex(df.index).ffill()

    df = df.reset_index()
    return df


def compute_derivatives_modifier(
    funding_rate: float,
    oi_change_pct: float,
    long_ratio: float,
) -> Tuple[float, bool, List[str]]:
    """
    Compute position modifier and entry_allowed from raw derivatives values.
    Returns (position_modifier, entry_allowed, caution_flags).
    Mirrors DerivativesDataProvider.compute_position_modifier logic.
    """
    modifier = 1.0
    flags = []

    # Funding rate classification
    if pd.isna(funding_rate):
        funding_signal = None
    elif funding_rate < -FUNDING_HIGH:
        funding_signal = "EXTREME_NEGATIVE"
    elif funding_rate < -0.0001:
        funding_signal = "NEGATIVE"
    elif funding_rate <= 0.0003:
        funding_signal = "NORMAL"
    elif funding_rate <= FUNDING_HIGH:
        funding_signal = "ELEVATED"
    else:
        funding_signal = "EXTREME"

    if funding_signal == "EXTREME":
        modifier *= 0.50
        flags.append(f"Extreme funding ({funding_rate*100:.4f}%)")
    elif funding_signal == "ELEVATED":
        modifier *= 0.75
        flags.append(f"Elevated funding ({funding_rate*100:.4f}%)")
    elif funding_signal == "EXTREME_NEGATIVE":
        modifier *= 1.25
        flags.append(f"Extreme neg funding ({funding_rate*100:.4f}%)")
    elif funding_signal == "NEGATIVE":
        modifier *= 1.10

    # L/S ratio
    if not pd.isna(long_ratio) and long_ratio > 0.65:
        modifier *= 0.75
        flags.append(f"Crowded long ({long_ratio:.0%})")

    modifier = max(0.25, min(1.25, modifier))

    # OI divergence — needs price context, computed separately
    # For simplicity, we use OI 24h change which is pre-computed
    oi_signal = "STABLE"
    if not pd.isna(oi_change_pct):
        if oi_change_pct > 10.0:
            oi_signal = "RISING"
        elif oi_change_pct < -5.0:
            oi_signal = "FALLING"

    return modifier, oi_signal, flags


class DerivativesBacktestEngine(BacktestEngine):
    """
    Extended backtest engine that applies derivatives signals to position sizing and entry filtering.
    """

    def __init__(self, config: TradingConfig, derivatives_df: pd.DataFrame, use_derivatives: bool, initial_capital: float = 10000):
        super().__init__(config, initial_capital)
        self.deriv_df = derivatives_df.set_index("timestamp") if derivatives_df is not None else None
        self.use_derivatives = use_derivatives
        self.blocked_entries = 0
        self.modifier_sum = 0.0
        self.modifier_count = 0

    def get_derivatives_at(self, timestamp: pd.Timestamp) -> Tuple[float, str, List[str]]:
        """Look up the derivatives modifier for a given hourly timestamp."""
        if self.deriv_df is None or not self.use_derivatives:
            return 1.0, "STABLE", []

        # Find nearest row at or before this timestamp
        candidates = self.deriv_df[self.deriv_df.index <= timestamp]
        if candidates.empty:
            return 1.0, "STABLE", []

        row = candidates.iloc[-1]
        funding_rate = row.get("funding_rate", float("nan"))
        long_ratio = row.get("long_ratio", float("nan"))
        oi = row.get("open_interest_usd", float("nan"))

        # 24h OI change
        oi_change_pct = float("nan")
        ts_24h_ago = timestamp - pd.Timedelta(hours=24)
        oi_24h = self.deriv_df[self.deriv_df.index <= ts_24h_ago]
        if not oi_24h.empty and not pd.isna(oi):
            old_oi = oi_24h.iloc[-1].get("open_interest_usd", float("nan"))
            if not pd.isna(old_oi) and old_oi > 0:
                oi_change_pct = (oi - old_oi) / old_oi * 100

        modifier, oi_signal, flags = compute_derivatives_modifier(funding_rate, oi_change_pct, long_ratio)
        return modifier, oi_signal, flags

    def _check_entries(self, strategy, prices: Dict[str, float],
                       dfs: Dict[str, pd.DataFrame], market_regime: str,
                       full_regime: str, timestamp):
        """Override to apply derivatives signals."""
        if len(self.positions) >= self.config.max_concurrent_positions:
            return
        if strategy.should_skip_regime(market_regime, full_regime):
            return

        # Get derivatives context
        deriv_modifier, oi_signal, deriv_flags = self.get_derivatives_at(timestamp)

        # Check 24h BTC price change for OI divergence
        btc_price_change = 0.0
        btc_df = dfs.get("BTC-USD")
        if btc_df is not None and len(btc_df) >= 24:
            btc_price_change = (btc_df.iloc[-1]["close"] / btc_df.iloc[-24]["close"] - 1) * 100

        # OI divergence: price up, OI falling → block entries
        if self.use_derivatives and oi_signal == "FALLING" and btc_price_change > 1.0:
            self.blocked_entries += 1
            return

        candidates = []
        for asset, df in dfs.items():
            if asset in self.positions or asset not in prices:
                continue
            candidate = strategy.scan_entry(
                asset=asset.split('-')[0],
                product_id=asset,
                df=df,
                market_regime=market_regime,
                full_regime=full_regime,
            )
            if candidate:
                candidates.append(candidate)

        if not candidates:
            return

        ranked = strategy.rank_candidates(candidates)
        positions_to_open = self.config.max_concurrent_positions - len(self.positions)

        for candidate in ranked[:positions_to_open]:
            asset = candidate['product_id']
            price = prices[asset]
            portfolio_value = self.get_portfolio_value(prices)
            target_allocation = portfolio_value / self.config.max_concurrent_positions

            if market_regime == "BEAR" and self.config.bear_position_scale < 1.0:
                target_allocation *= self.config.bear_position_scale

            # Apply derivatives modifier
            if self.use_derivatives:
                target_allocation *= deriv_modifier
                self.modifier_sum += deriv_modifier
                self.modifier_count += 1

            if target_allocation < float(self.config.min_order_usd):
                continue

            position_size = target_allocation / price
            cost = position_size * price
            buy_fee = self.risk_manager.calculate_fees(cost, is_round_trip=False)
            total_cost = cost + buy_fee

            if total_cost > self.capital * 0.95:
                continue

            self.capital -= total_cost
            self.total_fees += buy_fee
            self.positions[asset] = {
                'size': position_size,
                'entry': price,
                'hwm': price,
                'tp_flags': {}
            }


def run_comparison(ohlcv_files: List[str], start_date: str, end_date: str) -> None:
    """Run both backtests (with vs without derivatives) and print comparison."""
    print(f"\nBacktest window: {start_date} → {end_date}")
    print(f"Loading OHLCV data...")

    config = TradingConfig.from_env()

    # Load and filter OHLCV data to the overlap window
    datasets = {}
    for fpath in ohlcv_files:
        p = Path(fpath)
        # Parse asset from filename (BTC_USD_1h -> BTC-USD)
        parts = p.stem.split("_")
        asset = f"{parts[0]}-{parts[1]}"
        df = pd.read_csv(fpath)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df[(df["timestamp"] >= start_date) & (df["timestamp"] <= end_date)]
        df = df.sort_values("timestamp").reset_index(drop=True)
        datasets[asset] = df
        print(f"  {asset}: {len(df)} hourly candles")

    if not datasets:
        print("No data loaded. Aborting.")
        return

    # Load derivatives data
    print("\nLoading derivatives data...")
    try:
        funding, oi, ls = load_derivatives_data()
        hourly_deriv = build_hourly_derivatives(funding, oi, ls)
        # Filter to window
        hourly_deriv = hourly_deriv[
            (hourly_deriv["timestamp"] >= start_date) &
            (hourly_deriv["timestamp"] <= end_date)
        ]
        print(f"  Derivatives: {len(hourly_deriv)} hourly rows")
        print(f"  Funding coverage: {funding['timestamp'].min().date()} → {funding['timestamp'].max().date()}")
        print(f"  OI coverage:      {oi['timestamp'].min().date()} → {oi['timestamp'].max().date()}")
    except FileNotFoundError as e:
        print(f"  FAILED: {e}. Run download_derivatives_data.py first.")
        return

    # Run two backtests
    results = {}
    for label, use_deriv in [("Without derivatives", False), ("With derivatives", True)]:
        engine = DerivativesBacktestEngine(
            config=config,
            derivatives_df=hourly_deriv if use_deriv else None,
            use_derivatives=use_deriv,
            initial_capital=10000,
        )

        strategy = create_strategy("trend_following", engine.ta, config)
        primary_asset = list(datasets.keys())[0]
        primary_df = datasets[primary_asset]
        start_idx = max(config.ma_long_window, 50)

        for idx in range(start_idx, len(primary_df)):
            timestamp = primary_df.iloc[idx]["timestamp"]
            current_prices = {}
            current_dfs = {}

            for asset, df in datasets.items():
                asset_df = df[df["timestamp"] <= timestamp]
                if len(asset_df) > 0:
                    current_prices[asset] = float(asset_df.iloc[-1]["close"])
                    current_dfs[asset] = asset_df

            if primary_asset in current_dfs:
                market_regime, full_regime = engine.detect_regime(current_dfs[primary_asset])
            else:
                market_regime, full_regime = "NEUTRAL", "NEUTRAL"

            engine._check_exits(strategy, current_prices, current_dfs, timestamp)
            engine._check_entries(strategy, current_prices, current_dfs, market_regime, full_regime, timestamp)

            portfolio_value = engine.get_portfolio_value(current_prices)
            engine.equity_curve.append({
                "timestamp": timestamp,
                "portfolio_value": portfolio_value,
            })

        # Close remaining positions at end
        if current_prices:
            for asset in list(engine.positions.keys()):
                if asset in current_prices:
                    engine._close_position(asset, current_prices[asset], 1.0, "Backtest end", timestamp)

        metrics = engine._calculate_metrics("trend_following")
        metrics["blocked_entries"] = engine.blocked_entries
        if engine.modifier_count > 0:
            metrics["avg_modifier"] = engine.modifier_sum / engine.modifier_count
        else:
            metrics["avg_modifier"] = 1.0
        results[label] = (metrics, engine)

    # Print comparison
    print("\n" + "=" * 60)
    print(f"{'METRIC':<30} {'WITHOUT':>12} {'WITH':>12} {'DELTA':>10}")
    print("=" * 60)

    keys = [
        ("Total Return (%)", "total_return_pct", ".2f"),
        ("Final Value ($)", "final_value", ".2f"),
        ("Sharpe Ratio", "sharpe_ratio", ".3f"),
        ("Max Drawdown (%)", "max_drawdown_pct", ".2f"),
        ("Win Rate (%)", "win_rate", ".1f"),
        ("Total Trades", "total_trades", ".0f"),
        ("Total Fees ($)", "total_fees", ".2f"),
    ]

    no_deriv = results["Without derivatives"][0]
    with_deriv = results["With derivatives"][0]

    for label, key, fmt in keys:
        v1 = no_deriv.get(key, 0) or 0
        v2 = with_deriv.get(key, 0) or 0
        delta = v2 - v1
        delta_str = f"{delta:+{fmt}}"
        print(f"{label:<30} {v1:>{12}{fmt}} {v2:>{12}{fmt}} {delta_str:>10}")

    # Extra stats
    print("=" * 60)
    blocked = with_deriv.get("blocked_entries", 0)
    avg_mod = with_deriv.get("avg_modifier", 1.0)
    print(f"{'Entries blocked (OI divergence)':<30} {'':<12} {blocked:>12}")
    print(f"{'Avg position modifier':<30} {'':<12} {avg_mod:>12.3f}")

    # Monthly funding rate summary
    print("\n--- Funding Rate Signal Distribution ---")
    hourly_deriv_trimmed = hourly_deriv.dropna(subset=["funding_rate"])
    if not hourly_deriv_trimmed.empty:
        fr = hourly_deriv_trimmed["funding_rate"]
        extreme_neg = (fr < -FUNDING_HIGH).sum()
        negative = ((fr < -0.0001) & (fr >= -FUNDING_HIGH)).sum()
        normal = ((fr >= -0.0001) & (fr <= 0.0003)).sum()
        elevated = ((fr > 0.0003) & (fr <= FUNDING_HIGH)).sum()
        extreme = (fr > FUNDING_HIGH).sum()
        total_fr = len(fr)
        print(f"  Extreme negative (<-{FUNDING_HIGH*100:.2f}%): {extreme_neg:4d} rows ({extreme_neg/total_fr*100:.1f}%)")
        print(f"  Negative (<-0.01%):           {negative:4d} rows ({negative/total_fr*100:.1f}%)")
        print(f"  Normal:                        {normal:4d} rows ({normal/total_fr*100:.1f}%)")
        print(f"  Elevated (>{FUNDING_HIGH*100:.2f}%):            {elevated:4d} rows ({elevated/total_fr*100:.1f}%)")
        print(f"  Extreme (>{FUNDING_HIGH_EXTREME*100:.2f}%):           {extreme:4d} rows ({extreme/total_fr*100:.1f}%)")

    print(f"\n⚠ Note: Limited to {(pd.to_datetime(end_date) - pd.to_datetime(start_date)).days} day window.")
    print("  Run collect_derivatives_data.py hourly to build more history.")


FUNDING_HIGH_EXTREME = FUNDING_EXTREME


if __name__ == "__main__":
    # Overlap window: funding goes back to Jan 14, OHLCV goes to Mar 31
    # OI only goes back 30 days — use funding window as primary
    OHLCV_FILES = [
        str(OHLCV_DIR / "BTC_USD_1h_2025-01-01_2026-03-31.csv"),
    ]

    # Use the funding rate window (longest available history)
    START = "2026-01-14"
    END = "2026-03-31"

    run_comparison(OHLCV_FILES, START, END)
