#!/usr/bin/env python3
import os
import sys
import logging
logging.basicConfig(level=logging.INFO)

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ethereum_executor import EthereumExecutor

# Environment variables
rpc_url = os.environ.get('BASE_RPC_URL', 'https://mainnet.base.org')
private_key = os.environ.get('ETH_PRIVATE_KEY')
if not private_key:
    print("Error: ETH_PRIVATE_KEY not set")
    sys.exit(1)

trading_mode = os.environ.get('TRADING_MODE', 'paper')

print(f"Initializing Ethereum executor (mode={trading_mode})...")
executor = EthereumExecutor(rpc_url, private_key, trading_mode)

print("\n1. Getting balances...")
balances = executor.get_balances()
print(f"Cash: {balances['cash']}")
print(f"Crypto: {balances['crypto']}")

usdc_balance = balances['cash'].get('USDC', 0.0)
print(f"\nUSDC balance: ${usdc_balance:.2f}")

if usdc_balance < 5:
    print("Warning: USDC balance less than $5")

print("\n2. Testing product details for BTC-USDC...")
details = executor.get_product_details('BTC-USDC')
if details:
    print(f"BTC-USDC price: ${details['price']}")
else:
    print("Could not get price for BTC-USDC")

print("\n3. Testing quote for $5 USDC -> BTC...")
if executor.trading_mode == 'live':
    # Use actual USDC amount
    usdc_amount = 5.0
    usdc_addr = executor.TOKENS.get('USDC')
    btc_addr = executor.get_token_address('BTC-USDC')
    if btc_addr:
        usdc_contract = executor.w3.eth.contract(address=Web3.to_checksum_address(usdc_addr), abi=executor.ERC20_ABI)
        usdc_decimals = usdc_contract.functions.decimals().call()
        amount_in = int(usdc_amount * (10 ** usdc_decimals))
        fee = executor.POOL_FEES.get('BTC-USDC', 500)
        quote = executor.get_quote(usdc_addr, btc_addr, amount_in, fee)
        if quote:
            btc_contract = executor.w3.eth.contract(address=Web3.to_checksum_address(btc_addr), abi=executor.ERC20_ABI)
            btc_decimals = btc_contract.functions.decimals().call()
            btc_amount = quote / (10 ** btc_decimals)
            print(f"Expected output: {btc_amount:.8f} BTC")
        else:
            print("Quote failed")
    else:
        print("BTC address not found")
else:
    print("Paper mode, skipping quote")

print("\n4. Testing place_market_order (paper)...")
result = executor.place_market_order('BTC-USDC', 'BUY', amount_quote_currency=5.0)
print(f"Result: {result}")

print("\nDone.")