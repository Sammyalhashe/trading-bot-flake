#!/usr/bin/env python3
import os
from web3 import Web3

rpc_url = os.environ.get('BASE_RPC_URL', 'https://mainnet.base.org')
w3 = Web3(Web3.HTTPProvider(rpc_url))
print(f'Connected: {w3.is_connected()}')

# Check placeholder address
placeholder = '0x12745348866297371569477B73738018e6e8772a'
print(f"\nChecking placeholder address: {placeholder}")
code = w3.eth.get_code(Web3.to_checksum_address(placeholder))
print(f"Contract size: {len(code)}")

if len(code) > 0:
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
         "outputs": [{"name": "", "type": "uint128"}], "type": "function"},
        {"constant": True, "inputs": [], "name": "token0",
         "outputs": [{"name": "", "type": "address"}], "type": "function"},
        {"constant": True, "inputs": [], "name": "token1",
         "outputs": [{"name": "", "type": "address"}], "type": "function"},
        {"constant": True, "inputs": [], "name": "fee",
         "outputs": [{"name": "", "type": "uint24"}], "type": "function"}
    ]
    pool_contract = w3.eth.contract(address=Web3.to_checksum_address(placeholder), abi=pool_abi)
    try:
        slot0 = pool_contract.functions.slot0().call()
        liquidity = pool_contract.functions.liquidity().call()
        token0 = pool_contract.functions.token0().call()
        token1 = pool_contract.functions.token1().call()
        fee = pool_contract.functions.fee().call()
        print(f"Pool is valid!")
        print(f"  token0: {token0}")
        print(f"  token1: {token1}")
        print(f"  fee: {fee}")
        print(f"  liquidity: {liquidity}")
        print(f"  sqrtPriceX96: {slot0[0]}")
    except Exception as e:
        print(f"Error reading pool: {e}")
else:
    print("Not a contract")

# Try reversed order USDC, BTC
print("\nTrying reversed order USDC, BTC...")
USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
BTC = '0xcbB7C919d9600a40748358403e5Ff15d0d670081'
FACTORY = '0x33128a8fC17869897dcE68Ed026d694621f6FDfD'
factory_abi = [{"inputs": [{"internalType": "address", "name": "tokenA", "type": "address"}, {"internalType": "address", "name": "tokenB", "type": "address"}, {"internalType": "uint24", "name": "fee", "type": "uint24"}], "name": "getPool", "outputs": [{"internalType": "address", "name": "pool", "type": "address"}], "stateMutability": "view", "type": "function"}]
factory = w3.eth.contract(address=Web3.to_checksum_address(FACTORY), abi=factory_abi)

for fee in [100, 500, 3000, 10000]:
    pool = factory.functions.getPool(Web3.to_checksum_address(USDC), Web3.to_checksum_address(BTC), fee).call()
    if pool != '0x0000000000000000000000000000000000000000':
        print(f"Found pool for fee {fee}: {pool}")
        # check liquidity
        pool_contract = w3.eth.contract(address=pool, abi=pool_abi)
        try:
            liquidity = pool_contract.functions.liquidity().call()
            print(f"  liquidity: {liquidity}")
        except:
            pass
    else:
        print(f"No pool for fee {fee}")

print("\nDone.")