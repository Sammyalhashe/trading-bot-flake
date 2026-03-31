# Data Directory

This directory contains downloaded market data and backtest results. All files here are gitignored.

## Structure

```
data/
├── historical/          # Downloaded OHLCV data from Coinbase
├── backtest/           # Organized backtest data by period/timeframe
└── results/            # Backtest results (equity curves, trades, reports)
```

## Usage

Data is automatically downloaded and organized when you run backtesting scripts from the `backtesting/` directory.

See `docs/BACKTESTING.md` for more information.
