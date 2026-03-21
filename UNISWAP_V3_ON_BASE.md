# Uniswap V3 Swaps on Base — How It Works

## Overview

This bot swaps ERC-20 tokens on Base using Uniswap V3 through the `EthereumExecutor`
class in `ethereum_executor.py`. A single swap goes through these stages:

```
place_market_order (human amounts)
  → execute_swap (raw amounts, on-chain)
    → approve (once per token)
    → exactInput (router call)
    → Pool.swap (router calls pool)
    → Pool calls router callback for token transfer
```

## Contracts on Base

| Contract | Address | Purpose |
|----------|---------|---------|
| SwapRouter02 | `0x2626664c2603336E57B271c5C0b26F421741e481` | Routes swaps through V3 pools |
| QuoterV2 | `0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a` | Simulates swaps off-chain to get quotes |
| V3 Factory | `0x33128a8fC17869897dcE68Ed026d694621f6FDfD` | Deploys and tracks pools |
| USDC | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | Native USDC (6 decimals) |
| WETH | `0x4200000000000000000000000000000000000006` | Wrapped ETH (18 decimals) |
| WBTC | `0x0555E30da8f98308EdB960aa94C0Db47230d2B9c` | LayerZero bridged BTC (8 decimals) |

### Pools Used

Pools are identified by `(token0, token1, fee)` where token0 is the lower address:

| Pool | Address | Fee | token0 | token1 |
|------|---------|-----|--------|--------|
| WETH/USDC 0.3% | `0x6c561B446416E1A00E8E93E221854d6eA4171372` | 3000 | WETH | USDC |
| WBTC/USDC 0.3% | `0x49e30c322E2474B3767de9FC4448C1e9ceD6552f` | 3000 | WBTC | USDC |

Fee tiers: 500 = 0.05%, 3000 = 0.30%, 10000 = 1.00%.
The 0.3% tier has the deepest liquidity for major pairs on Base.

## The `exactInput` Function

The Base SwapRouter02 uses `exactInput` with selector `0xb858183f` and a 4-field struct:

```solidity
function exactInput(ExactInputParams params) external returns (uint256 amountOut);

struct ExactInputParams {
    bytes path;          // packed route: tokenA + fee(3 bytes) + tokenB
    address recipient;   // who receives the output tokens
    uint256 amountIn;    // how much to sell (in token's smallest unit)
    uint256 amountOutMinimum;  // revert if output < this (slippage guard)
}
```

Note: This is different from the "standard" `exactInputSingle` (selector `0x414bf389`,
8-field struct) that other Uniswap V3 deployments use. The Base deployment uses a
non-standard interface.

### Path Encoding

The `path` field is packed bytes (not ABI-encoded), 43 bytes for a single hop:

```
[tokenIn address: 20 bytes][pool fee: 3 bytes big-endian][tokenOut address: 20 bytes]
```

Example for USDC → WETH at 0.3% fee:
```
833589fcd6edb6e08f4c7c32d4f71b54bda02913  000bb8  4200000000000000000000000000000000000006
|____________ USDC _______________________| |_3000_| |____________ WETH ________________|
```

For multi-hop (e.g. USDC → WETH → TOKEN), chain more fee+token pairs
(total = 20 + (23 × n_hops) bytes).

## Price Math (sqrtPriceX96)

Uniswap V3 stores the current price in `slot0.sqrtPriceX96` — a fixed-point number
encoding `sqrt(token1/token0) * 2^96`. This representation allows efficient price
updates using only integer multiplication (no division or square roots during swaps).

### Decoding the price

```
sqrtPriceX96 = sqrt(token1_raw / token0_raw) * 2^96
price_ratio  = (sqrtPriceX96 / 2^96)² = token1_raw / token0_raw
```

The ratio is already in smallest-unit terms, so no decimal adjustment is needed.

### Computing output from input

For a swap from `token_in` to `token_out`:

- If `token_in == token0` (lower address): `output_raw = input_raw * price_ratio`
- If `token_in == token1` (higher address): `output_raw = input_raw / price_ratio`

Example (WETH/USDC pool, price = $2000/ETH):
- WETH is token0, USDC is token1
- price_ratio = 2e-9 (USDC_raw per WETH_raw)
- Selling 0.01 ETH (10^16 raw): `10^16 * 2e-9 = 2e7 raw USDC = 20 USDC` ✓
- Buying 0.01 ETH with USDC: `20 * 10^6 / 2e-9 = 10^16 raw WETH = 0.01 ETH` ✓

### Tick

The `slot0.tick` is `log_1.0001(price)` — an integer that linearly represents
price on a logarithmic scale. Each tick represents a ~0.01% price change.
Liquidity is organized in discrete tick ranges; swaps walk through ticks
until the input amount is exhausted.

## Slippage Protection

