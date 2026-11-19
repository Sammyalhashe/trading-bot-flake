#!/usr/bin/env python3
import json
import requests

# Base token list from Uniswap or Base official
TOKEN_LIST_URLS = [
    "https://static.optimism.io/optimism.tokenlist.json",  # Optimism token list (Base is OP stack)
    "https://tokens.coingecko.com/base/all.json",
    "https://raw.githubusercontent.com/base-org/token-list/main/src/tokens/base-tokens.json"
]

for url in TOKEN_LIST_URLS:
    print(f"\nTrying {url}...")
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            tokens = data.get('tokens', [])
            for token in tokens:
                if token.get('symbol', '').upper() == 'LINK':
                    print(f"  Found LINK: {token}")
                    print(f"    Address: {token.get('address')}")
                    print(f"    ChainId: {token.get('chainId')}")
                    break
            else:
                print("  LINK not found in this list")
        else:
            print(f"  HTTP {resp.status_code}")
    except Exception as e:
        print(f"  Error: {e}")

# Also try to query Base RPC for token info using known address from other sources
# Let's try a different address: maybe 0x350a791Bfc2C21F9Ed5d10980Dad2e2638ffa7f6 (some LINK on Optimism)
print("\nChecking alternative address...")
from web3 import Web3
rpc = 'https://mainnet.base.org'
w3 = Web3(Web3.HTTPProvider(rpc))
addr = '0x350a791Bfc2C21F9Ed5d10980Dad2e2638ffa7f6'
ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"}
]
try:
    contract = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ERC20_ABI)
    sym = contract.functions.symbol().call()
    dec = contract.functions.decimals().call()
    print(f"  {addr}: {sym} ({dec} decimals)")
except Exception as e:
    print(f"  Not a valid ERC20: {e}")