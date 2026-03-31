# Interactive Backtest Dashboard

Interactive web dashboard for visualizing cryptocurrency trading bot backtest results.

## Features

- 📊 **Interactive Charts** - Equity curves with Lightweight Charts
- 🎯 **Performance Metrics** - Return, Sharpe, Drawdown, Win Rate
- 🔄 **Dynamic Selection** - Choose period, timeframe, and strategy
- 📱 **Responsive Design** - Works on desktop and mobile
- ⚡ **Fast Loading** - Optimized JSON data (~160K points)

## Quick Start

### Local Testing

```bash
cd dashboard
python3 -m http.server 8000
```

Then open http://localhost:8000

### Updating Data

After running new backtests:

```bash
cd backtesting
source ../venv/bin/activate
./scripts/prepare_dashboard_data.py
```

This will:
- Convert CSV results to optimized JSON
- Decimate equity curves to 5000 points
- Generate manifest.json index
- Reduce file size by 40-60%

## Deploy to GitHub Pages

### Option 1: Separate gh-pages Branch

```bash
# Create gh-pages branch
git checkout --orphan gh-pages

# Copy dashboard files
cp -r dashboard/* .

# Commit and push
git add .
git commit -m "Deploy dashboard to GitHub Pages"
git push origin gh-pages

# Return to main branch
git checkout master
```

Then enable GitHub Pages in repository settings:
- Settings → Pages → Source: gh-pages branch

### Option 2: docs/ Directory (Main Branch)

```bash
# Move dashboard to docs/
mv dashboard docs

# Commit
git add docs/
git commit -m "Add interactive dashboard"
git push
```

Then enable GitHub Pages:
- Settings → Pages → Source: master branch → /docs folder

## File Structure

```
dashboard/
├── index.html              # Main dashboard page
├── data/                   # Backtest data (gitignored)
│   ├── manifest.json       # Index of all results
│   └── *.json             # Equity and trade data
└── README.md              # This file
```

## Technology

- **Charts:** [Lightweight Charts](https://tradingview.github.io/lightweight-charts/) by TradingView
- **Metrics:** Chart.js
- **Framework:** Vanilla JavaScript (no build step)
- **Hosting:** GitHub Pages (free static hosting)

## Data Format

Equity curves are stored as JSON:
```json
{
  "timestamps": ["2024-01-01T00:00:00", ...],
  "portfolio_value": [10000, 10050, ...],
  "cash": [5000, 4900, ...],
  "regime": ["BULL", "BULL", ...]
}
```

Trades are stored as:
```json
[
  {
    "timestamp": "2024-01-15T14:30:00",
    "asset": "BTC-USD",
    "price": 43500,
    "pnl_pct": 5.2,
    "reason": "Take profit 1"
  }
]
```

## Maintenance

The dashboard automatically reads from `data/manifest.json`. To add new backtests:

1. Run backtests (creates CSV files in `data/results/`)
2. Run `prepare_dashboard_data.py` (converts to JSON)
3. Refresh dashboard (data updates automatically)

No code changes needed!
