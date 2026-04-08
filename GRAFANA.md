# Grafana Dashboard Queries

## Loki Queries

Logs are shipped from the `coinbase-trader-ws.service` user systemd service.
Use `{unit="coinbase-trader-ws.service"}` as the stream selector.

---

### Bot Health & Uptime

**Bot run starts (heartbeat)**
```logql
{unit="coinbase-trader-ws.service"} |= "Crypto Bot Run"
```

**Errors over time**
```logql
sum by (level) (count_over_time({unit="coinbase-trader-ws.service"} |= "ERROR" [5m]))
```

**Warnings over time**
```logql
sum by (level) (count_over_time({unit="coinbase-trader-ws.service"} |= "WARNING" [5m]))
```

**Lock contention (another instance running)**
```logql
{unit="coinbase-trader-ws.service"} |= "Another bot instance is already running"
```

---

### Trade Activity

**All buy executions**
```logql
{unit="coinbase-trader-ws.service"} |~ "Buy .+ confirmed"
```

**All sell executions**
```logql
{unit="coinbase-trader-ws.service"} |= "✅ Sold"
```

**Trade PnL per trade**
```logql
{unit="coinbase-trader-ws.service"} |= "📊 PnL"
```

**Losing trades only**
```logql
{unit="coinbase-trader-ws.service"} |= "📊 PnL: $-"
```

**Winning trades only**
```logql
{unit="coinbase-trader-ws.service"} |= "📊 PnL" |~ "PnL: \\$\\+"
```

**Exit signals (all reasons)**
```logql
{unit="coinbase-trader-ws.service"} |= "🚨"
```

**Stop-loss triggers**
```logql
{unit="coinbase-trader-ws.service"} |= "🚨" |~ "[Ss]top"
```

**Take-profit triggers**
```logql
{unit="coinbase-trader-ws.service"} |= "🚨" |~ "[Tt]ake.profit|TP[12]"
```

**Trailing stop triggers**
```logql
{unit="coinbase-trader-ws.service"} |= "🚨" |~ "[Tt]railing"
```

**Trend exit deferrals (fee floor guard)**
```logql
{unit="coinbase-trader-ws.service"} |= "Trend-exit deferred"
```

---

### Order Execution Quality

**Limit orders placed (post-only maker)**
```logql
{unit="coinbase-trader-ws.service"} |= "Placing LIMIT" |= "Post-Only"
```

**Aggressive limit orders (stop-loss, no post-only)**
```logql
{unit="coinbase-trader-ws.service"} |= "Placing AGGRESSIVE LIMIT"
```

**Market order fallbacks (expensive taker fills)**
```logql
{unit="coinbase-trader-ws.service"} |= "Placing MARKET"
```

**Orders that failed to fill**
```logql
{unit="coinbase-trader-ws.service"} |= "not filled after"
```

**Order rejections / failures**
```logql
{unit="coinbase-trader-ws.service"} |~ "sell (failed|rejected)"
```

**Market order fallback rate (should be near zero)**
```logql
sum(count_over_time({unit="coinbase-trader-ws.service"} |= "falling back to market order" [24h]))
```

**Dust-skipped sells**
```logql
{unit="coinbase-trader-ws.service"} |= "dust-skipped"
```

---

### Market Regime

**Regime changes (confirmed)**
```logql
{unit="coinbase-trader-ws.service"} |= "Regime change confirmed"
```

**Current regime**
```logql
{unit="coinbase-trader-ws.service"} |= "Market Regime:" | line_format "{{.message}}"
```

**Regime pending confirmation**
```logql
{unit="coinbase-trader-ws.service"} |= "pending confirmation"
```

**BTC macro trend with MA values**
```logql
{unit="coinbase-trader-ws.service"} |= "BTC Macro:" |~ "MA"
```

**ETH/BTC rotation signal**
```logql
{unit="coinbase-trader-ws.service"} |= "ETH/BTC Rotation"
```

**Strategy switches**
```logql
{unit="coinbase-trader-ws.service"} |= "Strategy switched"
```

---

### Portfolio & Risk

**Portfolio value over time**
```logql
{unit="coinbase-trader-ws.service"} |= "Aggregate Portfolio Total"
```

**Sub-portfolio values (per executor)**
```logql
{unit="coinbase-trader-ws.service"} |= "Sub-Portfolio Value"
```

**Drawdown warnings**
```logql
{unit="coinbase-trader-ws.service"} |= "Drawdown" |~ "exceeds limit"
```

**Drawdown pause active**
```logql
{unit="coinbase-trader-ws.service"} |= "Drawdown pause active"
```

**New high water marks**
```logql
{unit="coinbase-trader-ws.service"} |= "New high water mark"
```

**Position limit hits**
```logql
{unit="coinbase-trader-ws.service"} |= "at max concurrent positions"
```

**Bear position scaling**
```logql
{unit="coinbase-trader-ws.service"} |= "Bear scaling"
```

**Performance summary (win rate, PnL)**
```logql
{unit="coinbase-trader-ws.service"} |= "[Performance]"
```

---

### Entry Analysis

**Skipped entries (all reasons)**
```logql
{unit="coinbase-trader-ws.service"} |= "Skip:"
```

**Skipped due to RSI overbought**
```logql
{unit="coinbase-trader-ws.service"} |= "Skip:" |~ "RSI|overbought"
```

**Volume spike RSI override**
```logql
{unit="coinbase-trader-ws.service"} |= "volume spike allows"
```

