#!/usr/bin/env python3
import os
import time
from web3 import Web3

rpc_url = os.environ.get('BASE_RPC_URL', 'https://mainnet.base.org')
w3 = Web3(Web3.HTTPProvider(rpc_url))
print(f'Connected: {w3.is_connected()}')

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

TOKENS = {
    "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "cbBTC": "0xcbB7C919d9600a40748358403e5Ff15d0d670081",
    "WBTC": "0x0555E30da8f98308EdB960aa94C0Db47230d2B9c",  # placeholder, verify
}

PAIRS = [
    ("cbBTC", "USDC"),
    ("WBTC", "USDC"),
]

FEE_TIERS = [100, 500, 3000, 10000]

factory = w3.eth.contract(address=Web3.to_checksum_address(FACTORY_ADDRESS), abi=FACTORY_ABI)

for token_a_sym, token_b_sym in PAIRS:
    token_a = TOKENS.get(token_a_sym)
    token_b = TOKENS.get(token_b_sym)
    if not token_a or not token_b:
        print(f"Missing token address for {token_a_sym} or {token_b_sym}")
        continue
    
    token_a = Web3.to_checksum_address(token_a)
    token_b = Web3.to_checksum_address(token_b)
    
    print(f"\nLooking for {token_a_sym}-{token_b_sym} pools...")
    for fee in FEE_TIERS:
        try:
            pool = factory.functions.getPool(token_a, token_b, fee).call()
            if pool != '0x0000000000000000000000000000000000000000':
                print(f"  Found pool: {pool} (fee {fee})")
                # Quick liquidity check
                pool_abi = [
                    {"constant": True, "inputs": [], "name": "liquidity",
                     "outputs": [{"name": "", "type": "uint128"}], "type": "function"}
                ]
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
        time.sleep(0.3)  # avoid rate limit

print("\nDone.")