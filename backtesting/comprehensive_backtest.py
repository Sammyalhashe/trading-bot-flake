#!/usr/bin/env python3
"""
Comprehensive backtesting suite: test all strategies across multiple timeframes and market conditions.
"""
import subprocess
import pandas as pd
import numpy as np
import argparse
from pathlib import Path
from datetime import datetime
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)


# Define test periods with different market conditions
TEST_PERIODS = {
    "Q1_2024_Bull": {
        "start": "2024-01-01",
        "end": "2024-03-01",
        "description": "Strong Bull Market (BTC $42k → $61k)"
    },
    "Q2_2023_Sideways": {
        "start": "2023-03-01",
        "end": "2023-06-01",
        "description": "Sideways/Choppy Market"
    },
    "2023_Full_Year": {
        "start": "2023-01-01",
        "end": "2024-01-01",
        "description": "Full Year 2023 (Mixed Conditions)"
    },
    "YTD_2025": {
        "start": "2025-01-01",
        "end": None,  # defaults to now
        "description": "Year-to-Date 2025-2026 (Recent Market)"
    },
    "H2_2024": {
        "start": "2024-07-01",
        "end": "2025-01-01",
        "description": "H2 2024 (Mid-Year to EOY)"
    },
}

# Timeframes to test (in seconds)
TIMEFRAMES = {
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

STRATEGIES = ["mean_reversion", "trend_following", "supertrend", "auto"]


def download_data(symbols, period_key, timeframe_key, timeframe_seconds):
    """Download historical data for a specific period and timeframe."""
    period = TEST_PERIODS[period_key]

    logger.info(f"Downloading {period_key} data at {timeframe_key} timeframe...")

    cmd = [
        "./download_historical_data.py",
        "--symbols", *symbols,
        "--start", period["start"],
        "--granularity", str(timeframe_seconds),
        "--output-dir", f"../data/backtest/{period_key}_{timeframe_key}"
    ]

    if period["end"]:
        cmd.extend(["--end", period["end"]])

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to download data: {e.stderr}")
        return False


def run_backtest(symbols, period_key, timeframe_key, strategy, initial_capital):
    """Run backtest for a specific configuration."""
    data_dir = f"../data/backtest/{period_key}_{timeframe_key}"
    output_prefix = f"../data/results/{period_key}_{timeframe_key}_{strategy}"

    logger.info(f"Testing {strategy} on {period_key} ({timeframe_key})...")

    # Create results directory
    Path("../data/results").mkdir(parents=True, exist_ok=True)

    cmd = [
        "./backtest.py",
        "--data-dir", data_dir,
        "--symbols", *symbols,
        "--strategies", strategy,
        "--initial-capital", str(initial_capital),
    ]

    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)

        # Move generated files to results directory and read metrics from equity curve
        equity_file = f"equity_curve_{strategy}.csv"
        trades_file = f"trades_{strategy}.csv"

        if not Path(equity_file).exists():
            logger.error(f"Equity curve file not found: {equity_file}")
            return None

        # Calculate metrics from the CSV files
        metrics = calculate_metrics_from_files(equity_file, trades_file, initial_capital)

        # Add metadata
        metrics["period"] = period_key
        metrics["timeframe"] = timeframe_key
        metrics["strategy"] = strategy

        # Move files to results directory
        new_equity = f"{output_prefix}_equity_curve.csv"
        new_trades = f"{output_prefix}_trades.csv"
        Path(equity_file).rename(new_equity)
        if Path(trades_file).exists():
            Path(trades_file).rename(new_trades)

        return metrics

    except subprocess.CalledProcessError as e:
        logger.error(f"Backtest failed: {e.stderr}")
        return None
    except Exception as e:
        logger.error(f"Error processing results: {e}")
        return None


