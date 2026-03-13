---
name: coinbase-trader
description: Manage and monitor the Coinbase trading bot. Use this skill to check service status, restart the bot, view logs, or troubleshoot issues with the automated trading service.
---

# Coinbase Trader

## Overview
This skill provides commands and procedures for managing the Coinbase trading bot, which is managed by `openclaw` and runs as a `systemctl` user service.

## Service Management
The bot runs as a user-level systemd service named `coinbase-trader.service`.

- **Check Status**: `systemctl --user status coinbase-trader.service`
- **Restart Service**: `systemctl --user restart coinbase-trader.service`
- **Start Service**: `systemctl --user start coinbase-trader.service`
- **Stop Service**: `systemctl --user stop coinbase-trader.service`

## Monitoring & Logs
- **Openclaw Logs**: Run `openclaw logs` to view the logs from the openclaw manager.
- **Application Logs**: The bot's internal logs are located at `/home/salhashemi2/.openclaw/workspace/trading-bot/trading.log`.
- **Trading Reports**: Reports are generated at `/home/salhashemi2/.openclaw/workspace/trading-bot/report.txt`.

## Troubleshooting
If the service fails to start, verify:
1. The Nix flake builds correctly: `nix build .#default`
2. Environment variables (API keys, etc.) are correctly set in the environment or secrets.
3. The log file directory exists and is writable.
