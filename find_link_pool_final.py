#!/usr/bin/env python3
import os
from web3 import Web3
import time

rpc_url = os.environ.get('BASE_RPC_URL', 'https://mainnet.base.org')
w3 = Web3(Web3.HTTPProvider(rpc_url))
print(f'Connected: {w3.is_connected()}')

LINK = '0x88fb150bdc53a65fe94dea0c9ba0a6daf8c6e196'
USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'

ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "name", "outputs": [{"name": "", "type": "string"}], "type": "function"}
]

print("\nChecking LINK token...")
contract = w3.eth.contract(address=Web3.to_checksum_address(LINK), abi=ERC20_ABI)
try:
    symbol = contract.functions.symbol().call()
    decimals = contract.functions.decimals().call()
    name = contract.functions.name().call()
    print(f"  Symbol: {symbol}")
    print(f"  Decimals: {decimals}")
    print(f"  Name: {name}")
except Exception as e:
    print(f"  Error: {e}")
    exit(1)

# Find pool with USDC
FACTORY = '0x33128a8fC17869897dcE68Ed026d694621f6FDfD'
FACTORY_ABI = [{"inputs": [{"internalType": "address", "name": "tokenA", "type": "address"}, {"internalType": "address", "name": "tokenB", "type": "address"}, {"internalType": "uint24", "name": "fee", "type": "uint24"}], "name": "getPool", "outputs": [{"internalType": "address", "name": "pool", "type": "address"}], "stateMutability": "view", "type": "function"}]
factory = w3.eth.contract(address=Web3.to_checksum_address(FACTORY), abi=FACTORY_ABI)

FEE_TIERS = [100, 500, 3000, 10000]
print("\nSearching for LINK-USDC pools...")
best_pool = None
best_liquidity = 0
for fee in FEE_TIERS:
    try:
        pool = factory.functions.getPool(Web3.to_checksum_address(LINK), Web3.to_checksum_address(USDC), fee).call()
        if pool != '0x0000000000000000000000000000000000000000':
            print(f"  Found pool: {pool} (fee {fee})")
            # Quick liquidity check
            pool_abi = [{"constant": True, "inputs": [], "name": "liquidity", "outputs": [{"name": "", "type": "uint128"}], "type": "function"}]
            pool_contract = w3.eth.contract(address=pool, abi=pool_abi)
            try:
                liquidity = pool_contract.functions.liquidity().call()
                print(f"    Liquidity: {liquidity}")
                if liquidity > best_liquidity:
                    best_liquidity = liquidity
                    best_pool = pool
            except Exception as e:
                print(f"    Could not read liquidity: {e}")
        else:
            print(f"  No pool for fee {fee}")
    except Exception as e:
        print(f"  Error for fee {fee}: {e}")
    time.sleep(0.3)

if best_pool:
    print(f"\nBest pool: {best_pool} with liquidity {best_liquidity}")
    # Determine token ordering
    pool_abi = [
        {"constant": True, "inputs": [], "name": "token0", "outputs": [{"name": "", "type": "address"}], "type": "function"},
        {"constant": True, "inputs": [], "name": "token1", "outputs": [{"name": "", "type": "address"}], "type": "function"},
        {"constant": True, "inputs": [], "name": "fee", "outputs": [{"name": "", "type": "uint24"}], "type": "function"}
    ]
    pool_contract = w3.eth.contract(address=best_pool, abi=pool_abi)
    token0 = pool_contract.functions.token0().call()
    token1 = pool_contract.functions.token1().call()
    fee = pool_contract.functions.fee().call()
    print(f"  token0: {token0}")
    print(f"  token1: {token1}")
    print(f"  fee: {fee}")
    if token0.lower() == LINK.lower():
        print("  token0 is LINK, token1 is USDC")
    else:
        print("  token0 is USDC, token1 is LINK")
else:
    print("\nNo LINK-USDC pool found")