def calculate_metrics_from_files(equity_file, trades_file, initial_capital):
    """Calculate metrics from generated CSV files."""
    metrics = {
        "total_return_pct": 0,
        "max_drawdown_pct": 0,
        "sharpe_ratio": 0,
        "num_trades": 0,
        "win_rate_pct": 0,
        "avg_win_pct": 0,
        "avg_loss_pct": 0,
        "profit_factor": 0,
    }

    # Read equity curve
    equity_df = pd.read_csv(equity_file)

    if len(equity_df) == 0:
        return metrics

    # Calculate return
    final_value = equity_df['portfolio_value'].iloc[-1]
    metrics["total_return_pct"] = (final_value / initial_capital - 1) * 100

    # Calculate max drawdown
    equity_df['peak'] = equity_df['portfolio_value'].cummax()
    equity_df['drawdown'] = (equity_df['portfolio_value'] / equity_df['peak'] - 1) * 100
    metrics["max_drawdown_pct"] = equity_df['drawdown'].min()

    # Calculate Sharpe ratio
    equity_df['returns'] = equity_df['portfolio_value'].pct_change()
    returns_std = equity_df['returns'].std()
    returns_mean = equity_df['returns'].mean()
    if returns_std > 0:
        metrics["sharpe_ratio"] = (returns_mean / returns_std * np.sqrt(365 * 24))
    else:
        metrics["sharpe_ratio"] = 0

    # Read trades if available
    if Path(trades_file).exists():
        trades_df = pd.read_csv(trades_file)
        sell_trades = trades_df[trades_df['type'] == 'SELL']

        if len(sell_trades) > 0:
            metrics["num_trades"] = len(sell_trades)

            # Win/loss statistics
            wins = sell_trades[sell_trades['pnl'] > 0]
            losses = sell_trades[sell_trades['pnl'] < 0]

            if len(sell_trades) > 0:
                metrics["win_rate_pct"] = len(wins) / len(sell_trades) * 100

            if len(wins) > 0:
                metrics["avg_win_pct"] = wins['pnl_pct'].mean()

            if len(losses) > 0:
                metrics["avg_loss_pct"] = losses['pnl_pct'].mean()

            # Profit factor
            if len(losses) > 0 and losses['pnl'].sum() != 0:
                metrics["profit_factor"] = abs(wins['pnl'].sum() / losses['pnl'].sum())
            elif len(wins) > 0:
                metrics["profit_factor"] = 999.0  # Only wins
            else:
                metrics["profit_factor"] = 0

    return metrics


def generate_report(all_results):
    """Generate comprehensive report from all test results."""
    df = pd.DataFrame(all_results)

    # Save raw results
    df.to_csv("../data/results/comprehensive_results.csv", index=False)
    logger.info("Saved raw results to ../data/results/comprehensive_results.csv")

    # Generate summary reports
    print("\n" + "="*100)
    print("COMPREHENSIVE BACKTEST RESULTS")
    print("="*100)

    # 1. Best strategy per market condition
    print("\n📊 BEST STRATEGY BY MARKET CONDITION")
    print("-"*100)
    for period in df['period'].unique():
        period_data = df[df['period'] == period]
        best = period_data.loc[period_data['total_return_pct'].idxmax()]
        print(f"\n{period} ({TEST_PERIODS[period]['description']})")
        print(f"  Winner: {best['strategy']} @ {best['timeframe']}")
        print(f"  Return: {best['total_return_pct']:+.2f}% | Sharpe: {best['sharpe_ratio']:.2f} | "
              f"MaxDD: {best['max_drawdown_pct']:.2f}% | Win Rate: {best['win_rate_pct']:.1f}%")

    # 2. Best timeframe per strategy
    print("\n\n📈 BEST TIMEFRAME BY STRATEGY")
    print("-"*100)
    for strategy in STRATEGIES:
        strategy_data = df[df['strategy'] == strategy]
        best = strategy_data.loc[strategy_data['total_return_pct'].idxmax()]
        worst = strategy_data.loc[strategy_data['total_return_pct'].idxmin()]

        print(f"\n{strategy.upper()}")
        print(f"  Best:  {best['timeframe']} in {best['period']} → {best['total_return_pct']:+.2f}% "
              f"(Sharpe: {best['sharpe_ratio']:.2f})")
        print(f"  Worst: {worst['timeframe']} in {worst['period']} → {worst['total_return_pct']:+.2f}% "
              f"(Sharpe: {worst['sharpe_ratio']:.2f})")

    # 3. Overall best configurations
    print("\n\n🏆 TOP 10 CONFIGURATIONS (by Return)")
    print("-"*100)
    top10 = df.nlargest(10, 'total_return_pct')
    print(f"{'Rank':<5} {'Strategy':<18} {'Timeframe':<10} {'Period':<18} {'Return':<10} {'Sharpe':<8} {'MaxDD':<8}")
    print("-"*100)
    for idx, (i, row) in enumerate(top10.iterrows(), 1):
        print(f"{idx:<5} {row['strategy']:<18} {row['timeframe']:<10} {row['period']:<18} "
              f"{row['total_return_pct']:>+8.2f}% {row['sharpe_ratio']:>7.2f} {row['max_drawdown_pct']:>7.2f}%")

    # 4. Risk-adjusted best (by Sharpe)
    print("\n\n🎯 TOP 10 CONFIGURATIONS (by Risk-Adjusted Return / Sharpe Ratio)")
    print("-"*100)
    top10_sharpe = df.nlargest(10, 'sharpe_ratio')
    print(f"{'Rank':<5} {'Strategy':<18} {'Timeframe':<10} {'Period':<18} {'Sharpe':<8} {'Return':<10} {'MaxDD':<8}")
    print("-"*100)
    for idx, (i, row) in enumerate(top10_sharpe.iterrows(), 1):
        print(f"{idx:<5} {row['strategy']:<18} {row['timeframe']:<10} {row['period']:<18} "
              f"{row['sharpe_ratio']:>7.2f} {row['total_return_pct']:>+8.2f}% {row['max_drawdown_pct']:>7.2f}%")

    # 5. Strategy comparison across all conditions
    print("\n\n📊 AVERAGE PERFORMANCE BY STRATEGY (across all conditions)")
    print("-"*100)
    strategy_avg = df.groupby('strategy').agg({
        'total_return_pct': 'mean',
        'max_drawdown_pct': 'mean',
        'sharpe_ratio': 'mean',
        'win_rate_pct': 'mean',
        'num_trades': 'sum'
    }).round(2)
    print(strategy_avg.to_string())

    # 6. Timeframe comparison
    print("\n\n⏱️  AVERAGE PERFORMANCE BY TIMEFRAME (across all conditions)")
    print("-"*100)
    timeframe_avg = df.groupby('timeframe').agg({
        'total_return_pct': 'mean',
        'max_drawdown_pct': 'mean',
        'sharpe_ratio': 'mean',
        'win_rate_pct': 'mean',
    }).round(2)
    print(timeframe_avg.to_string())

    print("\n" + "="*100)
    print(f"Complete results saved to: ../data/results/comprehensive_results.csv")
    print("="*100 + "\n")


