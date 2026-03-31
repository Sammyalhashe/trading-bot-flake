# Comprehensive Backtesting Guide

This script tests **all strategies** across **multiple timeframes** and **different market conditions** to find the optimal configuration.

## Quick Start

### Run Everything (Full Suite)
Tests all 3 strategies × 5 timeframes × 5 market periods = **75 backtests**:

```bash
source venv/bin/activate
./comprehensive_backtest.py --symbols BTC-USD
```

**Warning:** This will take 30-60 minutes and download ~500MB of data!

### Quick Test (Recommended First)
Test just the current timeframe (1h) across different market conditions:

```bash
./comprehensive_backtest.py --symbols BTC-USD --timeframes 1h
# Tests: 3 strategies × 1 timeframe × 5 periods = 15 backtests (~5 minutes)
```

### Test Specific Combinations

```bash
# Test mean reversion only, all timeframes, recent market
./comprehensive_backtest.py \
  --symbols BTC-USD \
  --strategies mean_reversion \
  --periods YTD_2025

# Test just 1h vs 4h on all strategies
./comprehensive_backtest.py \
  --symbols BTC-USD \
  --timeframes 1h 4h

# Test with multiple symbols
./comprehensive_backtest.py \
  --symbols BTC-USD ETH-USD \
  --timeframes 1h 4h \
  --periods Q1_2024_Bull YTD_2025
```

## What Gets Tested

### 📅 Market Periods (5 different conditions)

| Period | Dates | Market Type |
|--------|-------|-------------|
| **Q1_2024_Bull** | Jan-Mar 2024 | Strong bull market (BTC $42k → $61k) |
| **Q2_2023_Sideways** | Mar-Jun 2023 | Choppy/sideways consolidation |
| **2023_Full_Year** | Jan-Dec 2023 | Mixed conditions (full year) |
| **YTD_2025** | Jan 2025 - Now | Recent market (15 months) |
| **H2_2024** | Jul-Dec 2024 | Second half of 2024 |

### ⏱️ Timeframes (5 different intervals)

- **15m** - 15 minute bars (fast scalping)
- **30m** - 30 minute bars
- **1h** - 1 hour bars (current default)
- **4h** - 4 hour bars (swing trading)
- **1d** - Daily bars (position trading)

### 🎯 Strategies (3 types)

- **mean_reversion** - RSI + Bollinger Band entries
- **trend_following** - MA crossover + momentum
- **auto** - Dynamic switching based on regime

## Output Reports

The script generates several reports in the `results/` directory:

### 1. Raw Results CSV
`results/comprehensive_results.csv` - Complete dataset with all metrics

### 2. Console Reports

**Best Strategy by Market Condition:**
Shows which strategy + timeframe won in each market type

**Best Timeframe by Strategy:**
Optimal timeframe for each strategy across all conditions

**Top 10 Configurations (by Return):**
Highest absolute returns

**Top 10 Configurations (by Sharpe):**
Best risk-adjusted returns

**Average Performance by Strategy:**
Overall strategy comparison

**Average Performance by Timeframe:**
Overall timeframe comparison

### 3. Individual Result Files

For each test, you get:
- `results/{period}_{timeframe}_{strategy}_equity_curve.csv`
- `results/{period}_{timeframe}_{strategy}_trades.csv`

## Example Output

