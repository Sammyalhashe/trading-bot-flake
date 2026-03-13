#!/usr/bin/env python3
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ethereum_executor import EthereumExecutor
from web3 import Web3

# Paper mode, dummy key
rpc_url = 'https://mainnet.base.org'
executor = EthereumExecutor(rpc_url, '0x0000000000000000000000000000000000000000000000000000000000000001', 'paper')

print("Testing product details for BTC-USDC...")
details = executor.get_product_details('BTC-USDC')
if details:
    print(f"Price: ${details['price']}")
    print(f"Quote increment: {details['quote_increment']}")
else:
    print("Failed to get details")

print("\nTesting product details for ETH-USDC...")
details = executor.get_product_details('ETH-USDC')
if details:
    print(f"Price: ${details['price']}")
else:
    print("Failed")

print("\nTesting get_token_address for BTC-USDC:")
addr = executor.get_token_address('BTC-USDC')
print(f"Token address: {addr}")

print("\nChecking pool address in POOLS:")
from ethereum_executor import POOLS
print(f"BTC-USDC pool: {POOLS.get('BTC-USDC')}")