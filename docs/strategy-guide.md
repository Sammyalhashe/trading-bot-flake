# Trading Strategy Guide

A walkthrough of the algorithms in this codebase, what they do, and why they work (or don't).

---

## Table of Contents

1. [Core Concepts](#core-concepts)
2. [Technical Indicators](#technical-indicators)
3. [Trend-Following Strategy](#trend-following-strategy)
4. [Mean-Reversion Strategy](#mean-reversion-strategy)
5. [Risk Management](#risk-management)
6. [Market Regime Detection](#market-regime-detection)
7. [Further Reading](#further-reading)

---

## Core Concepts

### What is a trading strategy?

A trading strategy answers three questions:

1. **When do I buy?** (entry signal)
2. **When do I sell?** (exit signal)
3. **How much do I buy/sell?** (position sizing)

Everything else — fetching data, placing orders, tracking state — is infrastructure. The `Strategy` protocol in `core/strategy.py` captures this separation: strategies only decide *what* to do, while `trading_bot.py` handles *how* to do it.

### Two philosophies

The two strategies in this bot represent opposing beliefs about markets:

| | Trend-Following | Mean-Reversion |
|---|---|---|
| **Belief** | "Prices that are rising will keep rising" | "Prices that dropped too far will bounce back" |
| **Buys when** | Price is trending up | Price is abnormally low |
| **Sells when** | Trend reverses or profit target hit | Price returns to average |
| **Works best in** | Trending markets (strong bull/bear) | Sideways/choppy markets |
| **Fails in** | Sideways markets (whipsaw) | Strong trends (catches falling knives) |

This is why the bot lets you switch between them — no single strategy works in all conditions.

---

## Technical Indicators

These are the building blocks. Each one distills raw price data into a single number (or a few numbers) that tells you something about market conditions.

### Moving Averages (MA)

**What:** The average price over the last N bars. Smooths out noise to reveal the underlying trend direction.

**This bot uses Exponential Moving Averages (EMA)**, which weight recent prices more heavily than older ones. This makes them react faster to price changes than a simple average.

```python
# core/technical_analysis.py — analyze_trend()
s_ma = df['close'].ewm(span=self.ma_short_window, adjust=False).mean().iloc[-1]
l_ma = df['close'].ewm(span=self.ma_long_window, adjust=False).mean().iloc[-1]
```

- `ma_short_window=20` — reacts quickly (follows recent price action)
- `ma_long_window=50` — reacts slowly (represents the longer-term trend)

**How to read it:**
- Short MA > Long MA → price is trending up (recent prices higher than historical)
- Short MA < Long MA → price is trending down

**The 0.2% buffer:** The bot requires `short_ma > long_ma * 1.002` (not just `>`). Without this, tiny fluctuations around the crossover point cause rapid buy/sell signals ("whipsaw"). The buffer creates a dead zone:

```
        short MA crosses ABOVE long MA * 1.002 → BUY signal
  ─ ─ ─ ─ ─ ─ ─ ─ ─ long MA * 1.002 ─ ─ ─ ─ ─ ─ ─ ─
        (dead zone — no signal)
  ─ ─ ─ ─ ─ ─ ─ ─ ─ long MA * 0.998 ─ ─ ─ ─ ─ ─ ─ ─
        short MA crosses BELOW long MA * 0.998 → SELL signal
```

### Crossover Confirmation

**What:** Requires the MA crossover to hold for two consecutive bars before acting.

```python
# core/technical_analysis.py — is_crossover_confirmed()
current = s_ema.iloc[-1] > l_ema.iloc[-1] * buffer
previous = s_ema.iloc[-2] > l_ema.iloc[-2] * buffer
return current and previous
```

**Why:** A single bar can be noise (a wick, a large order). If the crossover holds for two bars, it's more likely to be a real trend change. This reduces false signals at the cost of slightly later entries.

### RSI (Relative Strength Index)

**What:** Measures how fast price is rising vs. falling, on a 0-100 scale.

```python
# core/technical_analysis.py — calculate_rsi()
# RS = average gain / average loss over 14 bars
# RSI = 100 - 100/(1 + RS)
```

**How to read it:**
- RSI > 70 → **overbought** — price rose too fast, likely to pull back
- RSI < 30 → **oversold** — price dropped too fast, likely to bounce
- RSI ~50 → neutral

**In the trend-following strategy:** RSI acts as a *filter*. Even if the MA says "buy", if RSI > 70 we skip it — buying an overbought asset often means buying the top.

```python
# strategies/trend_following.py — scan_entry()
rsi = self.ta.calculate_rsi(df)
if rsi is not None and rsi > float(self.config.rsi_overbought):
    return None  # Skip overbought
```

**In the mean-reversion strategy:** RSI is the *primary signal*. RSI < 30 means the asset is oversold and might bounce.

```python
# strategies/mean_reversion.py — scan_entry()
if rsi is None or rsi >= rsi_threshold:  # threshold = 30
    return None  # Not oversold enough
```

### ATR (Average True Range)

**What:** Measures volatility — how much price moves per bar, on average.

```python
# core/technical_analysis.py — calculate_atr()
# True Range = max of:
#   high - low           (normal bar range)
#   |high - prev close|  (gap up)
#   |low - prev close|   (gap down)
# ATR = average of True Range over 14 bars
```

**Why it matters:** A $100 stock that moves $5/day is very different from one that moves $0.50/day. ATR tells you which one you're dealing with, so you can set stops appropriately.

**In the bot:** ATR sets the trailing stop distance (see [Trailing Stop](#trailing-stop) below).

### Bollinger Bands

**What:** A channel around the moving average that expands and contracts with volatility.

```python
# core/technical_analysis.py — calculate_bollinger_bands()
middle = close.mean()           # 20-period SMA
std = close.std()               # Standard deviation
upper = middle + num_std * std  # Default: +2 std devs
lower = middle - num_std * std  # Default: -2 std devs
```

**How to read it:**
- Price near upper band → potentially overbought
- Price near lower band → potentially oversold
- Bands narrow → low volatility, breakout likely coming
- Bands wide → high volatility

Statistically, ~95% of price action stays within 2 standard deviations of the mean. When price breaks below the lower band, it's a statistical outlier — the mean-reversion strategy bets it will snap back.

```
  ━━━━━━━ Upper Band (SMA + 2σ) ━━━━━━━
       ~95% of price stays in here
  - - - - Middle (20-period SMA) - - - -
       ~95% of price stays in here
  ━━━━━━━ Lower Band (SMA - 2σ) ━━━━━━━
       ↑ Mean-reversion buys here
```

---

## Trend-Following Strategy

**File:** `strategies/trend_following.py`

### Entry Logic

The strategy requires ALL of these to be true:

1. **MA crossover:** Short EMA > Long EMA * 1.002 (uptrend)
2. **Confirmation:** Crossover held for 2 bars
3. **RSI filter:** RSI < 70 (not overbought)
4. **Volume filter:** 24h USD volume > $100,000 (liquidity check)
5. **Regime filter:** Market is BULL (or BTC in BEAR with exemption)

```python
# strategies/trend_following.py — scan_entry()
if not (ma_s and ma_l and ma_s > ma_l * 1.002 and allow_buy):
    return None
if not self.ta.is_crossover_confirmed(df, "bull"):
    return None
# ... volume filter ...
# ... RSI filter ...
return {"asset": asset, "product_id": product_id, "score": momentum}
```

**Candidate ranking:** Assets are ranked by **momentum** (24h price change %). The top 3 are bought. This is a "buy the strongest" approach — if BTC is up 5% and ETH is up 2%, BTC gets priority.

### Exit Logic (Priority Order)

The exits are checked in order. The first one that triggers wins.

#### 1. Take-Profit Level 2 (TP2)
```
Price >= entry * 1.20 (20% gain) → sell 50%
```
Lock in half the position at a significant profit. The remaining 50% rides for more upside.

#### 2. Take-Profit Level 1 (TP1)
```
Price >= entry * 1.10 (10% gain) → sell 33%
```
Take some profit early. After TP1, the trailing stop is raised to breakeven (see below).

#### 3. Trailing Stop (ATR-based)

This is the most sophisticated exit. Instead of a fixed percentage stop, it adapts to volatility:

```python
# strategies/trend_following.py — check_exit()
atr_stop = 2.5 * atr / price
atr_stop = max(0.03, min(0.15, atr_stop))  # Clamp to 3%-15%
stop_price = hwm * (1 - atr_stop)
```

**How it works:**
1. Calculate `atr_stop = 2.5 * ATR / price` — this is the stop distance as a fraction of price
2. Clamp it between 3% and 15% (avoids extremes)
3. The stop trails the **High Water Mark** (HWM), not the entry price

```
  Price:  $100 → $110 → $115 → $108 → $105
  HWM:   $100    $110    $115    $115    $115  (only goes up)
  Stop:   $93    $102    $106    $106    $106  (follows HWM)
                                         ↑ sell triggers here if ATR stop = 8%
```

**Why 2.5x ATR?** This tolerates ~2.5 bars of adverse movement before triggering. If the asset typically moves $4/bar (ATR=4), the stop is $10 below HWM. This avoids getting stopped out by normal volatility while still protecting against real reversals.

**Breakeven protection:** After TP1 is hit, the stop floor is raised to the entry price:

```python
if tp_flags.get("tp1_hit", False) and entry > stop_price:
    stop_price = entry
```

This guarantees you can't lose money on the remaining position after taking initial profits.

#### 4. Trend Exit
```
Short MA < Long MA * 0.998 → sell 50%
```
If the trend reverses (bearish crossover), sell half. This only fires once per position to avoid repeatedly selling on a choppy crossover.

### Why This Order Matters

The priority system prevents conflicting signals:
- If price is at +25%, TP2 fires (sell 50%) — you don't want the trailing stop interfering
- If price is at +12%, TP1 fires (sell 33%) — the trailing stop takes over the remaining position
- If price never hit any TP and drops, the trailing stop is your safety net
- Trend exit is last resort — a slower signal that catches extended reversals

---

## Mean-Reversion Strategy

**File:** `strategies/mean_reversion.py`

### The Core Idea

Markets oscillate. When an asset drops sharply (oversold), it often bounces back to its average. This strategy buys the dip and sells the bounce.

### Entry Logic

Requires ALL of these:

1. **RSI < 30** — asset is oversold
2. **Price < Lower Bollinger Band** — confirms the drop is statistically extreme
3. **Volume filter** — same liquidity check as trend-following
4. **Not in BEAR/STRONG_BEAR** — avoids catching falling knives in strong downtrends

```python
# strategies/mean_reversion.py — scan_entry()
rsi = self.ta.calculate_rsi(df)
if rsi is None or rsi >= rsi_threshold:
    return None  # Not oversold

bb = self.ta.calculate_bollinger_bands(df, period=bb_period, num_std=bb_std)
if bb is None:
    return None
_middle, _upper, lower = bb
if current_price >= lower:
    return None  # Not below lower band
```

**Why both RSI AND Bollinger?** Either signal alone has too many false positives. RSI < 30 can happen during a slow grind down. Price below lower BB can happen during a sudden but temporary spike in volatility. Together, they confirm: "this asset dropped fast AND is at a statistical extreme."

**Candidate ranking:** Ranked by RSI ascending — the most oversold asset gets priority.

### Exit Logic

Much simpler than trend-following — three checks, each sells the full position:

#### 1. Mean-Reversion Target
```python
sma = self.ta.calculate_sma(df, period=20)
if price >= sma:
    return True, 1.0, "Mean reversion target reached"
```
Price returned to the 20-period average. The trade thesis is complete.

#### 2. Stop Loss (5% from HWM)
```python
stop_price = hwm * (1 - stop_pct)  # 5% fixed stop
if price < stop_price:
    return True, 1.0, "Mean reversion stop loss"
```
A fixed stop, not ATR-based. Mean-reversion trades are shorter-duration, so a simpler stop is appropriate.

#### 3. Time Exit (24 candles)
```python
elapsed_candles = (time.time() - entry_time) / 3600
if elapsed_candles >= max_candles:  # 24 hours
    return True, 1.0, "Mean reversion time exit"
```
If the bounce hasn't happened in 24 hours, the thesis is wrong. Exit and move on. This is critical — without it, a failed mean-reversion trade can turn into an indefinite bag-hold.

### Why No Shorts?

Mean-reversion shorts (selling overbought assets expecting a pullback) are much riskier than longs. A stock can only fall to $0 (bounded loss on longs), but can rise indefinitely (unbounded loss on shorts). The strategy avoids this asymmetry.

---

## Risk Management

Risk management is what keeps a bot alive. The entry/exit signals can be wrong half the time and still be profitable if losses are kept small.

### Position Sizing

```python
# trading_bot.py
trade_limit = ex_value * PORTFOLIO_RISK_PERCENTAGE  # 15% of portfolio
buy_size = min(
    available_usdc * RISK_PER_TRADE_PCT / TOP_MOMENTUM_COUNT,
    trade_limit / TOP_MOMENTUM_COUNT
)
```

**Two caps:**
1. Don't spend more cash than you have (obviously)
2. Don't allocate more than 15% of portfolio to new trades per cycle

Splitting across `TOP_MOMENTUM_COUNT=3` candidates means each trade gets ~5% of portfolio — a single bad trade can't blow up the account.

### Max Position Size

```python
# Dynamic per-asset position cap: portfolio_value / max_positions.
# Scales with account size instead of a fixed dollar amount.
dynamic_max_position = ex_value / max(1, max_positions)
current_asset_value = held_total.get(asset, 0) * price
if current_asset_value + buy_size > dynamic_max_position:
    buy_size = max(0, dynamic_max_position - current_asset_value)
```

Even if the signals keep saying "buy BTC", each position is capped at a percentage of the portfolio (e.g., 33% if max positions = 3). This prevents concentration risk while allowing the bot to scale as the account grows.

### Drawdown Guard

```python
drawdown_pct = ((peak - ex_value) / peak * 100)
if drawdown_pct >= MAX_DRAWDOWN_PCT:  # 10%
    # Skip new buys
```

If the portfolio drops 10% from its all-time high, stop buying. This prevents compounding losses during a crash.

### Volume Filter

```python
usd_volume_24h = volume_24h * close_price
if usd_volume_24h < MIN_24H_VOLUME_USD:  # $100,000
    continue
```

Low-volume assets have wide bid-ask spreads and are easy to manipulate. The volume filter avoids them.

### Fee Awareness

```python
fee_cost = entry * sell_amount * ROUND_TRIP_FEE_PCT  # 0.6%
pnl = (exit_price - entry) * sell_amount - fee_cost
```

Every trade costs ~0.6% in fees (0.3% buy + 0.3% sell). A 1% gain is really only 0.4% after fees. The bot tracks this to avoid showing phantom profits.

---

## Market Regime Detection

**File:** `core/regime_detection.py`

The bot determines the overall market state before running strategy logic. This prevents the trend-following strategy from buying in a bear market (where trends are down) and the mean-reversion strategy from buying in a strong bear (where dips keep dipping).

### Single-Asset Mode

Uses BTC's MA crossover as a proxy for the whole market:
- BTC short MA > long MA * 1.002 → **BULL**
- BTC short MA < long MA * 0.998 → **BEAR**
- In between → defaults to BULL

### Dual-Signal Mode

Combines two signals for a richer picture:

1. **BTC Macro Trend** — is BTC trending up or down?
2. **ETH/BTC Ratio** — is money flowing from BTC into altcoins (risk-on) or back to BTC (risk-off)?

These combine into five states: `STRONG_BULL`, `BULL`, `NEUTRAL`, `BEAR`, `STRONG_BEAR`.

Each strategy responds differently:
- **Trend-following** skips `NEUTRAL` (sideways = whipsaw losses)
- **Mean-reversion** skips `BEAR` and `STRONG_BEAR` (falling knives)

---

## Further Reading

### Books
- **"Technical Analysis of the Financial Markets"** by John Murphy — the standard reference for indicators (MA, RSI, Bollinger Bands)
- **"Trading and Exchanges"** by Larry Harris — how markets actually work (order books, market microstructure)
- **"Quantitative Trading"** by Ernie Chan — practical quant strategies in Python

### Concepts to explore next
- **Backtesting** — running your strategy against historical data to see how it would have performed
- **Sharpe Ratio** — measuring risk-adjusted returns (a 10% return with 5% volatility is better than 10% with 20% volatility)
- **Kelly Criterion** — optimal position sizing based on your win rate and average win/loss
- **Correlation** — if all your positions move together, you're not really diversified
- **Slippage** — the difference between the price you wanted and the price you got (this bot uses limit orders to minimize it)