Before submitting a swap, the bot calculates the expected output via the
Quoter contract (or pool price as fallback), then reduces it by `SLIPPAGE_BPS`
(default 50 = 0.5%):

```
amountOutMinimum = expected_output * (1 - 50/10000) = expected_output * 0.995
```

This value is passed to the router. If the pool can't deliver at least
`amountOutMinimum` at execution time (because another trade moved the price,
or an MEV bot sandwiched us), the transaction reverts instead of executing
at a bad price.

**Tradeoffs:**
- Too tight (e.g. 10 bps): reverts on normal price movement, especially in volatile markets
- Too loose (e.g. 500 bps): vulnerable to sandwich attacks, MEV extraction

## The Swap Flow in Detail

### 1. Human → Raw Amounts (`place_market_order`)

```
"Buy $10 of ETH" → amount_in = 10 * 10^6 = 10,000,000 (USDC has 6 decimals)
```

### 2. ERC-20 Approval (`_approve_token`)

The router needs permission to pull tokens from the bot's wallet. This is done
via `USDC.approve(router, 2^256-1)` — a one-time max approval per token.
The approval is cached in memory and checked before each swap to avoid
redundant RPC calls.

### 3. Quote / Fallback (`_get_amount_out_minimum`)

Primary: call `QuoterV2.quoteExactInputSingle(tokenIn, tokenOut, fee, amountIn, 0)`.
This simulates the swap off-chain and returns the exact output the pool would give.

Fallback: if the Quoter fails (RPC error, rate limit), read `pool.slot0()` and
compute the output from the spot price. The formula handles the token0/token1
direction (multiply vs divide) as described above.

### 4. Build & Sign Transaction

web3.py's ABI encoder converts the `ExactInputParams` struct into calldata:
```
0xb858183f                          ← function selector
000000000000...0020                 ← offset to path (dynamic bytes)
000000000000...recipient             ← recipient address
000000000000...000186a0              ← amountIn (1000000)
000000000000...amountOutMinimum      ← slippage-protected minimum
000000000000...002b                  ← path length (43 bytes)
833589fc...000bb8...42000006          ← packed path data
```

The transaction is signed locally with the bot's private key, then broadcast
via `eth_sendRawTransaction`.

### 5. On-Chain Execution

1. Router receives the call, decodes the struct
2. Router calls `Pool.swap()` with the specified parameters
3. Pool calls back to the router via `uniswapV3SwapCallback(amount0, amount1, data)`
4. Router calls `token.transferFrom(wallet, pool, input_amount)` to pull tokens
5. Pool sends output tokens to the recipient
6. Router returns `amountOut` to the caller

## Debugging Lessons

### The Original Bug

Swaps were failing with the original `exactInputSingle` call (selector `0x414bf389`).
Investigation revealed:

1. The Base SwapRouter02 at `0x2626...e481` does **not** have the standard
   `exactInputSingle` function. Calling it produces `0x` (no function found).

2. The contract has `exactInputSingle` at a **different** selector (`0x04e45aaf`)
   with a slightly different struct. Calling this got further (104k gas) but
   reverted with "STF" (Safe Transfer Failed) — a bug in the contract's callback.

3. The working function is `exactInput` (selector `0xb858183f`) with a 4-field
   struct (no `deadline` field). This is what successful swaps on Base use.

### The Price Math Bug

The fallback price calculation had `10^(decimals_in - decimals_out)` where it
should have had either no adjustment (the raw price_ratio already accounts for
decimals) or `price_ratio / ...` for token1→token0 swaps. The bug manifested
only for pairs where the output token had more decimals than the input (e.g.
USDC→WBTC), producing ~5000x the correct output and making swaps always revert
on the slippage check.

## Key Links

- [Uniswap V3 Docs](https://docs.uniswap.org/contracts/v3/overview)
- [Uniswap V3 Whitepaper](https://uniswap.org/whitepaper-v3.pdf) — concentrated liquidity math
- [SwapRouter02 Source (GitHub)](https://github.com/Uniswap/v3-periphery/blob/main/contracts/SwapRouter.sol)
- [V3 Pool Source](https://github.com/Uniswap/v3-core/blob/main/contracts/UniswapV3Pool.sol) — `swap()`, `slot0`, tick math
- [BaseScan — SwapRouter02](https://basescan.org/address/0x2626664c2603336E57B271c5C0b26F421741e481)
- [BaseScan — WETH/USDC Pool](https://basescan.org/address/0x6c561B446416E1A00E8E93E221854d6eA4171372)
- [web3.py Contract Docs](https://web3py.readthedocs.io/en/stable/contracts.html) — ABI encoding, `build_transaction`, `sign_transaction`
- [Uniswap V3 Fee Tiers](https://docs.uniswap.org/contracts/v3/concepts/fees) — explains 500/3000/10000 tiers
