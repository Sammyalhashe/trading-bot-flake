# Repository Cleanup Plan

## Current Structure Issues

1. **Too many root-level files** - 13 Python scripts cluttering the root
2. **Test/debug files mixed with production** - `test_swap.py`, `check_btc_pool.py`, etc.
3. **Unclear separation** - Production vs development vs backtesting tools
4. **Documentation scattered** - Multiple MD files in root

## Proposed Structure

```
trading-bot-flake/
├── src/                        # Production code
│   ├── trading_bot.py         # Main bot (moved from root)
│   ├── report_bot.py          # Reporting (moved from root)
│   ├── notify_telegram.py     # Notifications (moved from root)
│   ├── core/                  # Core business logic (existing)
│   ├── config/                # Configuration (existing)
│   ├── executors/             # Executors (existing)
│   └── strategies/            # Strategies (existing)
│
├── backtesting/               # Backtesting suite
│   ├── backtest.py           # Main backtest engine
│   ├── comprehensive_backtest.py
│   ├── download_historical_data.py
│   ├── plot_backtest.py
│   └── run_backtest.sh
│
├── tools/                     # Dev/debug utilities
│   ├── debug_tx.py
│   ├── check_btc_pool.py
│   ├── check_wbtc_pool.py
│   ├── find_link_token.py
│   └── pool_discovery.py
│
├── tests/                     # Test files
│   ├── test_swap.py
│   ├── test_live_swap.py
│   ├── test_strategies.py    # (existing)
│   ├── test_coinbase_executor.py  # (existing)
│   └── test_trading_bot.py   # (existing)
│
├── scripts/                   # Utility scripts (existing - keep as is)
│   ├── migrate_state.py
│   ├── dev_utils.py
│   └── pool_discovery.py
│
├── docs/                      # Documentation
│   ├── BACKTESTING.md
│   ├── COMPREHENSIVE_BACKTEST.md
│   ├── IMPLEMENTATION_SUMMARY.md
│   ├── UNISWAP_V3_ON_BASE.md
│   └── RISK_MANAGEMENT.md     # (new)
│
├── data/                      # Data directories
│   ├── historical_data/       # Downloaded market data
│   ├── backtest_data/        # Organized backtest data
│   └── results/              # Backtest results
│
├── flake.nix                  # Nix configuration
├── README.md                  # Main readme
├── .gitignore
└── venv/                      # Python virtual env (gitignored)
```

## Files to Keep in Root

- `flake.nix` - Nix configuration
- `README.md` - Project readme
- `.gitignore` - Git ignore rules
- `secrets.yaml` - SOPS encrypted secrets (if exists)

## Files to Move

### → src/
- `trading_bot.py`
- `report_bot.py`
- `notify_telegram.py`

### → backtesting/
- `backtest.py`
- `comprehensive_backtest.py`
- `download_historical_data.py`
- `plot_backtest.py`
- `run_backtest.sh`

### → tools/
- `debug_tx.py`
- `check_btc_pool.py`
- `check_wbtc_pool.py`
- `find_link_token.py`

### → tests/
- `test_swap.py`
- `test_live_swap.py`

### → docs/
- `BACKTESTING.md`
- `COMPREHENSIVE_BACKTEST.md`
- `IMPLEMENTATION_SUMMARY.md`
- `UNISWAP_V3_ON_BASE.md`

## Files to DELETE (or archive)

**Consider removing these if not actively used:**
- `check_btc_pool.py` - Development debugging tool
- `check_wbtc_pool.py` - Development debugging tool
- `find_link_token.py` - One-time discovery script
- `test_swap.py` - Old test file
- `test_live_swap.py` - Old test file

**Archive option:** Move to `archive/` directory instead of deleting

## Migration Script

I can create a script to automatically reorganize everything. This will:
1. Create new directory structure
2. Move files to correct locations
3. Update import paths in Python files
4. Update flake.nix with new paths
5. Create symlinks for backward compatibility (optional)

## Impact on Nix Flake

After reorganization, `flake.nix` needs updates:
- Update `src` paths to `src/*.py`
- Update app program paths
- Possibly create separate apps for backtesting tools

## Backwards Compatibility

Options:
1. **Hard migration** - Change everything, update all imports
2. **Symlinks** - Keep symlinks in root for main scripts
3. **Gradual** - Move non-critical files first, test, then move core

## Recommendation

Start with **Option 3 (Gradual)**:
1. Move docs → `docs/` (safest, no code impact)
2. Move backtesting → `backtesting/` (self-contained)
3. Move tools → `tools/` (rarely used)
4. Test everything still works
5. Then move `src/` files and update flake.nix

This minimizes risk of breaking production.
