#!/usr/bin/env python3
import os
from web3 import Web3

rpc_url = os.environ.get('BASE_RPC_URL', 'https://mainnet.base.org')
w3 = Web3(Web3.HTTPProvider(rpc_url))

POOL_ADDRESS = '0x49e30c322E2474B3767de9FC4448C1e9ceD6552f'
POOL_ABI = [
    {"constant": True, "inputs": [], "name": "token0", "outputs": [{"name": "", "type": "address"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "token1", "outputs": [{"name": "", "type": "address"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "fee", "outputs": [{"name": "", "type": "uint24"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "liquidity", "outputs": [{"name": "", "type": "uint128"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "slot0", "outputs": [
        {"name": "sqrtPriceX96", "type": "uint160"},
        {"name": "tick", "type": "int24"},
        {"name": "observationIndex", "type": "uint16"},
        {"name": "observationCardinality", "type": "uint16"},
        {"name": "observationCardinalityNext", "type": "uint16"},
        {"name": "feeProtocol", "type": "uint8"},
        {"name": "unlocked", "type": "bool"}
    ], "type": "function"}
]

pool = w3.eth.contract(address=Web3.to_checksum_address(POOL_ADDRESS), abi=POOL_ABI)
token0 = pool.functions.token0().call()
token1 = pool.functions.token1().call()
fee = pool.functions.fee().call()
liquidity = pool.functions.liquidity().call()
slot0 = pool.functions.slot0().call()

print(f"Pool: {POOL_ADDRESS}")
print(f"Token0: {token0}")
print(f"Token1: {token1}")
print(f"Fee: {fee}")
print(f"Liquidity: {liquidity}")
print(f"sqrtPriceX96: {slot0[0]}")

# Determine which token is WBTC and which is USDC
USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
if token0.lower() == USDC.lower():
    print("Token0 is USDC, Token1 is WBTC")
else:
    print("Token0 is WBTC, Token1 is USDC")

# Get token details
ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"}
]
token0_contract = w3.eth.contract(address=token0, abi=ERC20_ABI)
token1_contract = w3.eth.contract(address=token1, abi=ERC20_ABI)
dec0 = token0_contract.functions.decimals().call()
dec1 = token1_contract.functions.decimals().call()
sym0 = token0_contract.functions.symbol().call()
sym1 = token1_contract.functions.symbol().call()
print(f"Token0: {sym0} ({dec0} decimals)")
print(f"Token1: {sym1} ({dec1} decimals)")