def main():
    parser = argparse.ArgumentParser(description='Comprehensive strategy backtesting suite')
    parser.add_argument('--symbols', nargs='+', default=['BTC-USD'],
                       help='Symbols to test (default: BTC-USD)')
    parser.add_argument('--periods', nargs='+', default=list(TEST_PERIODS.keys()),
                       choices=list(TEST_PERIODS.keys()),
                       help='Test periods to include')
    parser.add_argument('--timeframes', nargs='+', default=list(TIMEFRAMES.keys()),
                       choices=list(TIMEFRAMES.keys()),
                       help='Timeframes to test')
    parser.add_argument('--strategies', nargs='+', default=STRATEGIES,
                       choices=STRATEGIES,
                       help='Strategies to test')
    parser.add_argument('--initial-capital', type=float, default=10000,
                       help='Initial capital (default: 10000)')
    parser.add_argument('--skip-download', action='store_true',
                       help='Skip data download (use existing data)')

    args = parser.parse_args()

    logger.info("="*100)
    logger.info("COMPREHENSIVE BACKTESTING SUITE")
    logger.info("="*100)
    logger.info(f"Symbols: {', '.join(args.symbols)}")
    logger.info(f"Periods: {', '.join(args.periods)}")
    logger.info(f"Timeframes: {', '.join(args.timeframes)}")
    logger.info(f"Strategies: {', '.join(args.strategies)}")
    logger.info(f"Initial Capital: ${args.initial_capital:,.2f}")
    logger.info("="*100 + "\n")

    all_results = []
    total_tests = len(args.periods) * len(args.timeframes) * len(args.strategies)
    current_test = 0

    # Download data for all combinations
    if not args.skip_download:
        logger.info("PHASE 1: Downloading data...")
        for period_key in args.periods:
            for timeframe_key in args.timeframes:
                timeframe_seconds = TIMEFRAMES[timeframe_key]
                download_data(args.symbols, period_key, timeframe_key, timeframe_seconds)
        logger.info("Data download complete!\n")

    # Run backtests for all combinations
    logger.info("PHASE 2: Running backtests...")
    for period_key in args.periods:
        for timeframe_key in args.timeframes:
            for strategy in args.strategies:
                current_test += 1
                logger.info(f"Progress: {current_test}/{total_tests}")

                metrics = run_backtest(
                    args.symbols,
                    period_key,
                    timeframe_key,
                    strategy,
                    args.initial_capital
                )

                if metrics:
                    all_results.append(metrics)

    logger.info(f"\nCompleted {len(all_results)} backtests successfully!")

    # Generate comprehensive report
    if all_results:
        generate_report(all_results)
    else:
        logger.error("No successful backtests to report!")


if __name__ == '__main__':
    main()