```
================================================================================
COMPREHENSIVE BACKTEST RESULTS
================================================================================

📊 BEST STRATEGY BY MARKET CONDITION
--------------------------------------------------------------------------------

Q1_2024_Bull (Strong Bull Market)
  Winner: trend_following @ 4h
  Return: +15.23% | Sharpe: 6.45 | MaxDD: -3.2% | Win Rate: 71.4%

Q2_2023_Sideways (Sideways/Choppy Market)
  Winner: mean_reversion @ 15m
  Return: +3.87% | Sharpe: 4.12 | MaxDD: -0.8% | Win Rate: 68.9%

YTD_2025 (Recent Market)
  Winner: mean_reversion @ 1h
  Return: +4.23% | Sharpe: 0.78 | MaxDD: -2.8% | Win Rate: 56.2%


📈 BEST TIMEFRAME BY STRATEGY
--------------------------------------------------------------------------------

MEAN_REVERSION
  Best:  15m in Q2_2023_Sideways → +3.87% (Sharpe: 4.12)
  Worst: 1d in Q1_2024_Bull → -1.23% (Sharpe: -0.45)

TREND_FOLLOWING
  Best:  4h in Q1_2024_Bull → +15.23% (Sharpe: 6.45)
  Worst: 15m in YTD_2025 → -12.34% (Sharpe: -1.23)

AUTO
  Best:  1h in Q1_2024_Bull → +5.72% (Sharpe: 2.82)
  Worst: 15m in YTD_2025 → -8.91% (Sharpe: -0.67)


🏆 TOP 10 CONFIGURATIONS (by Return)
--------------------------------------------------------------------------------
Rank  Strategy           Timeframe  Period              Return    Sharpe   MaxDD
--------------------------------------------------------------------------------
1     trend_following    4h         Q1_2024_Bull        +15.23%   6.45    -3.20%
2     trend_following    1h         Q1_2024_Bull        +11.01%   5.12    -4.40%
3     trend_following    30m        Q1_2024_Bull         +9.87%   4.23    -5.10%
...
```

## Advanced Options

### Skip Data Download
If you already have the data:

```bash
./comprehensive_backtest.py --skip-download --symbols BTC-USD
```

### Change Initial Capital

```bash
./comprehensive_backtest.py --symbols BTC-USD --initial-capital 50000
```

### Focus on Recent Market Only

```bash
./comprehensive_backtest.py \
  --symbols BTC-USD ETH-USD \
  --periods YTD_2025 \
  --timeframes 15m 30m 1h 4h
```

## Interpreting Results

### What to Look For

1. **Consistent Winners** - Strategies that perform well across multiple conditions
2. **High Sharpe Ratios** - Better risk-adjusted returns (aim for >1.5)
3. **Low Drawdowns** - Max drawdown <10% preferred
4. **Win Rates** - Above 55% is good, but check avg win vs avg loss
5. **Trade Count** - Too few trades = not enough data, too many = overtrading

### Key Insights

- **Trend Following** typically needs larger timeframes (4h, daily) to avoid whipsaws
- **Mean Reversion** works better on smaller timeframes (15m, 30m, 1h) for quick reversions
- **Auto** strategy performance depends heavily on regime detection accuracy

### Recommended Configurations

Based on historical testing:

| Market Type | Strategy | Timeframe | Why |
|------------|----------|-----------|-----|
| Strong Trend | Trend Following | 4h or daily | Catches big moves, filters noise |
| Sideways/Chop | Mean Reversion | 15m or 30m | Fast reversions, tight stops |
| Mixed/Uncertain | Mean Reversion | 1h | Current default, good balance |

## Tips

1. **Start small** - Test one period first to validate setup
2. **Compare apples to apples** - Same symbols across all tests
3. **Watch for overfitting** - If one config is WAY better, it might be luck
4. **Consider transaction costs** - More trades = more fees (not yet in backtest)
5. **Test on recent data** - Market conditions change over time

## Performance Notes

- Each backtest takes ~10-20 seconds
- Full suite (75 tests) = ~30-60 minutes
- Data downloads add another 15-30 minutes
- Results use ~100MB disk space for full suite

## Next Steps

After running the comprehensive test:

1. Review the console output for high-level insights
2. Open `results/comprehensive_results.csv` in Excel/Python for deeper analysis
3. Identify top 3-5 configurations
4. Test those configurations on out-of-sample data (forward testing)
5. Update your bot's config to use the best timeframe/strategy combo
