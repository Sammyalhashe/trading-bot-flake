#!/bin/bash
# Quick backtest runner - downloads data and runs backtest

set -e

echo "=================================================="
echo "Trading Bot Backtesting Suite"
echo "=================================================="
echo ""

# Default values
SYMBOLS="${SYMBOLS:-BTC-USD ETH-USD}"
START_DATE="${START_DATE:-2024-01-01}"
INITIAL_CAPITAL="${INITIAL_CAPITAL:-10000}"
GRANULARITY="${GRANULARITY:-3600}"

echo "Configuration:"
echo "  Symbols: $SYMBOLS"
echo "  Start Date: $START_DATE"
echo "  Initial Capital: \$$INITIAL_CAPITAL"
echo "  Granularity: ${GRANULARITY}s ($(($GRANULARITY / 3600))h)"
echo ""

# Step 1: Download historical data
echo "Step 1: Downloading historical data..."
echo "=================================================="
./download_historical_data.py \
  --symbols $SYMBOLS \
  --start "$START_DATE" \
  --granularity $GRANULARITY

echo ""
echo "Step 2: Running backtests..."
echo "=================================================="
./backtest.py \
  --symbols $SYMBOLS \
  --strategies mean_reversion trend_following auto \
  --initial-capital $INITIAL_CAPITAL

echo ""
echo "=================================================="
echo "Backtesting complete!"
echo "=================================================="
echo ""
echo "Output files:"
echo "  - equity_curve_*.csv (portfolio value over time)"
echo "  - trades_*.csv (all executed trades)"
echo ""
echo "Next steps:"
echo "  - Review the results above"
echo "  - Analyze equity curves with Python/Excel"
echo "  - Adjust strategy parameters in .env"
echo "  - Re-run with different symbols or date ranges"
echo ""
