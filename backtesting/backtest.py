#!/usr/bin/env python3
"""
Backtest trading strategies against historical data.

Tests mean_reversion, trend_following, and auto (dynamic) strategies.
"""
import os
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


CANDLE_LOOKBACK = 300  # indicators need at most ~55 candles; avoids copying full history each bar


class HistoricalDataProvider:
    """Serves the current bar's candle slices through the live data-provider interface."""

    def __init__(self, dfs: Dict[str, pd.DataFrame]):
        self.dfs = dfs

    def get_market_data(self, product_id, window, granularity="1h"):
        df = self.dfs.get(product_id.split('-')[0])
        if df is None:
            return None
        return df.tail(window + 10).rename(columns={'timestamp': 'start'})


class BacktestEngine:
    """Backtesting engine for trading strategies."""

    EXECUTOR_ID = "backtest"

    def __init__(self, config: TradingConfig, initial_capital: float = 10000):
        self.config = config
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.positions = {}  # asset -> {'size': float, 'entry': float, 'hwm': float, 'tp_flags': dict}
        self.trades = []
        self.equity_curve = []
        self.total_fees = 0  # Track total fees paid
        self.stopout_times = {}  # asset -> timestamp of last stop-loss exit
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
        self.current_time = None
        self.daily = {}  # asset -> daily candles, for strategies that need "1d"
        self.risk_manager = RiskManager(config, initial_capital,
                                        clock=lambda: self.current_time.timestamp())

    def load_data(self, csv_path: str) -> pd.DataFrame:
        """Load historical OHLCV data from CSV."""
        df = pd.read_csv(csv_path)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('timestamp').reset_index(drop=True)
        return df

    def get_portfolio_value(self, prices: Dict[str, float]) -> float:
        """Calculate total portfolio value."""
        total = self.capital
        for asset, pos in self.positions.items():
            if asset in prices:
                total += pos['size'] * prices[asset]
        return total

    def run_backtest(self, data_files: List[str], strategy_name: str = "auto",
                     start=None, end=None) -> Dict:
        """
        Run backtest on historical data.

        Args:
            data_files: List of CSV file paths
            strategy_name: 'mean_reversion', 'trend_following', or 'auto'
            start, end: optional inclusive timestamp bounds (anything pd.Timestamp accepts)
        """
        logger.info(f"Starting backtest with strategy: {strategy_name}")
        logger.info(f"Initial capital: ${self.initial_capital:,.2f}")

        # Load all data files; rows before `start` are kept as indicator warmup.
        datasets = {}
        for file_path in data_files:
            path = Path(file_path)
            asset = path.stem.split('_')[0]  # BTC_USD_1h_full -> BTC
            df = self.load_data(file_path)
            if end is not None:
                df = df[df['timestamp'] <= pd.Timestamp(end)]
            datasets[asset] = df.reset_index(drop=True)
            logger.info(f"Loaded {len(datasets[asset])} candles for {asset}")

        if not datasets:
            logger.error("No data loaded")
            return {}

        required = ["BTC", "ETH"] if self.config.enable_dual_regime else ["BTC"]
        missing = [a for a in required if a not in datasets]
        if missing:
            raise ValueError(f"Live regime detection needs {', '.join(missing)} candles in the data files")

        # Create strategy instances
        strategies = {
            "trend_following": create_strategy("trend_following", self.ta, self.config),
            "mean_reversion": create_strategy("mean_reversion", self.ta, self.config),
            "supertrend": create_strategy("supertrend", self.ta, self.config),
            "mtf_trend": create_strategy("mtf_trend", self.ta, self.config),
        }
        if strategy_name != "auto" and strategy_name not in strategies:
            strategies[strategy_name] = create_strategy(strategy_name, self.ta, self.config)
        if any("1d" in s.required_timeframes for s in strategies.values() if strategy_name in ("auto", s.name)):
            self.daily = {asset: self._load_daily(path, datasets[asset])
                          for asset, path in zip(datasets, data_files)}

        # Iterate on BTC's timeline, which also drives the regime as in live trading
        clock_df = datasets["BTC"]
        first_idx = max(self.config.ma_long_window, 50)
        if start is not None:
            first_idx = max(first_idx, int(clock_df['timestamp'].searchsorted(pd.Timestamp(start), side='left')))

        current_prices = {}
        for idx in range(first_idx, len(clock_df)):
            timestamp = clock_df.iloc[idx]['timestamp']
            self.current_time = timestamp

            current_prices = {}
            current_dfs = {}
            for asset, df in datasets.items():
                bar_end = df['timestamp'].searchsorted(timestamp, side='right')
                asset_df = df.iloc[max(0, bar_end - CANDLE_LOOKBACK):bar_end]
                if len(asset_df) > 0:
                    current_prices[asset] = asset_df.iloc[-1]['close']
                    current_dfs[asset] = asset_df

            # Live confirms regime changes over 3 scans (~6 min), a no-op at hourly resolution.
            full_regime, market_regime = self.regime_detector.get_current_regime(
                HistoricalDataProvider(current_dfs), self.config.enable_dual_regime)

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

            # Drawdown guard is checked on the pre-trade value, exits always run (as in live)
            trading_allowed, _ = self.risk_manager.check_circuit_breakers(
                self.get_portfolio_value(current_prices), self.EXECUTOR_ID)

            self._check_exits(current_strategy, current_prices, current_dfs, timestamp)

            if trading_allowed:
                self._check_entries(current_strategy, current_prices, current_dfs,
                                    market_regime, full_regime, timestamp)

            portfolio_value = self.get_portfolio_value(current_prices)
            self.equity_curve.append({
                'timestamp': timestamp,
                'portfolio_value': portfolio_value,
                'cash': self.capital,
                'regime': full_regime
            })

        # Close all remaining positions at final prices
        final_timestamp = clock_df.iloc[-1]['timestamp']
        for asset in list(self.positions.keys()):
            if asset in current_prices:
                self._close_position(asset, current_prices[asset], 1.0,
                                   "Backtest end", final_timestamp)

        # Calculate performance metrics
        results = self._calculate_metrics(strategy_name)
        return results

    def _load_daily(self, hourly_path, hourly_df) -> pd.DataFrame:
        """Daily candles: the sibling 1d file when present (longer warm-up), else resampled hourly bars."""
        daily_path = Path(hourly_path).parent.parent / "1d" / Path(hourly_path).name.replace("_1h_", "_1d_")
        if daily_path.exists():
            return self.load_data(str(daily_path))
        return (hourly_df.set_index("timestamp")
                .resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
                .dropna().reset_index())

    def _daily_slice(self, asset, timestamp) -> pd.DataFrame:
        """Daily candles closed by the end of the hourly bar starting at `timestamp` (no look-ahead)."""
        days = self.daily.get(asset)
        if days is None:
            return None
        # A day starting at D closes at D+24h; the bar starting at t closes at t+1h.
        end = days["timestamp"].searchsorted(timestamp - pd.Timedelta(hours=23), side="right")
        return days.iloc[max(0, end - 400):end]

    def _build_market_data(self, strategy, df, asset=None, timestamp=None):
        """Build market_data dict from a single DataFrame for the strategy.

        For single-TF strategies: {"1h": df}
        For MTF strategies needing 15m: {"1h": resampled_1h, "15m": df}
        For daily strategies: {"1d": closed daily candles}
        """
        if "1d" in strategy.required_timeframes:
            return {"1d": self._daily_slice(asset, timestamp), "1h": df}
        if "15m" in strategy.required_timeframes and "1h" in strategy.required_timeframes:
            # Data is at 15m granularity; resample up to 1h for the "1h" key
            df_indexed = df.set_index("timestamp") if "timestamp" in df.columns else df.set_index("start")
            df_1h = df_indexed.resample("1h").agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum",
            }).dropna().reset_index()
            # Rename back to 'start' if needed for TA compatibility
            if "timestamp" in df_1h.columns:
                df_1h = df_1h.rename(columns={"timestamp": "start"})
            return {"1h": df_1h, "15m": df}
        return {"1h": df}

    def _check_exits(self, strategy, prices: Dict[str, float],
                    dfs: Dict[str, pd.DataFrame], timestamp):
        """Check exit conditions for all positions."""
        for asset in list(self.positions.keys()):
            if asset not in prices or asset not in dfs:
                continue

            pos = self.positions[asset]
            price = prices[asset]
            df = dfs[asset]
            bar = df.iloc[-1]

            if self._stopped_out_intrabar(strategy, asset, pos, df, timestamp):
                continue

            # Live polls every ~2 min, so its HWM sees intrabar highs
            pos['hwm'] = max(pos['hwm'], bar['high'], price)

            # Check exit conditions
            market_data = self._build_market_data(strategy, df, asset, timestamp)
            should_exit, sell_ratio, reason, new_tp_flags = strategy.check_exit(
                asset=asset.split('-')[0],  # BTC-USD -> BTC
                product_id=asset,
                market_data=market_data,
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

    def _stopped_out_intrabar(self, strategy, asset, pos, df, timestamp) -> bool:
        """Fill the trailing stop at its price when the bar trades through it, as live polling would."""
        if not hasattr(strategy, "trailing_stop_price") or len(df) < 2:
            return False
        before_bar = df.iloc[:-1]
        bar = df.iloc[-1]
        stop_price, stop_pct = strategy.trailing_stop_price(
            before_bar, before_bar['close'].iloc[-1], pos['entry'], pos['hwm'], pos['tp_flags'])
        if bar['low'] >= stop_price:
            return False
        fill = min(bar['open'], stop_price)
        reason = (f"Trailing stop-loss triggered for {asset} (price ${fill:,.2f} < stop ${stop_price:,.2f}, "
                  f"HWM=${pos['hwm']:,.2f}, ATR stop={stop_pct*100:.1f}%)")
        self._close_position(asset, fill, 1.0, reason, timestamp)
        return True

    def _in_stopout_cooldown(self, asset: str, timestamp) -> bool:
        cooldown_hours = float(self.config.stopout_cooldown_hours)
        stopped_at = self.stopout_times.get(asset)
        if cooldown_hours <= 0 or stopped_at is None:
            return False
        return (timestamp - stopped_at).total_seconds() < cooldown_hours * 3600

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

            if self._in_stopout_cooldown(asset, timestamp):
                continue

            market_data = self._build_market_data(strategy, df, asset, timestamp)
            candidate = strategy.scan_entry(
                asset=asset.split('-')[0],
                product_id=asset,
                market_data=market_data,
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
            if (market_regime == "BEAR" and self.config.bear_position_scale < 1.0
                    and getattr(strategy, "uses_hourly_regime", True)):
                target_allocation *= self.config.bear_position_scale
                if target_allocation < 10:
                    continue  # Skip tiny allocations

            # Like live, buy what available cash allows (keeping a 5% buffer) instead of skipping
            fee_rate = self.risk_manager.calculate_fees(1.0, is_round_trip=False)
            cost = min(target_allocation, self.capital * 0.95 / (1 + fee_rate))
            if cost < 10:
                continue
            position_size = cost / price
            buy_fee = self.risk_manager.calculate_fees(cost, is_round_trip=False)
            total_cost = cost + buy_fee

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

        if "stop" in reason.lower():
            self.stopout_times[asset] = timestamp

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
    parser.add_argument('--data-dir', type=str, default=os.environ.get('BACKTEST_DATA_DIR', '../data/historical'),
                       help='Directory containing CSV data files')
    parser.add_argument('--symbols', nargs='+', default=['BTC-USD', 'ETH-USD'],
                       help='Symbols to backtest (default: BTC-USD ETH-USD)')
    parser.add_argument('--strategies', nargs='+',
                       default=['mean_reversion', 'trend_following', 'supertrend', 'mtf_trend', 'auto'],
                       help='Strategies to test: mean_reversion, trend_following, supertrend, mtf_trend, auto')
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
