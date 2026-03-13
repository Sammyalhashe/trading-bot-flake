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
else:
    print("Failed")

print("\nTesting get_token_address for ETH-USDC:")
addr = executor.get_token_address('ETH-USDC')
print(f"Token address: {addr}")

print("\nTesting get_token_address for LINK-USDC:")
addr = executor.get_token_address('LINK-USDC')
print(f"Token address: {addr}")

print("\nAll good.")