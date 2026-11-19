# trading-bot-flake

A crypto trading bot for Coinbase Advanced and Base (on-chain), packaged as a Nix flake with a
Home Manager module. It runs live, paper (simulated ledger) and backtest modes from the same code.

## Layout

| Path | What lives there |
|---|---|
| `trading_bot.py` | Main loop (`trading-bot`): regime detection, entries, exits, notifications |
| `strategies/` | Entry/exit decision logic, one file per strategy; `__init__.py` is the registry |
| `executors/` | Venues behind one interface (`base.py`): Coinbase, Coinbase futures, paper ledger, Base (`ethereum_executor.py`: Aerodrome, 0x, KyberSwap, CoW) |
| `core/` | Shared machinery: risk manager, regime detection, indicators, state, trade log, control channel, Telegram |
| `config/` | Settings read from the environment (`trading_config.py`, `executor_config.py`, `network_config.py`) |
| `backtesting/` | Backtest engine (`backtest.py`), data download, and dated research experiments |
| `nix/hm-module.nix` | `services.trading-bot` Home Manager module: every systemd service, timer and setting |
| `tests/` | pytest suite (the only directory pytest collects) |
| `scripts/` | Manual one-off tools; `live_swap.py` / `swap_smoke.py` can place real swaps |
| `docs/` | Strategy, risk and backtesting guides |

Top-level entry points and the commands they become:

| File | Command |
|---|---|
| `trading_bot.py` | `trading-bot` (`--ws` for the WebSocket service) |
| `trading_botctl.py` | `trading-botctl` (pause, resume, flatten, status over the control socket) |
| `report_bot.py`, `notify_telegram.py`, `pnl_report.py` | `trading-report`, `trading-notify`, `trading-pnl` |
| `onchain_cost_probe.py`, `onchain_cost_report.py` | `onchain-cost-probe`, `onchain-cost-report` (quote-only DEX vs Coinbase costs) |
| `onchain_test_swap.py` | `onchain-test-swap` (small live swaps; dry run unless `--confirm`) |
| `debug_tx.py` | `debug-tx` |

## Where to make a change

| Change | Where |
|---|---|
| New strategy | Add `strategies/<name>.py` (copy a sibling) and one line in `STRATEGIES` in `strategies/__init__.py` |
| Position sizing, drawdown pause | `core/risk_manager.py` |
| Stops, take-profits, exit flow | `run_executor_strategy` in `trading_bot.py` |
| Market regime | `core/regime_detection.py` |
| New venue or DEX route | `executors/` (Base routes: `ethereum_executor.py`, safety settings in its docstring) |
| A setting | Read it in `config/`, then expose it via `settings` in `nix/hm-module.nix` |
| Services, timers, paper variants | `nix/hm-module.nix`; per-host values live in the nixosConfig repo (`homeManagerModules/coinbase-trader.nix`) |

## Develop

```sh
nix develop -c pytest -q          # full test suite
nix build                         # package with all commands
nix run .#backtest -- --help      # backtest engine
```

Live trading runs only where `TRADING_MODE=live` is set by the Home Manager module; everything
else defaults to paper.
