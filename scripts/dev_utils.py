#!/usr/bin/env python3
"""
Development utilities for testing trading bot components

Consolidates functionality from:
- test_imports.py
- test_btc_price.py
- test_executor.py
"""

import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_imports():
    """Test that all required dependencies are installed"""
    print("Testing imports...")
    try:
        import requests
        print("  ✓ requests")
        import pandas
        print("  ✓ pandas")
        import jwt
        print("  ✓ jwt")
        import cryptography
        print("  ✓ cryptography")
        import web3
        print("  ✓ web3")
        import trading_bot
        print("  ✓ trading_bot")
        print("\n✓ All modules imported successfully!")
        return True
    except ImportError as e:
        print(f"\n✗ Import failed: {e}")
        return False


def test_executor_prices(product_ids=None):
    """Test executor price fetching"""
    if product_ids is None:
        product_ids = ["BTC-USDC", "ETH-USDC", "LINK-USDC"]

    from executors.ethereum_executor import EthereumExecutor

    # Paper mode with dummy key
    rpc_url = os.environ.get('BASE_RPC_URL', 'https://mainnet.base.org')
    executor = EthereumExecutor(
        rpc_url,
        '0x0000000000000000000000000000000000000000000000000000000000000001',
        'paper'
    )

    print(f"Testing executor with RPC: {rpc_url}\n")

    for product_id in product_ids:
        print(f"Testing {product_id}...")
        try:
            details = executor.get_product_details(product_id)
            if details:
                print(f"  Price: ${details['price']}")
                print(f"  Quote increment: {details.get('quote_increment', 'N/A')}")

                # Test token address lookup
                addr = executor.get_token_address(product_id)
                print(f"  Token address: {addr}")
            else:
                print(f"  ✗ Failed to get details")
        except Exception as e:
            print(f"  ✗ Error: {e}")
        print()

    print("✓ Executor test complete")


def test_pool_addresses():
    """Test pool address lookup"""
    from executors.ethereum_executor import POOLS

    print("Configured pool addresses:")
    for product_id, pool_info in POOLS.items():
        print(f"  {product_id}: {pool_info['address']}")
    print()


def main():
    """CLI entry point"""
    if len(sys.argv) < 2:
        print("Usage: python dev_utils.py <test_name>")
        print("\nAvailable tests:")
        print("  imports         - Test all module imports")
        print("  executor        - Test executor price fetching")
        print("  pools           - Show configured pool addresses")
        print("  all             - Run all tests")
        print("\nExamples:")
        print("  python dev_utils.py imports")
        print("  python dev_utils.py executor")
        print("  python dev_utils.py all")
        sys.exit(1)

    test_name = sys.argv[1].lower()

    if test_name == "imports":
        test_imports()
    elif test_name == "executor":
        test_executor_prices()
    elif test_name == "pools":
        test_pool_addresses()
    elif test_name == "all":
        print("=" * 60)
        print("RUNNING ALL TESTS")
        print("=" * 60)
        print()

        test_imports()
        print("\n" + "=" * 60 + "\n")

        test_executor_prices()
        print("\n" + "=" * 60 + "\n")

        test_pool_addresses()
        print("\n" + "=" * 60)
        print("ALL TESTS COMPLETE")
        print("=" * 60)
    else:
        print(f"Unknown test: {test_name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
