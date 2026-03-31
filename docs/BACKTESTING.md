# Backtesting Guide

## Quick Start

### 1. Download Historical Data

Download historical OHLCV data from Coinbase (no API keys required):

```bash
# Download 1 year of hourly data for BTC and ETH
./download_historical_data.py --symbols BTC-USD ETH-USD --start 2024-01-01

# Download more symbols with custom timeframe
./download_historical_data.py \
  --symbols BTC-USD ETH-USD SOL-USD LINK-USD \
  --start 2023-01-01 \
  --end 2024-12-31 \
  --granularity 3600

# Download 5-minute candles for high-frequency testing
./download_historical_data.py \
  --symbols BTC-USD \
  --granularity 300 \
  --start 2025-01-01
```

Data will be saved to `historical_data/` directory as CSV files.

### 2. Run Backtests

Test all three strategies (mean_reversion, trend_following, auto):

```bash
# Test all strategies with default settings
./backtest.py

# Test specific strategy with custom capital
./backtest.py --strategies trend_following --initial-capital 50000

# Test multiple symbols
./backtest.py --symbols BTC-USD ETH-USD SOL-USD LINK-USD

# Test all strategies with comparison
./backtest.py \
  --symbols BTC-USD ETH-USD \
  --strategies mean_reversion trend_following auto \
  --initial-capital 10000
```

## Strategy Descriptions

### Mean Reversion Strategy
- **Entry**: RSI oversold (<30) + price below lower Bollinger Band
- **Exit**: Price reaches SMA, trailing stop loss, or time-based exit
- **Best for**: Neutral/choppy markets
- **Regime skip**: BEAR and STRONG_BEAR regimes

### Trend Following Strategy
- **Entry**: MA crossover (short > long) + momentum + RSI filter
- **Exit**: Multi-level take profit (TP1, TP2), ATR trailing stop, MA cross exit
- **Best for**: Trending markets (BULL/BEAR)
- **Regime skip**: NEUTRAL regime

### Auto (Dynamic) Strategy
- **Switches between strategies based on market regime**:
  - STRONG_BULL/BULL → Trend Following
  - NEUTRAL → Mean Reversion
  - BEAR/STRONG_BEAR → Trend Following
- **Best for**: All market conditions

## Output Files

Backtest generates the following files:

- `equity_curve_{strategy}.csv` - Portfolio value over time
- `trades_{strategy}.csv` - All executed trades with P&L
- Console output with performance metrics

## Performance Metrics

The backtest calculates:

- **Total Return %**: Overall profit/loss percentage
- **Max Drawdown %**: Largest peak-to-trough decline
- **Sharpe Ratio**: Risk-adjusted return (annualized)
- **Number of Trades**: Total buy/sell executions
- **Win Rate %**: Percentage of profitable trades
- **Average Win %**: Average profit on winning trades
- **Average Loss %**: Average loss on losing trades
- **Profit Factor**: Ratio of gross profits to gross losses

## Example Output

```
================================================================================
BACKTEST RESULTS - AUTO
================================================================================
Initial Capital:     $10,000.00
Final Value:         $15,234.56
Total Return:        +52.35%
Max Drawdown:        -12.45%
Sharpe Ratio:        1.85

Trade Statistics:
  Total Trades:      127
  Win Rate:          58.3%
  Avg Win:           +8.45%
  Avg Loss:          -3.21%
  Profit Factor:     2.14
================================================================================
```

## Tips for Better Backtesting

1. **Use longer timeframes**: Test on at least 1 year of data
2. **Test multiple symbols**: Diversification is key
3. **Compare strategies**: Run all three to see which performs best
4. **Check different market conditions**: Include both bull and bear markets
5. **Watch the equity curve**: Smooth upward curve is better than erratic spikes
6. **Consider transaction costs**: Backtest doesn't include fees yet (coming soon)

## Limitations

Current backtesting limitations:
- No transaction fees (0.6% on Coinbase)
- No slippage simulation
- Perfect order fills at close prices
- No liquidity constraints
- Simplified regime detection (no BTC dominance)

These will be added in future versions for more realistic results.

## Advanced Usage

### Custom Configuration

The backtest uses your environment variables from `.env`. Key settings:

- `MA_SHORT_WINDOW` / `MA_LONG_WINDOW`: Moving average periods
- `MAX_POSITIONS`: Maximum concurrent positions
- `TAKE_PROFIT_1_PCT` / `TAKE_PROFIT_2_PCT`: Profit targets
- `TRAILING_STOP_PCT`: Stop loss percentage
- `MR_RSI_OVERSOLD`: Mean reversion RSI threshold
- `MR_BOLLINGER_PERIOD` / `MR_BOLLINGER_STD`: Bollinger Band settings

Adjust these in your `.env` file to test different parameter combinations.

### Analyzing Results

Load equity curves and trades in Python for detailed analysis:

```python
import pandas as pd
import matplotlib.pyplot as plt

# Load equity curve
equity = pd.read_csv('equity_curve_auto.csv')
equity['timestamp'] = pd.to_datetime(equity['timestamp'])

# Plot portfolio value over time
plt.figure(figsize=(12, 6))
plt.plot(equity['timestamp'], equity['portfolio_value'])
plt.title('Portfolio Value Over Time')
plt.xlabel('Date')
plt.ylabel('Value ($)')
plt.grid(True)
plt.show()

# Load and analyze trades
trades = pd.read_csv('trades_auto.csv')
winning_trades = trades[trades['pnl'] > 0]
losing_trades = trades[trades['pnl'] < 0]

print(f"Average win: ${winning_trades['pnl'].mean():.2f}")
print(f"Average loss: ${losing_trades['pnl'].mean():.2f}")
```
