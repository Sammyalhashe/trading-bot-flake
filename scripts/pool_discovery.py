#!/usr/bin/env python3
"""
Unified pool discovery tool for Uniswap V3 on Base

Consolidates functionality from:
- find_btc_pool.py
- find_btc_pools.py
- find_link_pool.py
- find_link_pool_final.py
- find_pools.py
"""

import os
import sys
import time
from web3 import Web3
from decimal import Decimal

# Uniswap V3 Factory on Base
FACTORY_ADDRESS = '0x33128a8fC17869897dcE68Ed026d694621f6FDfD'
FACTORY_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "tokenA", "type": "address"},
            {"internalType": "address", "name": "tokenB", "type": "address"},
            {"internalType": "uint24", "name": "fee", "type": "uint24"}
        ],
        "name": "getPool",
        "outputs": [{"internalType": "address", "name": "pool", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }
]

POOL_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "observationIndex", "type": "uint16"},
            {"name": "observationCardinality", "type": "uint16"},
            {"name": "observationCardinalityNext", "type": "uint16"},
            {"name": "feeProtocol", "type": "uint8"},
            {"name": "unlocked", "type": "bool"}
        ],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "liquidity",
        "outputs": [{"name": "", "type": "uint128"}],
        "type": "function"
    }
]

# Token addresses on Base (from ethereum_executor.py)
TOKENS = {
    "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "WETH": "0x4200000000000000000000000000000000000006",
    "BTC": "0x0555E30da8f98308EdB960aa94C0Db47230d2B9c",  # Updated address
    "LINK": "0x88fb150bdc53a65fe94dea0c9ba0a6daf8c6e196",
    "BRETT": "0x532f27101965dd16442e59d40670faf5ebb142e4",
    "TOSHI": "0xAC1Bd2486aAf3B5C0fc3Fd868558b082a531B2B4",
    "MORPHO": "0xbaa5cc21fd487b8fcc2f632f3f4e8d37262a0842",
    "ZRO": "0x6985884c4392d348587b19cb9eaaf157f13271cd",
}

# Fee tiers to try (0.01%, 0.05%, 0.3%, 1%)
FEE_TIERS = [100, 500, 3000, 10000]


def connect_to_base() -> Web3:
    """Connect to Base network via RPC"""
    rpc_url = os.environ.get('BASE_RPC_URL', 'https://mainnet.base.org')
    w3 = Web3(Web3.HTTPProvider(rpc_url))

    if not w3.is_connected():
        raise ConnectionError(f"Failed to connect to Base RPC: {rpc_url}")

    print(f"✓ Connected to Base ({rpc_url})")
    return w3


def find_pool(
    w3: Web3,
    asset_symbol: str,
    base_symbol: str = "USDC",
    verbose: bool = True
) -> dict | None:
    """
    Find best Uniswap V3 pool for given asset pair.

    Args:
        w3: Web3 instance connected to Base
        asset_symbol: Asset to find pool for (e.g., "BTC", "LINK")
        base_symbol: Base token (default: "USDC")
        verbose: Print search progress

    Returns:
        dict with pool address, liquidity, fee tier, or None if not found
    """
    token_a = TOKENS.get(asset_symbol.upper())
    token_b = TOKENS.get(base_symbol.upper())

    if not token_a:
        print(f"✗ Unknown asset: {asset_symbol}")
        print(f"  Available: {', '.join(TOKENS.keys())}")
        return None

    if not token_b:
        print(f"✗ Unknown base token: {base_symbol}")
        return None

    token_a = Web3.to_checksum_address(token_a)
    token_b = Web3.to_checksum_address(token_b)

    factory = w3.eth.contract(
        address=Web3.to_checksum_address(FACTORY_ADDRESS),
        abi=FACTORY_ABI
    )

    best_pool = None
    max_liquidity = 0

    if verbose:
        print(f"\nSearching for {asset_symbol}-{base_symbol} pools...")

    for fee in FEE_TIERS:
        try:
            pool_address = factory.functions.getPool(token_a, token_b, fee).call()

            if pool_address == '0x0000000000000000000000000000000000000000':
                if verbose:
                    print(f"  {fee:5d} bp: No pool exists")
                continue

            # Check liquidity
            pool_contract = w3.eth.contract(address=pool_address, abi=POOL_ABI)
            try:
                slot0 = pool_contract.functions.slot0().call()
                liquidity = pool_contract.functions.liquidity().call()

                if liquidity > max_liquidity:
                    max_liquidity = liquidity
                    best_pool = {
                        "address": pool_address,
                        "fee": fee,
                        "liquidity": liquidity,
                        "sqrtPriceX96": slot0[0],
                        "tick": slot0[1],
                    }

                status = "✓" if liquidity > 0 else "✗"
                if verbose:
                    print(f"  {fee:5d} bp: {status} {pool_address[:10]}... (liquidity: {liquidity:,})")

            except Exception as e:
                if verbose:
                    print(f"  {fee:5d} bp: ✗ Pool exists but error: {e}")

            time.sleep(0.1)  # Rate limiting

        except Exception as e:
            if verbose:
                print(f"  {fee:5d} bp: Error querying: {e}")

    return best_pool


def main():
    """CLI entry point"""
    if len(sys.argv) < 2:
        print("Usage: python pool_discovery.py <ASSET> [BASE_TOKEN]")
        print("\nExamples:")
        print("  python pool_discovery.py BTC")
        print("  python pool_discovery.py LINK USDC")
        print("  python pool_discovery.py ETH USDC")
        print(f"\nAvailable tokens: {', '.join(TOKENS.keys())}")
        sys.exit(1)

    asset = sys.argv[1]
    base = sys.argv[2] if len(sys.argv) > 2 else "USDC"

    try:
        w3 = connect_to_base()
        pool = find_pool(w3, asset, base, verbose=True)

        if pool:
            print("\n" + "=" * 60)
            print("BEST POOL FOUND")
            print("=" * 60)
            print(f"Address:   {pool['address']}")
            print(f"Fee:       {pool['fee']} bp ({pool['fee'] / 10000}%)")
            print(f"Liquidity: {pool['liquidity']:,}")
            print(f"Tick:      {pool['tick']}")
            print("=" * 60)
        else:
            print(f"\n✗ No pools found for {asset}-{base}")
            sys.exit(1)

    except Exception as e:
        print(f"\n✗ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
