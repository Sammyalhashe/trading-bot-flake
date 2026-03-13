#!/usr/bin/env python3
import os
import sys
from web3 import Web3
from decimal import Decimal

# Base RPC
rpc_url = os.environ.get('BASE_RPC_URL', 'https://mainnet.base.org')
w3 = Web3(Web3.HTTPProvider(rpc_url))
print(f'Connected: {w3.is_connected()}')

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

# Token addresses (from ethereum_executor.py)
TOKENS = {
    "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "USDC.e": "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",
    "WETH": "0x4200000000000000000000000000000000000006",
    "BTC": "0xcbB7C919d9600a40748358403e5Ff15d0d670081",
    "DEGEN": "0x4ed4E281562193f5C8c11259D3e21839951e7d23",
    "AERO": "0x9401811A062933285c64D72A25e8e3cf24f3fFBE",
    "POL": "0x4edd6d3c96ba47d1c6f6b31c4d3b8e0b9e0b9e0b",  # placeholder, need correct address
}

# Pairs to look up
PAIRS = [
    ("WETH", "USDC"),
    ("BTC", "USDC"),
    ("POL", "USDC"),
    ("DEGEN", "USDC"),
    ("AERO", "USDC"),
]

# Fee tiers to try (0.01%, 0.05%, 0.3%, 1%)
FEE_TIERS = [100, 500, 3000, 10000]

factory = w3.eth.contract(address=Web3.to_checksum_address(FACTORY_ADDRESS), abi=FACTORY_ABI)

results = {}

for token_a_sym, token_b_sym in PAIRS:
    token_a = TOKENS.get(token_a_sym)
    token_b = TOKENS.get(token_b_sym)
    if not token_a or not token_b:
        print(f"Missing token address for {token_a_sym} or {token_b_sym}")
        continue
    
    token_a = Web3.to_checksum_address(token_a)
    token_b = Web3.to_checksum_address(token_b)
    
    for fee in FEE_TIERS:
        try:
            pool = factory.functions.getPool(token_a, token_b, fee).call()
            if pool != '0x0000000000000000000000000000000000000000':
                # Check if pool has liquidity (get slot0)
                pool_abi = [
                    {"constant": True, "inputs": [], "name": "slot0", 
                     "outputs": [
                         {"name": "sqrtPriceX96", "type": "uint160"},
                         {"name": "tick", "type": "int24"},
                         {"name": "observationIndex", "type": "uint16"},
                         {"name": "observationCardinality", "type": "uint16"},
                         {"name": "observationCardinalityNext", "type": "uint16"},
                         {"name": "feeProtocol", "type": "uint8"},
                         {"name": "unlocked", "type": "bool"}
                     ], "type": "function"},
                    {"constant": True, "inputs": [], "name": "liquidity",
                     "outputs": [{"name": "", "type": "uint128"}], "type": "function"}
                ]
                pool_contract = w3.eth.contract(address=pool, abi=pool_abi)
                try:
                    slot0 = pool_contract.functions.slot0().call()
                    liquidity = pool_contract.functions.liquidity().call()
                    if liquidity > 0:
                        key = f"{token_a_sym}-{token_b_sym}"
                        results[key] = {
                            "address": pool,
                            "fee": fee,
                            "liquidity": liquidity,
                            "sqrtPriceX96": slot0[0]
                        }
                        print(f"Found pool {key}: {pool} (fee {fee}, liquidity {liquidity})")
                except Exception as e:
                    print(f"Pool {pool} exists but no liquidity or error: {e}")
        except Exception as e:
            pass

print("\n=== Results ===")
for pair, info in results.items():
    print(f"{pair}: {info['address']} (fee {info['fee']})")

# Also get Quoter address for reference
print("\nQuoter address: 0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a")
print("SwapRouter address: 0x2626664c2603336E57B271c5C0b26F421741e481")