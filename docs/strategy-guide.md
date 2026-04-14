# Trading Strategy Guide

A walkthrough of everything this bot does — the financial concepts, the algorithms, the risk management, the derivatives signals, and how on-chain execution works on Base.

---

## Table of Contents

1. [How Markets Work](#how-markets-work)
2. [Technical Indicators](#technical-indicators)
3. [Market Regime Detection](#market-regime-detection)
4. [Trend-Following Strategy](#trend-following-strategy)
5. [Mean-Reversion Strategy](#mean-reversion-strategy)
6. [Supertrend Strategy](#supertrend-strategy)
7. [Risk Management](#risk-management)
8. [Derivatives Market Signals](#derivatives-market-signals)
9. [On-Chain Execution (Base / Uniswap V3)](#on-chain-execution)
10. [Further Reading](#further-reading)

---

## How Markets Work

Before anything else, it helps to understand what the bot is actually doing in the market.

### Spot vs. Derivatives Markets

The bot trades **spot markets** — it buys actual BTC and ETH with USDC, and sells them later. There is no leverage. If it buys $1,000 of BTC, it holds $1,000 of BTC.

**Derivatives markets** (futures, perpetual swaps) are a parallel market where traders make leveraged bets on price direction without owning the underlying asset. These markets are useful to *observe* because they reveal how much leverage traders are using and how confident they are in a direction.

### Order Books and Limit Orders

Every trade happens in an **order book**: a list of buyers willing to pay a specific price (bids) and sellers willing to accept a price (asks). The gap between the best bid and ask is the **spread**.

The bot uses **limit orders** — it says "I'll buy at exactly $X" instead of "buy at whatever the current price is." This avoids paying the spread (you become the maker, not the taker) and gets better execution, especially on Coinbase where maker fees are significantly lower.

### Bid-Ask Spread and Slippage

When you place a market order, you buy at the ask price. The moment you own it, the asset is only worth the bid price. That difference is immediate slippage. On a liquid pair like BTC/USDC, the spread is a few cents on a $90,000 asset — negligible. On thin altcoin markets, it can be 0.5-1%.

### Fees

The bot uses Coinbase Advanced Trade with Coinbase One, which gives maker rates of ~0.075% per side (0.15% round-trip). This is baked into all PnL calculations. A trade needs to move more than 0.15% in your favor just to break even on fees.

---

## Technical Indicators

These distill raw price data into signals. Each one measures something specific about market conditions.

### Moving Averages (EMA)

**What:** The average price over N bars, with recent prices weighted more heavily.

```python
# core/technical_analysis.py — analyze_trend()
s_ma = df['close'].ewm(span=21, adjust=False).mean().iloc[-1]  # EMA21
l_ma = df['close'].ewm(span=55, adjust=False).mean().iloc[-1]  # EMA55
```

The bot uses **EMA21 and EMA55** — a Fibonacci pair. EMA21 responds quickly to recent price action. EMA55 follows the longer-term trend. When EMA21 crosses above EMA55, the recent trend is outpacing the historical trend — a bullish signal.

**The 0.2% buffer** prevents "whipsaw" — rapid buy/sell signals when price oscillates near the crossover:

```
       EMA21 > EMA55 * 1.002 → BULL signal
  ─ ─ ─ ─ ─ ─ EMA55 * 1.002 ─ ─ ─ ─ ─ ─
        (dead zone — no signal)
  ─ ─ ─ ─ ─ ─ EMA55 * 0.998 ─ ─ ─ ─ ─ ─
       EMA21 < EMA55 * 0.998 → BEAR signal
```

**Crossover confirmation** requires the crossover to hold for two consecutive bars before acting. A single bar can be noise; two bars suggests a real trend change.

### RSI (Relative Strength Index)

**What:** How fast price is rising vs. falling, scaled 0-100.

```
RSI = 100 - 100 / (1 + avg_gain / avg_loss)  [14-bar window]
```

- RSI > 70: **overbought** — price rose too fast, likely to pull back
- RSI < 30: **oversold** — price dropped too fast, likely to bounce
- RSI ~50: neutral

**In trend-following:** RSI is a filter. Even if the MA says "buy", if RSI > 75 (or higher in bull regimes) we skip — you're likely buying a top.

**In mean-reversion:** RSI is the primary signal. RSI < 30 means the asset is oversold and may bounce.

**Regime-adaptive RSI thresholds:**

| Regime | RSI Limit | Why |
|--------|-----------|-----|
| STRONG_BULL | 88 | Very overbought OK in euphoria |
| BULL | 82 | Normal bull market tolerance |
| NEUTRAL | 75 | Tighter in sideways markets |
| BEAR | 70 | Tight — momentum must be strong |

### ATR (Average True Range)

**What:** Average size of each bar's range (high - low), accounting for gaps.

```
True Range = max(high - low, |high - prev_close|, |low - prev_close|)
ATR = average of True Range over 14 bars
```

ATR measures **volatility**. It's used to set the trailing stop distance: in a volatile market, you give the position more room to breathe; in a calm market, you tighten stops to protect gains.

### Bollinger Bands

A channel around the 20-period moving average, at ±2 standard deviations:

```
Upper Band = SMA20 + 2 × std
Lower Band = SMA20 - 2 × std
```

~95% of price action stays inside the bands. Price breaking below the lower band is a statistical outlier — the mean-reversion strategy bets on a snap-back to the middle.

### Momentum

Simply the 24-hour price change percentage:

```python
momentum = (current_close / close_24h_ago - 1) * 100
```

Used to **rank candidates**: if BTC is up 5% and ETH is up 2% over the last 24h, BTC has stronger momentum and gets prioritized.

---

## Market Regime Detection

**File:** `core/regime_detection.py`

Before any entry/exit decisions, the bot classifies the overall market state. This prevents trend-following from buying in bear markets (where trends are down) and mean-reversion from buying in strong bear markets (where dips keep dipping).

### Two Signals, Five States

The bot combines two independent signals:

**Signal 1: BTC Macro Trend**
```
EMA21 > EMA55 * 1.002 → BULL
EMA21 < EMA55 * 0.998 → BEAR
Otherwise            → FLAT
```

**Signal 2: ETH/BTC Rotation**
Uses EMA21 of the ETH/BTC ratio:
```
ETH/BTC EMA21 > EMA55 * 1.003 → ETH_LEADING  (risk-on, alts outperforming)
ETH/BTC EMA21 < EMA55 * 0.997 → BTC_LEADING  (risk-off, capital retreating to BTC)
Otherwise                       → NEUTRAL_RATIO
```

These combine into five regimes:

| Regime | BTC Trend | Rotation | Meaning |
|--------|-----------|----------|---------|
| STRONG_BULL | BULL | ETH_LEADING | Full risk-on, alts rallying |
| BULL | BULL | BTC_LEADING or NEUTRAL | BTC uptrend, cautious positioning |
| NEUTRAL | FLAT | Any | Sideways market |
| BEAR | BEAR | Any | Downtrend |
| STRONG_BEAR | BEAR | ETH_LEADING | Alts dumping harder than BTC |

### The 3-Confirmation Hysteresis

Regime changes require **3 consecutive signals** before they take effect (`trading_bot.py:846-866`). This prevents whipsawing between regimes when signals are noisy.

```
State stored in trading_state.json:
  confirmed_regime: "BEAR"      ← what the bot currently acts on
  regime_streak: 2              ← signals pointing to new regime (not confirmed yet)
```

If the raw signal says "BULL" but the confirmed regime is "BEAR", the bot keeps trading in BEAR mode until the BULL signal appears 3 times in a row — then switches.

**Why this matters:** Without hysteresis, a single choppy candle could flip the regime and cause the bot to buy into a trend that immediately reverses. The streak requirement filters out noise.

### Strategy Selection by Regime

```python
# trading_bot.py — select_strategy_for_regime()
STRONG_BULL / BULL  → trend_following
NEUTRAL             → mean_reversion
BEAR / STRONG_BEAR  → trend_following  (with bear-specific entry logic)
```

---

## Trend-Following Strategy

**File:** `strategies/trend_following.py`

### Core Idea

"Rising prices keep rising." In a trend, momentum has inertia. This strategy buys when a bullish trend is confirmed and exits when the trend ends.

### Bull Market Entry

Requires ALL of the following:
1. EMA21 > EMA55 × 1.002 (uptrend confirmed)
2. Crossover held for 2 consecutive bars
3. RSI below the regime threshold (not overbought)
4. 24h USD volume > $500,000 (liquid market)
5. Regime is BULL or STRONG_BULL

### Bear Market Entry: `_bear_momentum_entry()`

In BEAR regime, MA crossover is meaningless (EMA21 is structurally below EMA55). Instead, the bot uses a **momentum-based** entry:

```python
# strategies/trend_following.py — _bear_momentum_entry()
momentum_24h = (current_close / close_24h_ago - 1) * 100
if momentum_24h < 2.0:    return None  # Weak bounce, skip
if rsi > 70:              return None  # Overbought
if rsi < 35:              return None  # Too oversold, still dumping
if volume < threshold:    return None  # Low conviction
# All pass → bear momentum entry at 25% position size
```

This catches short-lived relief rallies in bear markets. Position size is reduced to 25% (default `BEAR_POSITION_SCALE=0.25`) to limit exposure. A bear rally can reverse quickly.

### Exit Logic (Priority Order)

Exits are checked in order. The first one that triggers wins.

#### 1. Take-Profit Level 2 (TP2)
```
Price ≥ entry × 1.40 → sell 35%
```
Lock in 35% of the position at 40% gain. The rest rides.

#### 2. Take-Profit Level 1 (TP1)
```
Price ≥ entry × 1.15 → sell 25%
```
Take some profit at 15% gain. After TP1, the trailing stop floor rises to breakeven (can't lose money on the remaining position).

#### 3. ATR Trailing Stop

```python
atr_stop = 2.5 * atr / price        # Volatility-scaled distance
atr_stop = max(0.03, min(0.15, atr_stop))  # Clamped 3%-15%
stop_price = high_water_mark * (1 - atr_stop)
```

The stop trails the **High Water Mark (HWM)** — the highest price seen since entry:

```
Price:  $100 → $110 → $115 → $108 → $105
HWM:    $100    $110    $115    $115    $115  (never goes down)
Stop:    $93    $102    $106    $106    $106  (follows HWM down)
                                       ↑ fires here (8% ATR stop)
```

**Why 2.5× ATR?** If the asset moves $4/bar on average (ATR=$4), a stop at $10 below HWM tolerates ~2.5 bars of adverse movement. This avoids being stopped out by normal volatility while still catching real reversals.

#### 4. Trend Exit
```
EMA21 < EMA55 × 0.998 → sell 50%
```
The trend reversed. Sell half, let the trailing stop handle the rest.

---

## Mean-Reversion Strategy

**File:** `strategies/mean_reversion.py`

### Core Idea

"Extreme moves bounce back." When an asset drops sharply to a statistical extreme, it often bounces to its average. This strategy buys the dip and sells the recovery.

### Entry Logic

Requires ALL of:
1. RSI < 30 (oversold)
2. Price < Lower Bollinger Band (statistically extreme)
3. Volume > $500,000
4. Regime is NOT BEAR or STRONG_BEAR (avoids catching falling knives)

**Why both RSI AND Bollinger?** Each filters different false positives. RSI < 30 can happen during a slow grind down. Price below the lower BB can happen from a sudden volatility spike. Together they confirm: fast drop AND statistical extreme.

### Exit Logic

Simpler than trend-following — exits the full position on any trigger:

1. **Mean-reversion target:** Price ≥ 20-period SMA. The bounce worked, thesis complete.
2. **Stop loss:** Price < HWM × 0.92 (8% trailing stop). Thesis was wrong.
3. **Time exit:** Position held for 24+ bars (candles). Bounce hasn't happened — exit and move on.

The time exit is critical: without it, a failed mean-reversion trade becomes a bag-hold that ties up capital indefinitely.

---

## Supertrend Strategy

**File:** `strategies/supertrend.py`

### Core Idea

A volatility-based trend indicator that plots a dynamic support/resistance line using ATR. Unlike MA crossovers, it adapts to volatility in real time.

```
Basic Upper Band = (high + low)/2 + 3 × ATR(10)
Basic Lower Band = (high + low)/2 - 3 × ATR(10)
```

- Bullish: price above the band, band acts as a trailing stop below
- Bearish: price below the band, band acts as resistance above

### Entry and Exit

**Entry:** Supertrend flips bullish AND held bullish for 2+ bars  
**Primary exit:** Supertrend flips bearish  
**Backup exit:** ATR trailing stop (same formula as trend-following)

### vs. MA Trend-Following

| | Supertrend | MA Crossover |
|--|--|--|
| Adapts to volatility | Yes | No |
| Lag | Less (responds faster) | More (smoothed) |
| False signals in chop | More | Fewer |
| Best for | Volatile trending markets | Smooth, extended trends |

---

## Risk Management

**File:** `core/risk_manager.py`

Risk management is what keeps the bot alive. Signals can be wrong half the time and the bot still profits — if losses are capped and wins are allowed to run.

### Position Sizing

```python
# trading_bot.py
buy_size = min(
    available_usdc * RISK_PER_TRADE_PCT / TOP_MOMENTUM_COUNT,
    ex_value * PORTFOLIO_RISK_PCT / TOP_MOMENTUM_COUNT,
    dynamic_max_per_position,
)
```

**Key constraints:**
- Don't spend more than 90% of available cash
- Each position ≤ portfolio / max_positions (equal-weight, scales with account)
- Max 3 concurrent positions (concentration guard)

**BEAR scaling:** In BEAR regime, `buy_size *= 0.25` (configurable via `BEAR_POSITION_SCALE`). This was backtested as the optimal bear market exposure — allows participation in bear rallies while limiting downside if the rally fails.

### Drawdown Guard

```python
drawdown_pct = (peak_value - current_value) / peak_value * 100
if drawdown_pct >= 15:
    # Skip all new buys until recovery
```

If the portfolio drops 15% from its all-time high, the bot stops buying entirely until recovery. This prevents compounding losses during a crash by sitting in cash.

After a 48-hour cooldown with recovery, normal trading resumes.

### Volume Filter

```python
if usd_volume_24h < 500_000:
    continue  # Skip illiquid assets
```

Low-volume assets have wide spreads, are easy to manipulate, and have poor price discovery. The $500K daily volume floor ensures trades are in liquid, well-priced markets.

### Fee Accounting

Coinbase Advanced maker rate: **0.075% per side**, ~0.15% round-trip. All PnL calculations subtract fees. A 1% price gain is ~0.85% net. The bot also avoids trades where the expected gain (from momentum and regime) doesn't clearly exceed the fee hurdle.

---

## Derivatives Market Signals

**File:** `core/derivatives_data.py`

This is an **opt-in layer** that uses perpetual futures market data from OKX to modify position sizing and filter entries. Enable with `ENABLE_DERIVATIVES_SIGNALS=true`.

The signals come from the **derivatives market** — where traders use leverage to bet on BTC price direction. This market reveals positioning and sentiment that price alone doesn't show.

### What Are Perpetual Futures?

A perpetual swap is like a futures contract that never expires. Traders can go long (bet price rises) or short (bet price falls) with leverage — e.g., 10× leverage means a 1% price move creates a 10% gain or loss on capital.

Because the perp can trade at a different price than spot, exchanges use a **funding rate** to keep them aligned:
- If the perp trades *above* spot → longs pay shorts (positive funding). Longs are paying a premium to stay in their position.
- If the perp trades *below* spot → shorts pay longs (negative funding). Shorts are crowded.

### Signal 1: Funding Rate

**Source:** OKX perpetual BTC-USD-SWAP, settled every 8 hours.  
**Endpoint:** `GET https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USD-SWAP&limit=3`

The bot averages the last 3 settlements (24 hours) to get a smoothed signal:

| Rate (24h avg) | Signal | Position Modifier | Interpretation |
|----------------|--------|-------------------|----------------|
| < -0.05% | EXTREME_NEGATIVE | ×1.25 | Capitulation — shorts crowded, potential bottom |
| -0.05% to -0.01% | NEGATIVE | ×1.10 | Mild capitulation |
| -0.01% to +0.03% | NORMAL | ×1.00 | Neutral |
| +0.03% to +0.05% | ELEVATED | ×0.75 | Overleveraged longs — caution |
| > +0.05% | EXTREME | ×0.50 | Dangerous leverage buildup — reduce exposure |

**Why this matters:** When funding is extreme positive (>0.05%), longs are paying 0.05% every 8 hours (≈22% annually) to hold their position. This creates enormous pressure to close. A wave of closing longs is a potential cascade of selling. The bot reduces position size to protect against this.

When funding is extreme negative, shorts are the crowded trade. A short squeeze can send prices sharply higher as shorts are forced to buy to close.

**In the Jan-Mar 2026 backtested window**, funding was normal 98.7% of the time — meaning the derivatives market wasn't generating excess leverage signals. The modifier had almost no effect. This is correct behavior: the signal is designed for extreme conditions.

### Signal 2: Open Interest (OI) Divergence

**Source:** OKX, hourly.  
**Endpoint:** `GET https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?ccy=BTC&period=1H`

**Open interest** = total dollar value of all outstanding futures contracts. It measures how much capital is committed to leveraged positions.

**The key pattern — bearish divergence:**

| Price Action | OI Action | Interpretation | Bot Response |
|---|---|---|---|
| Rising | Rising | New money entering, confirmed trend | Allow entries |
| Rising | Falling | Longs taking profit/closing, weak rally | **Block entries** |
| Falling | Rising | Short buildup, squeeze risk | Allow entries |
| Falling | Falling | Capitulation, longs liquidated | Allow entries |

"Price rising but OI falling" is the signal the bot acts on. This means the rally is running on old positions being closed, not on new conviction entering. These rallies tend to fade. The bot blocks new entries when BTC is up >1% in 24h but OI has fallen >5%.

### Signal 3: Long/Short Ratio

**Source:** OKX, hourly.  
**Endpoint:** `GET https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy=BTC&period=1H`

The ratio of accounts that are net long vs. net short. A contrarian signal:

| Long Ratio | Signal | Modifier |
|---|---|---|
| > 65% | EXTREME_LONG | ×0.75 |
| 35%-65% | NEUTRAL | ×1.00 |
| < 35% | EXTREME_SHORT | ×1.00 (no action) |

When 65%+ of accounts are long, the crowd is positioned for a rally. Historically, when everyone is already long, there's nobody left to buy. The bot reduces position size as a contrarian caution flag.

### How They Compose

Position modifiers multiply together and are clamped to [0.25, 1.25]:

```
final_modifier = funding_modifier × ls_ratio_modifier
final_modifier = clamp(final_modifier, 0.25, 1.25)
buy_size *= final_modifier  (after bear_position_scale)
```

Example: Extreme funding (×0.50) + extreme long ratio (×0.75) = ×0.375, clamped to ×0.25 minimum.

### Historical Data

OKX provides:
- **Funding rates:** ~3 months of history (8h resolution, paginated)
- **Open interest:** ~30 days (1h resolution)
- **Long/short ratio:** ~30 days (1h resolution)

To build more backtest history, run the collector hourly:
```bash
python backtesting/collect_derivatives_data.py
```
Data is saved to `data/derivatives/` and appended without duplicates.

---

## On-Chain Execution

**File:** `executors/ethereum_executor.py`

The bot can trade on two venues:
1. **Coinbase** (`coinbase_executor.py`) — centralized exchange, traditional limit/market orders
2. **Base / Uniswap V3** (`ethereum_executor.py`) — decentralized exchange on an L2 blockchain

This section explains how the on-chain path works.

### What Is Base?

[Base](https://base.org) is an **Ethereum Layer 2** (L2) blockchain developed by Coinbase. It uses **Optimistic Rollup** technology:

- Transactions are processed off-chain in batches
- Batches are periodically committed to Ethereum mainnet (the "L1")
- Result: Ethereum's security with 10-100× lower gas fees and faster finality

The bot interacts with Base via [web3.py](https://web3py.readthedocs.io/), connecting to an RPC endpoint (e.g., from Coinbase or Alchemy).

### What Is Uniswap V3?

Uniswap V3 is a **decentralized exchange** (DEX) — a set of smart contracts on Base that allow anyone to swap tokens without a central intermediary.

Unlike a traditional exchange with an order book, Uniswap uses an **Automated Market Maker (AMM)**:
- Liquidity providers deposit token pairs into a pool
- The pool prices swaps using the formula: `x × y = k` (constant product)
- As you buy BTC (WBTC), the pool has more USDC and less WBTC — price rises

**V3 improvement:** Liquidity providers can concentrate liquidity in a specific price range, making the pool more capital-efficient. The trade-off: if price moves outside the range, the LP earns no fees.

### Key Contracts on Base

| Contract | Address | Purpose |
|----------|---------|---------|
| SwapRouter02 | `0x2626664c2603336E57B271c5C0b26F421741e481` | Routes swaps through V3 pools |
| QuoterV2 | `0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a` | Simulates swaps off-chain to get a quote |
| V3 Factory | `0x33128a8fC17869897dcE68Ed026d694621f6FDfD` | Tracks all pools |
| USDC | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | Native USDC (6 decimals) |
| WETH | `0x4200000000000000000000000000000000000006` | Wrapped ETH (18 decimals) |
| WBTC | `0x0555E30da8f98308EdB960aa94C0Db47230d2B9c` | Wrapped BTC, bridged via LayerZero (8 decimals) |

**Decimal note:** ERC-20 tokens store amounts as integers. To represent $1.00 USDC, the contract stores `1,000,000` (6 decimals). To represent 1 ETH (WETH), it stores `1,000,000,000,000,000,000` (18 decimals). The executor converts between human amounts and raw amounts.

### A Swap Step by Step

When the bot executes a swap on Base, here's what happens:

```
1. QUOTE: Call QuoterV2.quoteExactInput() to get expected output amount (off-chain)
2. APPROVE: If this is the first swap of this token, call token.approve(router, amount)
3. SWAP: Call SwapRouter02.exactInput(path, amountIn, amountOutMin, deadline)
4. POOL: Router calls the Uniswap V3 pool which executes the swap
5. RECEIPT: Transaction hash returned, bot waits for confirmation
```

**Why approve first?** ERC-20 tokens require the owner to explicitly allow a contract to spend them (`approve()`). Without this, the router can't pull your USDC. This is a one-time call per token per wallet.

**Slippage protection:** The `amountOutMin` parameter is set based on the QuoterV2 quote minus 0.5-1%. If the actual swap would give you less than this (due to price movement between quote and execution), the transaction reverts. This protects against MEV (maximal extractable value) attacks where bots front-run your swap.

### Finding the Best Route

The bot uses a dynamic route-finding approach:

1. **Single-hop first:** Try `USDC → WBTC` directly via all fee tiers (0.05%, 0.30%, 1.00%)
2. **Multi-hop fallback:** If no direct pool exists or liquidity is too thin, route through WETH: `USDC → WETH → WBTC`
3. **Cache pool addresses** to avoid redundant RPC calls

The 0.30% fee tier has the deepest liquidity for major pairs on Base.

### On-Chain vs. Coinbase: Trade-offs

| | Coinbase | Base / Uniswap |
|--|--|--|
| Custody | Coinbase holds your assets | You hold your keys |
| Fees | 0.075% maker / 0.15% taker | Uniswap fee (0.05-1%) + gas |
| Speed | Instant fill (order book) | ~2 seconds (L2 block time) |
| Slippage | Minimal (deep order book) | Depends on pool liquidity |
| Available assets | Coinbase listings | Any ERC-20 on Base |
| Liquidation risk | None (spot only) | None (spot only) |
| Trust model | Trust Coinbase | Trust Ethereum/Base consensus |

---

## Backtesting

**Files:** `backtesting/backtest.py`, `backtesting/comprehensive_backtest.py`

Backtesting simulates the strategy on historical data to measure what performance *would have been*. The bot has 5 test periods covering different market conditions:

| Period | Range | Market Type |
|--------|-------|-------------|
| Q2_2023_Sideways | Mar-Jun 2023 | Choppy/sideways |
| 2023_Full_Year | All 2023 | Mixed (recovery) |
| Q1_2024_Bull | Jan-Mar 2024 | Strong bull ($42k→$61k) |
| H2_2024 | Jul-Dec 2024 | Mid-year to EOY |
| YTD_2025 | Jan 2025 - Mar 2026 | Recent market |

**Key metrics:**

| Metric | What It Measures |
|--------|-----------------|
| Total Return % | Raw PnL |
| Sharpe Ratio | Return per unit of risk (higher = better) |
| Max Drawdown | Worst peak-to-trough decline |
| Win Rate | % of trades that were profitable |

**Current defaults (backtested optimal as of April 2026):**
- Strategy: `trend_following` (not `auto` — see MEMORY.md)
- MA: EMA21 / EMA55 (vs. old 20/100 or 50/200)
- Bear scale: 0.25 (25% position size in bear regime)
- Momentum-based bear entry enabled

### Derivatives Backtest Limitation

The `backtesting/backtest_derivatives.py` script compares performance with vs. without derivatives signals. **Important caveat:** OKX provides only ~30-90 days of history for OI/funding data. This means:

1. The backtested window is short (low statistical significance)
2. In the Jan-Mar 2026 test window, funding was normal 98.7% of the time — the signals had almost no effect because there was no extreme leverage present
3. The signals are designed for extreme market conditions (funding >0.05% or <-0.05%) which are relatively rare

To build more history: run `collect_derivatives_data.py` hourly via cron/systemd. Each run appends new rows to `data/derivatives/` without duplicates.

---

## Further Reading

### Books (Financial Concepts)
- **"Technical Analysis of the Financial Markets"** — John Murphy. The standard reference for indicators.
- **"Trading and Exchanges"** — Larry Harris. How markets actually work (order books, market microstructure, why spreads exist).
- **"Quantitative Trading"** — Ernie Chan. Practical strategies in Python. Directly relevant to this codebase.

### Books (Crypto/Blockchain)
- **"Mastering Ethereum"** — Andreas Antonopoulos & Gavin Wood. Free online. The EVM, smart contracts, gas.
- **"DeFi and the Future of Finance"** — Campbell Harvey et al. AMMs, liquidity pools, how Uniswap works.

### Papers
- Aloosh & Bezbradica (2024): Funding rates outperform social sentiment 3-5× in Sharpe ratio
- Bouri et al. (2022): Fear & Greed index is a coincident indicator (lagging), not predictive
- Corbet et al. (2020): Twitter sentiment has statistically significant but economically small effect on BTC returns

### Concepts to Explore Next
- **Kelly Criterion** — optimal position sizing based on win rate and payoff ratio
- **Information Ratio** — Sharpe ratio relative to a benchmark (e.g., buy-and-hold BTC)
- **Execution algorithms** — TWAP, VWAP: how institutional traders split large orders to minimize impact
- **MEV (Maximal Extractable Value)** — how bots front-run on-chain transactions and how to defend against it
- **Liquidity provider mechanics** — impermanent loss in AMMs, concentrated liquidity
