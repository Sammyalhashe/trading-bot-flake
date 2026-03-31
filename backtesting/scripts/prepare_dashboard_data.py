#!/usr/bin/env python3
"""
Prepare backtest data for interactive dashboard.

Converts CSV files to optimized JSON format:
- Decimates equity curves to ~5000 points
- Generates manifest.json index
- Calculates summary metrics
- Reduces file size by 40-60%
"""
import json
import pandas as pd
from pathlib import Path
import sys

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def decimate_timeseries(df, target_points=5000):
    """Reduce timeseries to target number of points using downsampling."""
    if len(df) <= target_points:
        return df

    step = len(df) // target_points
    return df.iloc[::step].copy()


def prepare_equity_curve(csv_path, output_path):
    """Convert equity curve CSV to optimized JSON."""
    df = pd.read_csv(csv_path)

    # Decimate to 5000 points
    df_decimated = decimate_timeseries(df, target_points=5000)

    # Convert to JSON-friendly format
    data = {
        'timestamps': df_decimated['timestamp'].tolist(),
        'portfolio_value': df_decimated['portfolio_value'].tolist(),
        'cash': df_decimated['cash'].tolist()
    }

    if 'regime' in df_decimated.columns:
        data['regime'] = df_decimated['regime'].tolist()

    with open(output_path, 'w') as f:
        json.dump(data, f)

    return len(df), len(df_decimated)


def prepare_trades(csv_path, output_path):
    """Convert trades CSV to JSON."""
    try:
        df = pd.read_csv(csv_path)

        # Only include sell trades for visualization
        sell_trades = df[df['type'] == 'SELL'].copy()

        trades = []
        for _, row in sell_trades.iterrows():
            trades.append({
                'timestamp': row['timestamp'],
                'asset': row['asset'],
                'price': float(row['price']),
                'size': float(row['size']),
                'pnl': float(row['pnl']),
                'pnl_pct': float(row['pnl_pct']),
                'reason': row['reason']
            })

        with open(output_path, 'w') as f:
            json.dump(trades, f)

        return len(trades)
    except Exception as e:
        print(f"Warning: Could not process trades from {csv_path}: {e}")
        # Create empty trades file
        with open(output_path, 'w') as f:
            json.dump([], f)
        return 0


def calculate_metrics(equity_csv, trades_csv, initial_capital=10000):
    """Calculate performance metrics from CSV files."""
    equity_df = pd.read_csv(equity_csv)

    metrics = {}

    # Return metrics
    final_value = equity_df['portfolio_value'].iloc[-1]
    metrics['initial_capital'] = initial_capital
    metrics['final_value'] = float(final_value)
    metrics['total_return_pct'] = float((final_value / initial_capital - 1) * 100)

    # Drawdown
    equity_df['peak'] = equity_df['portfolio_value'].cummax()
    equity_df['drawdown'] = (equity_df['portfolio_value'] / equity_df['peak'] - 1) * 100
    metrics['max_drawdown_pct'] = float(equity_df['drawdown'].min())

    # Sharpe ratio (annualized, assuming hourly data)
    equity_df['returns'] = equity_df['portfolio_value'].pct_change()
    returns_std = equity_df['returns'].std()
    returns_mean = equity_df['returns'].mean()
    if returns_std > 0:
        metrics['sharpe_ratio'] = float(returns_mean / returns_std * (365 * 24) ** 0.5)
    else:
        metrics['sharpe_ratio'] = 0.0

    # Trade metrics
    try:
        trades_df = pd.read_csv(trades_csv)
        sell_trades = trades_df[trades_df['type'] == 'SELL']

        if len(sell_trades) > 0:
            wins = sell_trades[sell_trades['pnl'] > 0]
            losses = sell_trades[sell_trades['pnl'] < 0]

            metrics['num_trades'] = int(len(sell_trades))
            metrics['win_rate_pct'] = float(len(wins) / len(sell_trades) * 100)
            metrics['avg_win_pct'] = float(wins['pnl_pct'].mean()) if len(wins) > 0 else 0.0
            metrics['avg_loss_pct'] = float(losses['pnl_pct'].mean()) if len(losses) > 0 else 0.0

            if len(losses) > 0 and losses['pnl'].sum() != 0:
                metrics['profit_factor'] = float(abs(wins['pnl'].sum() / losses['pnl'].sum()))
            elif len(wins) > 0:
                metrics['profit_factor'] = 999.0
            else:
                metrics['profit_factor'] = 0.0
        else:
            metrics['num_trades'] = 0
            metrics['win_rate_pct'] = 0.0
            metrics['avg_win_pct'] = 0.0
            metrics['avg_loss_pct'] = 0.0
            metrics['profit_factor'] = 0.0
    except:
        metrics['num_trades'] = 0
        metrics['win_rate_pct'] = 0.0
        metrics['avg_win_pct'] = 0.0
        metrics['avg_loss_pct'] = 0.0
        metrics['profit_factor'] = 0.0

    return metrics


