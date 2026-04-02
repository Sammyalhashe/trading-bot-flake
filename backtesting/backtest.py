#!/usr/bin/env python3
"""
Backtest trading strategies against historical data.

Tests mean_reversion, trend_following, and auto (dynamic) strategies.
"""
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from datetime import datetime
import argparse
import logging
from typing import Dict, List, Tuple
from collections import defaultdict

# Import strategy components
from config import TradingConfig
from core import TechnicalAnalysis, RegimeDetector, RiskManager
from strategies import create_strategy


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class BacktestEngine:
    """Backtesting engine for trading strategies."""

    def __init__(self, config: TradingConfig, initial_capital: float = 10000):
        self.config = config
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.positions = {}  # asset -> {'size': float, 'entry': float, 'hwm': float, 'tp_flags': dict}
        self.trades = []
        self.equity_curve = []
        self.total_fees = 0  # Track total fees paid
        self.ta = TechnicalAnalysis(
            ma_short_window=config.ma_short_window,
            ma_long_window=config.ma_long_window
        )
        self.regime_detector = RegimeDetector(
            technical_analysis=self.ta,
            ma_short_window=config.ma_short_window,
            ma_long_window=config.ma_long_window,
            enable_btc_dominance=False  # Disabled for backtesting
        )
        self.risk_manager = RiskManager(config, initial_capital)

    def load_data(self, csv_path: str) -> pd.DataFrame:
        """Load historical OHLCV data from CSV."""
        df = pd.read_csv(csv_path)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('timestamp').reset_index(drop=True)
        return df

    def detect_regime(self, df: pd.DataFrame) -> Tuple[str, str]:
        """Detect market regime from price data."""
        # Use last N candles for regime detection
        regime_df = df.tail(max(self.config.ma_long_window, 50))

        ma_short, ma_long = self.ta.analyze_trend(regime_df)

        if ma_short is None or ma_long is None:
            return "NEUTRAL", "NEUTRAL"

        # Simple regime classification
        if ma_short > ma_long * 1.05:
            full_regime = "STRONG_BULL"
            market_regime = "BULL"
        elif ma_short > ma_long * 1.002:
            full_regime = "BULL"
            market_regime = "BULL"
        elif ma_short < ma_long * 0.95:
            full_regime = "STRONG_BEAR"
            market_regime = "BEAR"
        elif ma_short < ma_long * 0.998:
            full_regime = "BEAR"
            market_regime = "BEAR"
        else:
            full_regime = "NEUTRAL"
            market_regime = "NEUTRAL"

        return market_regime, full_regime

    def get_portfolio_value(self, prices: Dict[str, float]) -> float:
        """Calculate total portfolio value."""
        total = self.capital
        for asset, pos in self.positions.items():
            if asset in prices:
                total += pos['size'] * prices[asset]
        return total

    def run_backtest(self, data_files: List[str], strategy_name: str = "auto") -> Dict:
        """
        Run backtest on historical data.

        Args:
            data_files: List of CSV file paths
            strategy_name: 'mean_reversion', 'trend_following', or 'auto'
        """
        logger.info(f"Starting backtest with strategy: {strategy_name}")
        logger.info(f"Initial capital: ${self.initial_capital:,.2f}")

        # Load all data files
        datasets = {}
        for file_path in data_files:
            path = Path(file_path)
            asset = path.stem.split('_')[0].replace('_', '-')  # BTC_USD -> BTC-USD
            datasets[asset] = self.load_data(file_path)
            logger.info(f"Loaded {len(datasets[asset])} candles for {asset}")

        if not datasets:
            logger.error("No data loaded")
            return {}

        # Create strategy instances
        strategies = {
            "trend_following": create_strategy("trend_following", self.ta, self.config),
            "mean_reversion": create_strategy("mean_reversion", self.ta, self.config),
            "supertrend": create_strategy("supertrend", self.ta, self.config),
        }

        # Get the primary dataset for time iteration (use first asset)
        primary_asset = list(datasets.keys())[0]
        primary_df = datasets[primary_asset]

        # Ensure we have enough data for indicators
        start_idx = max(self.config.ma_long_window, 50)

        # Iterate through time
        for idx in range(start_idx, len(primary_df)):
            timestamp = primary_df.iloc[idx]['timestamp']

            # Get current prices for all assets
            current_prices = {}
            current_dfs = {}

            for asset, df in datasets.items():
                # Find matching timestamp (or closest)
                asset_df = df[df['timestamp'] <= timestamp]
                if len(asset_df) > 0:
                    current_prices[asset] = asset_df.iloc[-1]['close']
                    current_dfs[asset] = asset_df

            # Detect regime using primary asset
            if primary_asset in current_dfs:
                market_regime, full_regime = self.detect_regime(current_dfs[primary_asset])
            else:
                market_regime, full_regime = "NEUTRAL", "NEUTRAL"

            # Select strategy for auto mode
            if strategy_name == "auto":
                if full_regime in ("STRONG_BULL", "BULL"):
                    current_strategy = strategies["trend_following"]
                elif full_regime == "NEUTRAL":
                    current_strategy = strategies["mean_reversion"]
                else:  # BEAR, STRONG_BEAR
                    current_strategy = strategies["trend_following"]
            else:
                current_strategy = strategies[strategy_name]

            # Check exits first
            self._check_exits(current_strategy, current_prices, current_dfs, timestamp)

            # Check entries
            self._check_entries(current_strategy, current_prices, current_dfs,
                              market_regime, full_regime, timestamp)

            # Record equity
            portfolio_value = self.get_portfolio_value(current_prices)
            self.equity_curve.append({
                'timestamp': timestamp,
                'portfolio_value': portfolio_value,
                'cash': self.capital,
                'regime': full_regime
            })

        # Close all remaining positions at final prices
        final_timestamp = primary_df.iloc[-1]['timestamp']
        for asset in list(self.positions.keys()):
            if asset in current_prices:
                self._close_position(asset, current_prices[asset], 1.0,
                                   "Backtest end", final_timestamp)

        # Calculate performance metrics
        results = self._calculate_metrics(strategy_name)
        return results

    def _check_exits(self, strategy, prices: Dict[str, float],
                    dfs: Dict[str, pd.DataFrame], timestamp):
        """Check exit conditions for all positions."""
        for asset in list(self.positions.keys()):
            if asset not in prices or asset not in dfs:
                continue

            pos = self.positions[asset]
            price = prices[asset]
            df = dfs[asset]

            # Update high-water mark
            if price > pos['hwm']:
                pos['hwm'] = price

            # Check exit conditions
            should_exit, sell_ratio, reason, new_tp_flags = strategy.check_exit(
                asset=asset.split('-')[0],  # BTC-USD -> BTC
                product_id=asset,
                df=df,
                price=price,
                entry=pos['entry'],
                hwm=pos['hwm'],
                tp_flags=pos['tp_flags'],
                state={'entry_timestamps': {asset: timestamp.timestamp()}},
                entry_key=asset
            )

            if should_exit:
                pos['tp_flags'] = new_tp_flags
                self._close_position(asset, price, sell_ratio, reason, timestamp)

    def _check_entries(self, strategy, prices: Dict[str, float],
                      dfs: Dict[str, pd.DataFrame], market_regime: str,
                      full_regime: str, timestamp):
        """Check entry conditions for all assets."""
        # Limit maximum positions
        max_concurrent_positions = self.config.max_concurrent_positions
        if len(self.positions) >= max_concurrent_positions:
            return

        # Skip regime check
        if strategy.should_skip_regime(market_regime, full_regime):
            return

        # Scan for entry candidates
        candidates = []
        for asset, df in dfs.items():
            if asset in self.positions:
                continue  # Already have position

            if asset not in prices:
                continue

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

        # Rank and select top candidates
        ranked = strategy.rank_candidates(candidates)
        positions_to_open = max_concurrent_positions - len(self.positions)

        for candidate in ranked[:positions_to_open]:
            asset = candidate['product_id']
            price = prices[asset]

            # Position sizing: equal allocation
            portfolio_value = self.get_portfolio_value(prices)
            target_allocation = portfolio_value / max_concurrent_positions

            # Scale down in BEAR regime
            if market_regime == "BEAR" and self.config.bear_position_scale < 1.0:
                target_allocation *= self.config.bear_position_scale
                if target_allocation < 10:
                    continue  # Skip tiny allocations

            position_size = target_allocation / price
            cost = position_size * price

            # Calculate buy fee (half of round-trip)
            buy_fee = self.risk_manager.calculate_fees(cost, is_round_trip=False)
            total_cost = cost + buy_fee

            # Check if we have enough capital
            if total_cost > self.capital * 0.95:  # Leave 5% buffer
                continue

            # Open position (deduct cost + fee)
            self.capital -= total_cost
            self.total_fees += buy_fee
            self.positions[asset] = {
                'size': position_size,
                'entry': price,
                'hwm': price,
                'tp_flags': {}
            }

            self.trades.append({
                'timestamp': timestamp,
                'asset': asset,
                'type': 'BUY',
                'price': price,
                'size': position_size,
                'cost': cost,
                'fee': buy_fee
            })

            logger.info(f"{timestamp} | BUY {asset} @ ${price:,.2f} | Size: {position_size:.6f} | "
                       f"Cost: ${cost:,.2f} | Fee: ${buy_fee:,.2f}")

    def _close_position(self, asset: str, price: float, ratio: float,
                       reason: str, timestamp):
        """Close a position (full or partial)."""
        if asset not in self.positions:
            return

        pos = self.positions[asset]
        sell_size = pos['size'] * ratio
        proceeds = sell_size * price

        # Calculate sell fee (half of round-trip)
        sell_fee = self.risk_manager.calculate_fees(proceeds, is_round_trip=False)
        net_proceeds = proceeds - sell_fee

        self.capital += net_proceeds
        self.total_fees += sell_fee
        pos['size'] -= sell_size

        # Calculate P&L (including fees)
        cost_basis = pos['entry'] * sell_size
        pnl = net_proceeds - cost_basis
        pnl_pct = (price / pos['entry'] - 1) * 100

        self.trades.append({
            'timestamp': timestamp,
            'asset': asset,
            'type': 'SELL',
            'price': price,
            'size': sell_size,
            'proceeds': proceeds,
            'fee': sell_fee,
            'net_proceeds': net_proceeds,
            'pnl': pnl,
            'pnl_pct': pnl_pct,
            'reason': reason
        })

        logger.info(f"{timestamp} | SELL {asset} @ ${price:,.2f} | Size: {sell_size:.6f} | "
                   f"Proceeds: ${proceeds:,.2f} | Fee: ${sell_fee:,.2f} | "
                   f"Net P&L: ${pnl:+,.2f} ({pnl_pct:+.2f}%) | {reason}")

        # Remove position if fully closed
        if pos['size'] < 1e-8:
            del self.positions[asset]

    def _calculate_metrics(self, strategy_name: str) -> Dict:
        """Calculate performance metrics."""
        if not self.equity_curve:
            return {}

        equity_df = pd.DataFrame(self.equity_curve)
        trades_df = pd.DataFrame(self.trades) if self.trades else pd.DataFrame()

        final_value = equity_df['portfolio_value'].iloc[-1]
        total_return = (final_value / self.initial_capital - 1) * 100

        # Calculate drawdown
        equity_df['peak'] = equity_df['portfolio_value'].cummax()
        equity_df['drawdown'] = (equity_df['portfolio_value'] / equity_df['peak'] - 1) * 100
        max_drawdown = equity_df['drawdown'].min()

        # Trade statistics
        if len(trades_df) > 0:
            sell_trades = trades_df[trades_df['type'] == 'SELL']
            if len(sell_trades) > 0:
                wins = sell_trades[sell_trades['pnl'] > 0]
                losses = sell_trades[sell_trades['pnl'] < 0]

                num_trades = len(sell_trades)
                win_rate = len(wins) / num_trades * 100 if num_trades > 0 else 0
                avg_win = wins['pnl_pct'].mean() if len(wins) > 0 else 0
                avg_loss = losses['pnl_pct'].mean() if len(losses) > 0 else 0
                profit_factor = abs(wins['pnl'].sum() / losses['pnl'].sum()) if len(losses) > 0 and losses['pnl'].sum() != 0 else float('inf')
            else:
                num_trades = win_rate = avg_win = avg_loss = profit_factor = 0
        else:
            num_trades = win_rate = avg_win = avg_loss = profit_factor = 0

        # Sharpe ratio (annualized, assuming hourly data)
        equity_df['returns'] = equity_df['portfolio_value'].pct_change()
        returns_std = equity_df['returns'].std()
        returns_mean = equity_df['returns'].mean()
        sharpe = (returns_mean / returns_std * np.sqrt(365 * 24)) if returns_std > 0 else 0

        results = {
            'strategy': strategy_name,
            'initial_capital': self.initial_capital,
            'final_value': final_value,
            'total_return_pct': total_return,
            'max_drawdown_pct': max_drawdown,
            'total_fees': self.total_fees,
            'fees_pct_of_capital': (self.total_fees / self.initial_capital) * 100,
            'num_trades': num_trades,
            'win_rate_pct': win_rate,
            'avg_win_pct': avg_win,
            'avg_loss_pct': avg_loss,
            'profit_factor': profit_factor,
            'sharpe_ratio': sharpe,
            'equity_curve': equity_df,
            'trades': trades_df
        }

        return results


