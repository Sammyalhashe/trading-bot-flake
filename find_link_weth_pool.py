#!/usr/bin/env python3
import os
from web3 import Web3
import time

rpc_url = os.environ.get('BASE_RPC_URL', 'https://mainnet.base.org')
w3 = Web3(Web3.HTTPProvider(rpc_url))

LINK = '0x88fb150bdc53a65fe94dea0c9ba0a6daf8c6e196'
WETH = '0x4200000000000000000000000000000000000006'

FACTORY = '0x33128a8fC17869897dcE68Ed026d694621f6FDfD'
FACTORY_ABI = [{"inputs": [{"internalType": "address", "name": "tokenA", "type": "address"}, {"internalType": "address", "name": "tokenB", "type": "address"}, {"internalType": "uint24", "name": "fee", "type": "uint24"}], "name": "getPool", "outputs": [{"internalType": "address", "name": "pool", "type": "address"}], "stateMutability": "view", "type": "function"}]
factory = w3.eth.contract(address=Web3.to_checksum_address(FACTORY), abi=FACTORY_ABI)

FEE_TIERS = [100, 500, 3000, 10000]
print("Searching for LINK-WETH pools...")
for fee in FEE_TIERS:
    try:
        pool = factory.functions.getPool(Web3.to_checksum_address(LINK), Web3.to_checksum_address(WETH), fee).call()
        if pool != '0x0000000000000000000000000000000000000000':
            print(f"  Found pool: {pool} (fee {fee})")
            # Quick liquidity check
            pool_abi = [{"constant": True, "inputs": [], "name": "liquidity", "outputs": [{"name": "", "type": "uint128"}], "type": "function"}]
            pool_contract = w3.eth.contract(address=pool, abi=pool_abi)
            try:
                liquidity = pool_contract.functions.liquidity().call()
                print(f"    Liquidity: {liquidity}")
                if liquidity > 0:
                    print(f"    *** This pool has liquidity!")
            except Exception as e:
                print(f"    Could not read liquidity: {e}")
        else:
            print(f"  No pool for fee {fee}")
    except Exception as e:
        print(f"  Error for fee {fee}: {e}")
    time.sleep(0.5)