def main():
    # Paths
    results_dir = Path(__file__).parent.parent.parent / 'data' / 'results'
    dashboard_dir = Path(__file__).parent.parent.parent / 'dashboard'
    data_dir = dashboard_dir / 'data'

    # Create directories
    data_dir.mkdir(parents=True, exist_ok=True)

    print("Preparing dashboard data...")
    print(f"Input: {results_dir}")
    print(f"Output: {data_dir}")

    # Find all equity curve files
    equity_files = list(results_dir.glob('*_equity_curve.csv'))

    if not equity_files:
        print("ERROR: No equity curve files found!")
        print(f"Make sure backtest results exist in {results_dir}")
        return 1

    print(f"\nFound {len(equity_files)} backtest results")

    manifest = []
    total_original = 0
    total_decimated = 0

    for equity_file in sorted(equity_files):
        # Parse filename: period_timeframe_strategy_equity_curve.csv
        basename = equity_file.stem.replace('_equity_curve', '')
        parts = basename.split('_')

        # Handle multi-word periods like "2023_Full_Year"
        if len(parts) >= 3:
            # Last two are always timeframe and strategy
            strategy = parts[-1]
            timeframe = parts[-2]
            period = '_'.join(parts[:-2])
        else:
            print(f"Warning: Could not parse {equity_file.name}, skipping")
            continue

        print(f"\nProcessing: {period} / {timeframe} / {strategy}")

        # Output paths
        equity_json = data_dir / f"{basename}_equity.json"
        trades_json = data_dir / f"{basename}_trades.json"

        # Convert equity curve
        orig_points, dec_points = prepare_equity_curve(equity_file, equity_json)
        total_original += orig_points
        total_decimated += dec_points
        print(f"  Equity: {orig_points:,} → {dec_points:,} points ({dec_points/orig_points*100:.1f}%)")

        # Convert trades
        trades_file = results_dir / f"{basename}_trades.csv"
        num_trades = 0
        if trades_file.exists():
            num_trades = prepare_trades(trades_file, trades_json)
            print(f"  Trades: {num_trades}")
        else:
            # Create empty trades file
            with open(trades_json, 'w') as f:
                json.dump([], f)
            print(f"  Trades: none")

        # Calculate metrics
        metrics = calculate_metrics(equity_file, trades_file if trades_file.exists() else None)

        # Add to manifest
        manifest.append({
            'id': basename,
            'period': period,
            'timeframe': timeframe,
            'strategy': strategy,
            'metrics': metrics,
            'files': {
                'equity': f"data/{equity_json.name}",
                'trades': f"data/{trades_json.name}"
            }
        })

    # Write manifest
    manifest_path = data_dir / 'manifest.json'
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)

    print(f"\n{'='*60}")
    print(f"✓ Dashboard data prepared successfully!")
    print(f"{'='*60}")
    print(f"Results processed: {len(manifest)}")
    print(f"Total data points: {total_original:,} → {total_decimated:,} ({total_decimated/total_original*100:.1f}%)")
    print(f"Manifest: {manifest_path}")
    print(f"\nNext steps:")
    print(f"  1. Create dashboard/index.html")
    print(f"  2. Test locally: cd dashboard && python3 -m http.server 8000")
    print(f"  3. Deploy to GitHub Pages")

    return 0


if __name__ == '__main__':
    sys.exit(main())
