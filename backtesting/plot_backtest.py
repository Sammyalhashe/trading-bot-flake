#!/usr/bin/env python3
"""Plot backtest results with equity curves and trade analysis."""
import pandas as pd
import matplotlib.pyplot as plt
import argparse
from pathlib import Path


def plot_equity_curve(strategy_name):
    """Plot equity curve for a strategy."""
    file_path = f"equity_curve_{strategy_name}.csv"

    if not Path(file_path).exists():
        print(f"Error: {file_path} not found. Run backtest first.")
        return

    df = pd.read_csv(file_path)
    df['timestamp'] = pd.to_datetime(df['timestamp'])

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10))

    # Plot 1: Portfolio Value
    ax1.plot(df['timestamp'], df['portfolio_value'], label='Portfolio Value', linewidth=2)
    ax1.axhline(y=df['portfolio_value'].iloc[0], color='gray', linestyle='--',
                label='Initial Capital', alpha=0.5)
    ax1.fill_between(df['timestamp'], df['portfolio_value'],
                     df['portfolio_value'].iloc[0], alpha=0.2)
    ax1.set_xlabel('Date')
    ax1.set_ylabel('Portfolio Value ($)')
    ax1.set_title(f'Equity Curve - {strategy_name.upper()}')
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    # Calculate drawdown
    df['peak'] = df['portfolio_value'].cummax()
    df['drawdown'] = (df['portfolio_value'] / df['peak'] - 1) * 100

    # Plot 2: Drawdown
    ax2.fill_between(df['timestamp'], df['drawdown'], 0, alpha=0.3, color='red')
    ax2.plot(df['timestamp'], df['drawdown'], color='darkred', linewidth=1.5)
    ax2.set_xlabel('Date')
    ax2.set_ylabel('Drawdown (%)')
    ax2.set_title('Drawdown Over Time')
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    output_file = f"equity_curve_{strategy_name}.png"
    plt.savefig(output_file, dpi=150, bbox_inches='tight')
    print(f"Saved plot to {output_file}")
    plt.close()


def plot_trades(strategy_name):
    """Plot trade distribution and analysis."""
    file_path = f"trades_{strategy_name}.csv"

    if not Path(file_path).exists():
        print(f"Error: {file_path} not found. Run backtest first.")
        return

    df = pd.read_csv(file_path)
    sell_trades = df[df['type'] == 'SELL'].copy()

    if len(sell_trades) == 0:
        print("No completed trades found.")
        return

    fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(14, 10))

    # Plot 1: P&L Distribution
    ax1.hist(sell_trades['pnl'], bins=30, edgecolor='black', alpha=0.7)
    ax1.axvline(x=0, color='red', linestyle='--', linewidth=2)
    ax1.set_xlabel('P&L ($)')
    ax1.set_ylabel('Frequency')
    ax1.set_title('P&L Distribution')
    ax1.grid(True, alpha=0.3)

    # Plot 2: P&L % Distribution
    ax2.hist(sell_trades['pnl_pct'], bins=30, edgecolor='black', alpha=0.7, color='green')
    ax2.axvline(x=0, color='red', linestyle='--', linewidth=2)
    ax2.set_xlabel('P&L (%)')
    ax2.set_ylabel('Frequency')
    ax2.set_title('P&L % Distribution')
    ax2.grid(True, alpha=0.3)

    # Plot 3: Cumulative P&L
    sell_trades = sell_trades.sort_values('timestamp')
    sell_trades['cumulative_pnl'] = sell_trades['pnl'].cumsum()
    sell_trades['timestamp'] = pd.to_datetime(sell_trades['timestamp'])

    ax3.plot(sell_trades['timestamp'], sell_trades['cumulative_pnl'],
             linewidth=2, color='blue')
    ax3.fill_between(sell_trades['timestamp'], sell_trades['cumulative_pnl'], 0,
                     alpha=0.2, color='blue')
    ax3.set_xlabel('Date')
    ax3.set_ylabel('Cumulative P&L ($)')
    ax3.set_title('Cumulative Profit/Loss')
    ax3.grid(True, alpha=0.3)

    # Plot 4: Win/Loss by Asset
    asset_pnl = sell_trades.groupby('asset')['pnl'].agg(['sum', 'count'])
    asset_pnl = asset_pnl.sort_values('sum', ascending=False)

    colors = ['green' if x > 0 else 'red' for x in asset_pnl['sum']]
    ax4.barh(asset_pnl.index, asset_pnl['sum'], color=colors, alpha=0.7)
    ax4.set_xlabel('Total P&L ($)')
    ax4.set_ylabel('Asset')
    ax4.set_title('P&L by Asset')
    ax4.grid(True, alpha=0.3, axis='x')

    plt.tight_layout()
    output_file = f"trades_{strategy_name}.png"
    plt.savefig(output_file, dpi=150, bbox_inches='tight')
    print(f"Saved plot to {output_file}")
    plt.close()


def compare_strategies(strategies):
    """Compare multiple strategies side by side."""
    equity_data = {}

    for strategy in strategies:
        file_path = f"equity_curve_{strategy}.csv"
        if Path(file_path).exists():
            df = pd.read_csv(file_path)
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            equity_data[strategy] = df

    if not equity_data:
        print("No equity curve files found. Run backtest first.")
        return

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10))

    # Plot 1: Portfolio Value Comparison
    for strategy, df in equity_data.items():
        initial_value = df['portfolio_value'].iloc[0]
        normalized = (df['portfolio_value'] / initial_value - 1) * 100
        ax1.plot(df['timestamp'], normalized, label=strategy.upper(), linewidth=2)

    ax1.axhline(y=0, color='gray', linestyle='--', alpha=0.5)
    ax1.set_xlabel('Date')
    ax1.set_ylabel('Return (%)')
    ax1.set_title('Strategy Performance Comparison')
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    # Plot 2: Drawdown Comparison
    for strategy, df in equity_data.items():
        df['peak'] = df['portfolio_value'].cummax()
        df['drawdown'] = (df['portfolio_value'] / df['peak'] - 1) * 100
        ax2.plot(df['timestamp'], df['drawdown'], label=strategy.upper(), linewidth=2)

    ax2.set_xlabel('Date')
    ax2.set_ylabel('Drawdown (%)')
    ax2.set_title('Drawdown Comparison')
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    output_file = "strategy_comparison.png"
    plt.savefig(output_file, dpi=150, bbox_inches='tight')
    print(f"Saved comparison plot to {output_file}")
    plt.close()


def main():
    parser = argparse.ArgumentParser(description='Plot backtest results')
    parser.add_argument('--strategies', nargs='+',
                       default=['mean_reversion', 'trend_following', 'auto'],
                       help='Strategies to plot')
    parser.add_argument('--compare', action='store_true',
                       help='Generate comparison plot')

    args = parser.parse_args()

    print("Generating backtest visualizations...")
    print()

    for strategy in args.strategies:
        print(f"Plotting {strategy}...")
        plot_equity_curve(strategy)
        plot_trades(strategy)
        print()

    if args.compare and len(args.strategies) > 1:
        print("Creating strategy comparison...")
        compare_strategies(args.strategies)
        print()

    print("Done! Check the generated PNG files.")


if __name__ == '__main__':
    main()
