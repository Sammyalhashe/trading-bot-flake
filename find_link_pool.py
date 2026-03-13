#!/usr/bin/env python3
import os
from web3 import Web3
import time

rpc_url = os.environ.get('BASE_RPC_URL', 'https://mainnet.base.org')
w3 = Web3(Web3.HTTPProvider(rpc_url))
print(f'Connected: {w3.is_connected()}')

# Candidate LINK addresses on Base (official Chainlink token)
LINK_CANDIDATES = [
    "0x8D21D63f749b514b1c9B8998d75956Dd59b60d60",  # likely official
    "0x514910771AF9Ca656af840dff83E8264EcF986CA",  # Ethereum LINK (maybe bridged)
]

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "name", "outputs": [{"name": "", "type": "string"}], "type": "function"}
]

def get_token_info(address):
    try:
        contract = w3.eth.contract(address=Web3.to_checksum_address(address), abi=ERC20_ABI)
        symbol = contract.functions.symbol().call()
        decimals = contract.functions.decimals().call()
        name = contract.functions.name().call()
        return symbol, decimals, name
    except Exception as e:
        return None, None, None

print("\nChecking LINK token candidates...")
for addr in LINK_CANDIDATES:
    sym, dec, name = get_token_info(addr)
    if sym:
        print(f"  {addr}: {sym} ({dec} decimals) - {name}")
    else:
        print(f"  {addr}: Not a valid ERC20")

# Use the first candidate as LINK token
LINK = LINK_CANDIDATES[0]
print(f"\nUsing LINK address: {LINK}")

# Find pool with USDC
FACTORY = '0x33128a8fC17869897dcE68Ed026d694621f6FDfD'
FACTORY_ABI = [{"inputs": [{"internalType": "address", "name": "tokenA", "type": "address"}, {"internalType": "address", "name": "tokenB", "type": "address"}, {"internalType": "uint24", "name": "fee", "type": "uint24"}], "name": "getPool", "outputs": [{"internalType": "address", "name": "pool", "type": "address"}], "stateMutability": "view", "type": "function"}]
factory = w3.eth.contract(address=Web3.to_checksum_address(FACTORY), abi=FACTORY_ABI)

FEE_TIERS = [100, 500, 3000, 10000]
print("\nSearching for LINK-USDC pools...")
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
            except Exception as e:
                print(f"    Could not read liquidity: {e}")
        else:
            print(f"  No pool for fee {fee}")
    except Exception as e:
        print(f"  Error for fee {fee}: {e}")
    time.sleep(0.3)

print("\nDone.")