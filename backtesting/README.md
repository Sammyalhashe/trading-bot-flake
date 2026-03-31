# Backtesting Suite

Tools for backtesting trading strategies against historical data.

## Quick Start

```bash
cd backtesting

# Download historical data
./download_historical_data.py --symbols BTC-USD ETH-USD --start 2024-01-01

# Run backtest
./backtest.py --symbols BTC-USD ETH-USD

# Generate plots
./plot_backtest.py --compare
```

## Scripts

- **`download_historical_data.py`** - Download OHLCV data from Coinbase
- **`backtest.py`** - Main backtesting engine
- **`comprehensive_backtest.py`** - Test all strategies × timeframes × periods
- **`plot_backtest.py`** - Generate charts and visualizations
- **`run_backtest.sh`** - One-command runner (download + backtest)

## Documentation

See `docs/BACKTESTING.md` for detailed usage instructions.

## Data Storage

- Downloaded data: `../data/historical/`
- Backtest results: `../data/results/`
- Organized test data: `../data/backtest/`