def print_results(results: Dict):
    """Print backtest results."""
    print("\n" + "="*80)
    print(f"BACKTEST RESULTS - {results['strategy'].upper()}")
    print("="*80)
    print(f"Initial Capital:     ${results['initial_capital']:,.2f}")
    print(f"Final Value:         ${results['final_value']:,.2f}")
    print(f"Total Return:        {results['total_return_pct']:+.2f}%")
    print(f"Max Drawdown:        {results['max_drawdown_pct']:.2f}%")
    print(f"Sharpe Ratio:        {results['sharpe_ratio']:.2f}")
    print(f"\nFees & Costs:")
    print(f"  Total Fees Paid:   ${results['total_fees']:,.2f}")
    print(f"  Fees % of Capital: {results['fees_pct_of_capital']:.2f}%")
    print(f"\nTrade Statistics:")
    print(f"  Total Trades:      {results['num_trades']}")
    print(f"  Win Rate:          {results['win_rate_pct']:.1f}%")
    print(f"  Avg Win:           {results['avg_win_pct']:+.2f}%")
    print(f"  Avg Loss:          {results['avg_loss_pct']:+.2f}%")
    print(f"  Profit Factor:     {results['profit_factor']:.2f}")
    print("="*80 + "\n")


def main():
    parser = argparse.ArgumentParser(description='Backtest trading strategies')
    parser.add_argument('--data-dir', type=str, default='../data/historical',
                       help='Directory containing CSV data files')
    parser.add_argument('--symbols', nargs='+', default=['BTC-USD', 'ETH-USD'],
                       help='Symbols to backtest (default: BTC-USD ETH-USD)')
    parser.add_argument('--strategies', nargs='+',
                       default=['mean_reversion', 'trend_following', 'supertrend', 'auto'],
                       help='Strategies to test: mean_reversion, trend_following, supertrend, auto')
    parser.add_argument('--initial-capital', type=float, default=10000,
                       help='Initial capital in USD (default: 10000)')
    parser.add_argument('--ma-short', type=int, help='Override short MA window')
    parser.add_argument('--ma-long', type=int, help='Override long MA window')
    parser.add_argument('--output', type=str, default='backtest_results.csv',
                       help='Output file for results')

    args = parser.parse_args()

    # Find data files
    data_dir = Path(args.data_dir)
    data_files = []

    for symbol in args.symbols:
        symbol_pattern = symbol.replace('-', '_')
        matching_files = list(data_dir.glob(f"{symbol_pattern}_*.csv"))

        if matching_files:
            # Use the most recent file
            data_files.append(str(matching_files[-1]))
            logger.info(f"Found data file: {matching_files[-1]}")
        else:
            logger.warning(f"No data file found for {symbol}")

    if not data_files:
        logger.error("No data files found. Run download_historical_data.py first.")
        return

    # Load configuration
    config = TradingConfig.from_env()
    if args.ma_short:
        config.ma_short_window = args.ma_short
    if args.ma_long:
        config.ma_long_window = args.ma_long

    # Run backtests for each strategy
    all_results = []

    for strategy_name in args.strategies:
        logger.info(f"\n{'='*80}")
        logger.info(f"Testing strategy: {strategy_name}")
        logger.info(f"{'='*80}\n")

        engine = BacktestEngine(config, initial_capital=args.initial_capital)
        results = engine.run_backtest(data_files, strategy_name)

        if results:
            print_results(results)
            all_results.append(results)

            # Save equity curve
            equity_file = f"equity_curve_{strategy_name}.csv"
            results['equity_curve'].to_csv(equity_file, index=False)
            logger.info(f"Saved equity curve to {equity_file}")

            # Save trades
            if len(results['trades']) > 0:
                trades_file = f"trades_{strategy_name}.csv"
                results['trades'].to_csv(trades_file, index=False)
                logger.info(f"Saved trades to {trades_file}")

    # Compare strategies
    if len(all_results) > 1:
        print("\n" + "="*80)
        print("STRATEGY COMPARISON")
        print("="*80)
        print(f"{'Strategy':<20} {'Return':>12} {'MaxDD':>12} {'Sharpe':>10} {'Trades':>8} {'Win%':>8}")
        print("-"*80)

        for r in all_results:
            print(f"{r['strategy']:<20} {r['total_return_pct']:>11.2f}% "
                  f"{r['max_drawdown_pct']:>11.2f}% {r['sharpe_ratio']:>10.2f} "
                  f"{r['num_trades']:>8} {r['win_rate_pct']:>7.1f}%")

        print("="*80 + "\n")


if __name__ == '__main__':
    main()
