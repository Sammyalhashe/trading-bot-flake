# Risk Management Recommendations

## Current Risk Controls

### ✅ Already Implemented

1. **Position Sizing** - Dynamic allocation based on portfolio value
2. **Maximum Positions** - Limit of 3 concurrent positions
3. **Trailing Stops** - ATR-based adaptive stops
4. **Take Profit Levels** - Two-tier profit taking (TP1, TP2)
5. **Minimum Volume Filter** - Avoids illiquid markets
6. **RSI Overbought Filter** - Prevents buying into pumps
7. **Regime Detection** - Skips unfavorable market conditions
8. **Dust Threshold** - Ignores tiny balances

### ⚠️ Gaps & Improvements Needed

Based on backtest results showing -25% drawdown, here are critical additions:

## 🚨 High Priority Improvements

### 1. Portfolio-Level Risk Limits

**Problem:** Individual position stops don't protect overall portfolio

**Solution: Add circuit breakers**

```python
# In TradingConfig
max_portfolio_drawdown_pct = 0.15  # Stop all trading at -15% portfolio DD
daily_loss_limit_pct = 0.05        # Pause trading for day at -5% daily loss
```

**Implementation:**
- Track portfolio high-water mark
- If current value < HWM * (1 - max_portfolio_drawdown_pct), halt all trading
- Require manual restart after hitting circuit breaker
- Send Telegram alert

### 2. Position Size Limits

**Problem:** Equal allocation can risk too much on single trades

**Solution: Add risk-based sizing**

```python
# Current: Equal allocation (portfolio_value / max_positions)
# Better: Risk-based sizing

max_position_size_pct = 0.30  # No single position > 30% of portfolio
max_risk_per_trade_pct = 0.02  # Risk only 2% per trade

position_size = min(
    portfolio_value * max_position_size_pct,
    (portfolio_value * max_risk_per_trade_pct) / expected_stop_distance
)
```

### 3. Correlation Limits

**Problem:** Holding BTC, ETH, SOL = essentially same bet (highly correlated)

**Solution: Limit correlated positions**

```python
# Group correlated assets
correlation_groups = {
    "major_crypto": ["BTC", "ETH"],
    "defi": ["UNI", "AAVE", "LINK"],
    "layer1": ["SOL", "AVAX", "ADA"]
}

max_positions_per_group = 1  # Only 1 major crypto at a time
```

### 4. Volatility-Adjusted Stops

**Problem:** Fixed 3% stop too tight in high volatility, too loose in low volatility

**Solution: Already using ATR! But needs tuning**

```python
# Current: atr_stop = max(0.03, min(0.15, 2.5 * atr / price))
# Better: Adjust multiplier by regime

atr_multiplier = {
    "STRONG_BULL": 3.0,   # Wider stops in trends
    "BULL": 2.5,
    "NEUTRAL": 2.0,       # Tighter in chop
    "BEAR": 2.5,
    "STRONG_BEAR": 3.0
}
```

### 5. Time-Based Risk Controls

**Problem:** Bot can overtrade during volatile periods

**Solution: Rate limiting**

```python
max_trades_per_hour = 2
max_trades_per_day = 10
min_time_between_trades_minutes = 30  # Cool-off period
```

### 6. Drawdown-Based Position Reduction

**Problem:** Same position size whether up or down

**Solution: Scale down when losing**

```python
# If portfolio down 5%, reduce position sizes by 50%
# If down 10%, reduce by 75%
# If down 15%, stop trading

if portfolio_dd < -0.05:
    position_size *= 0.5
elif portfolio_dd < -0.10:
    position_size *= 0.25
elif portfolio_dd < -0.15:
    position_size = 0  # Circuit breaker
```

## 🎯 Medium Priority Improvements

### 7. Leverage Limits

**Current:** No leverage tracking

**Add:**
- Track total capital deployed vs available
- Prevent over-leveraging (max 1.0x = no leverage for crypto)
- Alert if getting close to margin call (if using margin)

### 8. Slippage Protection

**Current:** Assumes perfect fills at close price

**Add:**
```python
max_slippage_pct = 0.005  # 0.5% max slippage
limit_price = current_price * (1 + max_slippage_pct)  # For buys
```

### 9. Fee Awareness

**Current:** Ignores 0.6% Coinbase fees

**Impact:** On 10 round-trip trades, that's -12% drag!