**Bear momentum entries**
```logql
{unit="coinbase-trader-ws.service"} |= "Bear momentum entry"
```

**Regime skip (no entries in current regime)**
```logql
{unit="coinbase-trader-ws.service"} |= "regime" |= "skipping new entries"
```

---

### Infrastructure / Errors

**API errors**
```logql
{unit="coinbase-trader-ws.service"} |= "request failed"
```

**WebSocket disconnections**
```logql
{unit="coinbase-trader-ws.service"} |= "WebSocket disconnected"
```

**RPC failovers (Ethereum executor)**
```logql
{unit="coinbase-trader-ws.service"} |= "Rotating RPC"
```

**RPC retry attempts**
```logql
{unit="coinbase-trader-ws.service"} |= "RPC call failed"
```

**Insufficient gas**
```logql
{unit="coinbase-trader-ws.service"} |= "Insufficient ETH for gas"
```

**Telegram failures**
```logql
{unit="coinbase-trader-ws.service"} |= "Failed to send Telegram"
```

**Orphaned state cleanup**
```logql
{unit="coinbase-trader-ws.service"} |= "Removing orphaned state entry"
```

---

### Useful Aggregations (Stat Panels / Graphs)

**Trades per day**
```logql
sum(count_over_time({unit="coinbase-trader-ws.service"} |= "📊 PnL" [24h]))
```

**Sells per day**
```logql
sum(count_over_time({unit="coinbase-trader-ws.service"} |= "✅ Sold" [24h]))
```

**Errors per hour**
```logql
sum(count_over_time({unit="coinbase-trader-ws.service"} |= "ERROR" [1h]))
```

**Stop-losses per day**
```logql
sum(count_over_time({unit="coinbase-trader-ws.service"} |= "🚨" |~ "[Ss]top" [24h]))
```

**Market order fallbacks per day (fee waste indicator)**
```logql
sum(count_over_time({unit="coinbase-trader-ws.service"} |= "Placing MARKET" [24h]))
```

**Fee floor deferrals per day**
```logql
sum(count_over_time({unit="coinbase-trader-ws.service"} |= "Trend-exit deferred" [24h]))
```

---

## Prometheus Queries

There's no Prometheus exporter in the bot currently, but if you add one
(e.g. `prometheus_client` with a `/metrics` endpoint or pushgateway),
here are worthwhile metrics to expose and query.

### Metrics to Instrument

```python
# Counters
trades_total              # labels: side={buy,sell}, asset, executor, reason
order_type_total          # labels: type={limit_postonly,aggressive_limit,market}
errors_total              # labels: component={api,rpc,ws,telegram}
regime_changes_total      # labels: from_regime, to_regime
entry_skips_total         # labels: asset, reason={rsi,regime,volume,max_positions}

# Gauges
portfolio_value_usd       # labels: executor
cash_available_usd        # labels: executor, currency={USD,USDC}
open_positions            # labels: executor
current_regime            # enum gauge or info metric
drawdown_pct              # labels: executor
btc_price_usd
rsi_current               # labels: asset
high_water_mark_usd       # labels: executor

# Histograms
trade_pnl_usd             # labels: asset, executor
trade_pnl_pct             # labels: asset
fee_cost_usd              # labels: asset, order_type
order_fill_duration_sec   # labels: order_type
```

### Example PromQL Queries

**Portfolio value over time**
```promql
portfolio_value_usd
```

**Total PnL (cumulative)**
```promql
sum(trade_pnl_usd)
```

**Win rate (last 24h)**
```promql
sum(rate(trades_total{pnl="win"}[24h])) / sum(rate(trades_total{side="sell"}[24h]))
```

**Average PnL per trade**
```promql
histogram_quantile(0.5, rate(trade_pnl_usd_bucket[24h]))
```

**Fee costs per day**
```promql
sum(increase(fee_cost_usd_sum[24h]))
```

**Maker vs taker order ratio**
```promql
sum by (type) (rate(order_type_total[24h]))
```

**Drawdown alert (> 10%)**
```promql
drawdown_pct > 10
```

**Position count**
```promql
sum by (executor) (open_positions)
```

**Trade frequency (trades per hour)**
```promql
sum(rate(trades_total{side="sell"}[1h])) * 3600
```

**Regime distribution (time spent in each)**
```promql
count_over_time(current_regime[7d])
```

**Error rate spike detection**
```promql
sum(rate(errors_total[5m])) > 0.1
```

**RSI readings for open positions**
```promql
rsi_current{asset=~"BTC.*|ETH.*"}
```

### Alert Rules (Alertmanager)

```yaml
groups:
  - name: trading-bot
    rules:
      - alert: BotDown
        expr: time() - max(bot_last_run_timestamp) > 600
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "Trading bot hasn't run in 10+ minutes"

      - alert: HighDrawdown
        expr: drawdown_pct > 12
        for: 0m
        labels:
          severity: warning
        annotations:
          summary: "Drawdown {{ $value }}% exceeds threshold"

      - alert: MarketOrderSpike
        expr: sum(increase(order_type_total{type="market"}[1h])) > 3
        for: 0m
        labels:
          severity: warning
        annotations:
          summary: "{{ $value }} market orders in last hour (taker fees)"

      - alert: APIErrors
        expr: sum(rate(errors_total{component="api"}[5m])) > 0.05
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Elevated API error rate"
```
