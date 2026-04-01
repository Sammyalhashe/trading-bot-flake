#!/usr/bin/env python3
"""
Experiment: Can we catch rallies during BEAR regime without getting wrecked by dumps?

Experiment 3: Shorter MA windows (env-var only, no code changes)
  - MA50/200 (baseline), MA20/100, MA20/50, MA10/30

Experiment 2: Reduced position sizing in BEAR regime
  - bear_position_scale: 0.0 (current), 0.25, 0.50, 0.75, 1.0
  - Allows entries in BEAR but with reduced size

Both experiments run on existing downloaded data across all periods.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os
import pandas as pd
import numpy as np
from datetime import datetime
import logging
from typing import Dict, List, Tuple
from collections import defaultdict
from copy import deepcopy

from config import TradingConfig
from core import TechnicalAnalysis, RegimeDetector, RiskManager
from strategies import create_strategy
from backtesting.backtest import BacktestEngine, print_results

logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class ExperimentEngine(BacktestEngine):
    """Extended backtest engine with bear position scaling support."""

    def __init__(self, config: TradingConfig, initial_capital: float = 10000,
                 bear_position_scale: float = 0.0, allow_all_bear_entries: bool = False):
        super().__init__(config, initial_capital)
        self.bear_position_scale = bear_position_scale
        self.allow_all_bear_entries = allow_all_bear_entries

    def _check_entries(self, strategy, prices, dfs, market_regime, full_regime, timestamp):
        """Override to support bear position scaling and relaxed bear entry rules."""
        max_concurrent_positions = self.config.max_concurrent_positions
        if len(self.positions) >= max_concurrent_positions:
            return

        # If bear_position_scale > 0, don't skip BEAR regime
        if self.bear_position_scale > 0 and full_regime in ("BEAR", "STRONG_BEAR"):
            pass  # Allow through
        elif strategy.should_skip_regime(market_regime, full_regime):
            return

        candidates = []
        for asset, df in dfs.items():
            if asset in self.positions:
                continue
            if asset not in prices:
                continue

            # For bear position scaling: override the strategy's internal bear check
            if self.allow_all_bear_entries and market_regime == "BEAR":
                candidate = self._bear_scan_entry(strategy, asset, asset, df, market_regime, full_regime)
            else:
                candidate = strategy.scan_entry(
                    asset=asset.split('-')[0],
                    product_id=asset,
                    df=df,
                    market_regime=market_regime,
                    full_regime=full_regime
                )

            if candidate:
                candidates.append(candidate)

        if not candidates:
            return

        ranked = strategy.rank_candidates(candidates)
        positions_to_open = max_concurrent_positions - len(self.positions)

        for candidate in ranked[:positions_to_open]:
            asset = candidate['product_id']
            price = prices[asset]

            portfolio_value = self.get_portfolio_value(prices)
            target_allocation = portfolio_value / max_concurrent_positions

            # Scale down in BEAR regime
            if market_regime == "BEAR" and self.bear_position_scale < 1.0:
                target_allocation *= self.bear_position_scale

            if target_allocation < 10:  # Skip tiny allocations
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

            scale_note = f" [bear_scale={self.bear_position_scale}]" if market_regime == "BEAR" else ""
            self.trades.append({
                'timestamp': timestamp,
                'asset': asset,
                'type': 'BUY',
                'price': price,
                'size': position_size,
                'cost': cost,
                'fee': buy_fee
            })

    def _bear_scan_entry(self, strategy, asset, product_id, df, market_regime, full_regime):
        """Momentum-based entry for BEAR regime — catches rallies during downtrends."""
        momentum = self.ta.get_momentum_ranking(df, self.config.momentum_window_hours)
        if momentum < 2.0:  # Require >2% 24h momentum
            return None

        rsi = self.ta.calculate_rsi(df)
        if rsi is None:
            return None
        if rsi > float(self.config.rsi_overbought):
            return None
        if rsi < 35:  # Skip deeply oversold (likely dumping)
            return None

        # Volume filter
        min_volume = float(self.config.min_24h_volume_usd)
        if df is not None and len(df) >= 24:
            volume_24h = df['volume'].iloc[-24:].sum()
            close_price = df['close'].iloc[-1]
            usd_volume_24h = volume_24h * close_price
            if usd_volume_24h < min_volume:
                return None

        return {"asset": asset, "product_id": product_id, "score": momentum}


def find_data_files(data_dir: str, symbols: list) -> list:
    """Find data files for given symbols in a directory."""
    data_path = Path(data_dir)
    if not data_path.exists():
        return []

    files = []
    for symbol in symbols:
        pattern = symbol.replace('-', '_')
        matches = list(data_path.glob(f"{pattern}*.csv"))
        if matches:
            files.append(str(matches[0]))
    return files


def run_experiment_3(periods: dict, symbols: list, initial_capital: float) -> list:
    """Experiment 3: Test shorter MA windows."""
    ma_configs = [
        ("MA50/200", 50, 200),   # baseline
        ("MA20/100", 20, 100),   # moderate speedup
        ("MA20/50", 20, 50),     # fast
        ("MA10/30", 10, 30),     # very fast
    ]

    results = []
    strategies = ["auto", "trend_following"]

    for period_key, period_info in periods.items():
        for timeframe in ["1h"]:  # Focus on 1h which is what the live bot uses
            data_dir = f"data/backtest/{period_key}_{timeframe}"
            data_files = find_data_files(data_dir, symbols)

            if not data_files:
                logger.warning(f"No data for {period_key}/{timeframe}, skipping")
                continue

            for ma_name, short_w, long_w in ma_configs:
                for strategy_name in strategies:
                    # Set env vars for MA windows
                    os.environ["SHORT_WINDOW"] = str(short_w)
                    os.environ["LONG_WINDOW"] = str(long_w)
                    config = TradingConfig.from_env()

                    engine = BacktestEngine(config, initial_capital=initial_capital)
                    result = engine.run_backtest(data_files, strategy_name)

                    if result:
                        results.append({
                            "experiment": "MA_windows",
                            "ma_config": ma_name,
                            "short_window": short_w,
                            "long_window": long_w,
                            "strategy": strategy_name,
                            "period": period_key,
                            "timeframe": timeframe,
                            "total_return_pct": result["total_return_pct"],
                            "max_drawdown_pct": result["max_drawdown_pct"],
                            "sharpe_ratio": result["sharpe_ratio"],
                            "num_trades": result["num_trades"],
                            "win_rate_pct": result["win_rate_pct"],
                            "profit_factor": result["profit_factor"],
                            "total_fees": result["total_fees"],
                        })

                        logger.info(
                            f"  {ma_name:>10} | {strategy_name:>16} | {period_key:>20} | "
                            f"Return: {result['total_return_pct']:>+8.2f}% | "
                            f"MaxDD: {result['max_drawdown_pct']:>7.2f}% | "
                            f"Sharpe: {result['sharpe_ratio']:>6.2f} | "
                            f"Trades: {result['num_trades']:>4}"
                        )

    # Restore defaults
    os.environ["SHORT_WINDOW"] = "50"
    os.environ["LONG_WINDOW"] = "200"
    return results


def run_experiment_2(periods: dict, symbols: list, initial_capital: float) -> list:
    """Experiment 2: Test bear position scaling."""
    bear_scales = [0.0, 0.25, 0.50, 0.75, 1.0]

    results = []
    strategies = ["auto", "trend_following"]

    for period_key, period_info in periods.items():
        for timeframe in ["1h"]:
            data_dir = f"data/backtest/{period_key}_{timeframe}"
            data_files = find_data_files(data_dir, symbols)

            if not data_files:
                logger.warning(f"No data for {period_key}/{timeframe}, skipping")
                continue

            for scale in bear_scales:
                for strategy_name in strategies:
                    os.environ["SHORT_WINDOW"] = "50"
                    os.environ["LONG_WINDOW"] = "200"
                    config = TradingConfig.from_env()

                    engine = ExperimentEngine(
                        config,
                        initial_capital=initial_capital,
                        bear_position_scale=scale,
                        allow_all_bear_entries=(scale > 0),
                    )
                    result = engine.run_backtest(data_files, strategy_name)

                    if result:
                        results.append({
                            "experiment": "bear_position_scale",
                            "bear_scale": scale,
                            "strategy": strategy_name,
                            "period": period_key,
                            "timeframe": timeframe,
                            "total_return_pct": result["total_return_pct"],
                            "max_drawdown_pct": result["max_drawdown_pct"],
                            "sharpe_ratio": result["sharpe_ratio"],
                            "num_trades": result["num_trades"],
                            "win_rate_pct": result["win_rate_pct"],
                            "profit_factor": result["profit_factor"],
                            "total_fees": result["total_fees"],
                        })

                        logger.info(
                            f"  scale={scale:.2f} | {strategy_name:>16} | {period_key:>20} | "
                            f"Return: {result['total_return_pct']:>+8.2f}% | "
                            f"MaxDD: {result['max_drawdown_pct']:>7.2f}% | "
                            f"Sharpe: {result['sharpe_ratio']:>6.2f} | "
                            f"Trades: {result['num_trades']:>4}"
                        )

    return results


def print_experiment_report(exp3_results: list, exp2_results: list):
    """Print combined experiment report."""
    print("\n" + "=" * 110)
    print("EXPERIMENT RESULTS: CATCHING RALLIES IN BEAR REGIME")
    print("=" * 110)

    # --- Experiment 3: MA Windows ---
    if exp3_results:
        df3 = pd.DataFrame(exp3_results)

        print("\n" + "-" * 110)
        print("EXPERIMENT 3: SHORTER MA WINDOWS")
        print("-" * 110)

        # Summary by MA config
        print(f"\n{'MA Config':>12} | {'Strategy':>16} | {'Avg Return':>12} | {'Avg MaxDD':>12} | "
              f"{'Avg Sharpe':>12} | {'Avg Trades':>12} | {'Avg WinRate':>12}")
        print("-" * 110)

        for ma_config in df3['ma_config'].unique():
            for strategy in df3['strategy'].unique():
                subset = df3[(df3['ma_config'] == ma_config) & (df3['strategy'] == strategy)]
                if len(subset) == 0:
                    continue
                print(f"{ma_config:>12} | {strategy:>16} | "
                      f"{subset['total_return_pct'].mean():>+11.2f}% | "
                      f"{subset['max_drawdown_pct'].mean():>11.2f}% | "
                      f"{subset['sharpe_ratio'].mean():>12.2f} | "
                      f"{subset['num_trades'].mean():>12.1f} | "
                      f"{subset['win_rate_pct'].mean():>11.1f}%")

        # Breakdown by period
        print(f"\nBreakdown by period:")
        print(f"{'MA Config':>12} | {'Strategy':>16} | {'Period':>22} | {'Return':>10} | "
              f"{'MaxDD':>10} | {'Sharpe':>8} | {'Trades':>8}")
        print("-" * 110)

        for _, row in df3.sort_values(['ma_config', 'strategy', 'period']).iterrows():
            print(f"{row['ma_config']:>12} | {row['strategy']:>16} | {row['period']:>22} | "
                  f"{row['total_return_pct']:>+9.2f}% | {row['max_drawdown_pct']:>9.2f}% | "
                  f"{row['sharpe_ratio']:>8.2f} | {row['num_trades']:>8}")

    # --- Experiment 2: Bear Position Scaling ---
    if exp2_results:
        df2 = pd.DataFrame(exp2_results)

        print("\n" + "-" * 110)
        print("EXPERIMENT 2: BEAR POSITION SCALING")
        print("-" * 110)

        # Summary by scale
        print(f"\n{'Bear Scale':>12} | {'Strategy':>16} | {'Avg Return':>12} | {'Avg MaxDD':>12} | "
              f"{'Avg Sharpe':>12} | {'Avg Trades':>12} | {'Avg WinRate':>12}")
        print("-" * 110)

        for scale in sorted(df2['bear_scale'].unique()):
            for strategy in df2['strategy'].unique():
                subset = df2[(df2['bear_scale'] == scale) & (df2['strategy'] == strategy)]
                if len(subset) == 0:
                    continue
                print(f"{scale:>12.2f} | {strategy:>16} | "
                      f"{subset['total_return_pct'].mean():>+11.2f}% | "
                      f"{subset['max_drawdown_pct'].mean():>11.2f}% | "
                      f"{subset['sharpe_ratio'].mean():>12.2f} | "
                      f"{subset['num_trades'].mean():>12.1f} | "
                      f"{subset['win_rate_pct'].mean():>11.1f}%")

        # Breakdown by period
        print(f"\nBreakdown by period:")
        print(f"{'Bear Scale':>12} | {'Strategy':>16} | {'Period':>22} | {'Return':>10} | "
              f"{'MaxDD':>10} | {'Sharpe':>8} | {'Trades':>8}")
        print("-" * 110)

        for _, row in df2.sort_values(['bear_scale', 'strategy', 'period']).iterrows():
            print(f"{row['bear_scale']:>12.2f} | {row['strategy']:>16} | {row['period']:>22} | "
                  f"{row['total_return_pct']:>+9.2f}% | {row['max_drawdown_pct']:>9.2f}% | "
                  f"{row['sharpe_ratio']:>8.2f} | {row['num_trades']:>8}")

    # Save all results
    all_results = exp3_results + exp2_results
    if all_results:
        results_dir = Path("data/results")
        results_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(all_results).to_csv(
            results_dir / "bear_rally_experiment_results.csv", index=False
        )
        print(f"\nResults saved to data/results/bear_rally_experiment_results.csv")

    print("\n" + "=" * 110)


def main():
    symbols = ["BTC-USD"]

    periods = {
        "Q1_2024_Bull": {"description": "Bull Market (BTC $42k->$61k)"},
        "Q2_2023_Sideways": {"description": "Sideways/Choppy Market"},
        "2023_Full_Year": {"description": "Full Year 2023 (Mixed)"},
        "YTD_2025": {"description": "YTD 2025-2026 (Recent)"},
        "H2_2024": {"description": "H2 2024"},
    }

    initial_capital = 10000

    logger.info("=" * 80)
    logger.info("BEAR RALLY EXPERIMENT SUITE")
    logger.info("=" * 80)

    # Run Experiment 3 first (no code changes needed)
    logger.info("\n--- EXPERIMENT 3: Shorter MA Windows ---")
    exp3_results = run_experiment_3(periods, symbols, initial_capital)

    # Run Experiment 2 (bear position scaling)
    logger.info("\n--- EXPERIMENT 2: Bear Position Scaling ---")
    exp2_results = run_experiment_2(periods, symbols, initial_capital)

    # Print report
    print_experiment_report(exp3_results, exp2_results)


if __name__ == '__main__':
    main()