**Add:**
```python
# Include fees in backtest
# Only take trades with expected profit > 3x fees
min_expected_return = trading_fees * 3  # 1.8% minimum target
```

### 10. Kelly Criterion Position Sizing

**Current:** Equal allocation

**Better:** Optimal sizing based on edge

```python
# Kelly Formula: f = (p*b - q) / b
# f = fraction of capital to risk
# p = win probability
# b = win/loss ratio
# q = 1 - p

if win_rate > 0.55 and profit_factor > 1.5:
    kelly_fraction = (win_rate * profit_factor - (1 - win_rate)) / profit_factor
    kelly_fraction = kelly_fraction * 0.5  # Half Kelly for safety
    position_size = portfolio_value * kelly_fraction
```

## 📊 Monitoring & Alerts

### Critical Metrics to Track

1. **Portfolio Drawdown** - Real-time vs HWM
2. **Daily P&L** - Running total for the day
3. **Trade Win Rate** - Rolling 20-trade average
4. **Average Trade Duration** - Detect if getting stopped too quickly
5. **Sharpe Ratio** - Rolling 30-day risk-adjusted return
6. **Profit Factor** - Gross wins / gross losses
7. **Largest Loss** - Track worst single trade
8. **Consecutive Losses** - Stop after 3-5 losses in a row

### Telegram Alerts

```python
# Send alerts for:
- Portfolio DD > -5% (WARNING)
- Portfolio DD > -10% (CRITICAL)
- Portfolio DD > -15% (SHUTDOWN)
- Single trade loss > -3%
- Daily loss > -5%
- 3 consecutive losses
- Position size > 35% of portfolio
- Trading halted (circuit breaker)
```

## 🔧 Implementation Priority

### Phase 1 (Critical - Do First)
1. Portfolio-level drawdown limits
2. Daily loss limits
3. Position size caps (max 30% per position)
4. Telegram alerts for critical events

### Phase 2 (Important - Do Soon)
1. Correlation limits
2. Time-based controls (max trades per day)
3. Drawdown-based position reduction
4. Fee-aware backtesting

### Phase 3 (Nice to Have)
1. Kelly Criterion sizing
2. Advanced volatility adjustments
3. Slippage modeling
4. ML-based risk scoring

## 📈 Backtest Validation

After implementing risk controls:

1. **Re-run comprehensive backtest** with risk limits
2. **Expect lower returns** but much lower drawdowns
3. **Target metrics**:
   - Max DD < -10%
   - Sharpe > 1.5
   - Profit Factor > 2.0
   - Win Rate > 50%

4. **Stress test** on worst historical period
5. **Monte Carlo** simulation with position sizing

## 🎓 Risk Management Principles

1. **Survive First, Profit Second** - Don't blow up
2. **Cut Losers, Let Winners Run** - Asymmetric risk/reward
3. **Position Size = Your Confidence** - Bigger edge = bigger size
4. **Correlation Kills** - Diversify or concentrate, not both badly
5. **Drawdowns Compound** - -50% needs +100% to recover
6. **Risk What You Can Afford to Lose** - Sleep test

## 💡 Quick Wins

Easiest improvements to implement today:

```python
# 1. Add to TradingConfig
MAX_PORTFOLIO_DRAWDOWN = 0.15
DAILY_LOSS_LIMIT = 0.05
MAX_POSITION_SIZE_PCT = 0.30

# 2. In trading_bot.py, before any entry:
portfolio_dd = (current_value / portfolio_hwm - 1)
daily_pnl_pct = (current_value / start_of_day_value - 1)

if portfolio_dd < -MAX_PORTFOLIO_DRAWDOWN:
    logging.critical("Portfolio drawdown limit hit! Halting trading.")
    send_telegram_alert("🚨 TRADING HALTED - Portfolio DD > 15%")
    sys.exit(1)

if daily_pnl_pct < -DAILY_LOSS_LIMIT:
    logging.warning("Daily loss limit hit. Pausing until tomorrow.")
    return  # Skip this scan cycle

# 3. Before opening position:
position_value = position_size * price
if position_value > current_portfolio_value * MAX_POSITION_SIZE_PCT:
    position_size = (current_portfolio_value * MAX_POSITION_SIZE_PCT) / price
```

These 3 simple changes would prevent catastrophic losses!
