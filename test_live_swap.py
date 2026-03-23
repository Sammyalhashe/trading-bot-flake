#!/usr/bin/env python3
import os
import sys
import logging
import time
import argparse
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from executors.ethereum_executor import EthereumExecutor, TOKENS, POOL_FEES
from web3 import Web3

def main():
    parser = argparse.ArgumentParser(description='Live swap test')
    parser.add_argument('--yes', action='store_true', help='Skip confirmation')
    parser.add_argument('--amount', type=float, default=5.0, help='USDC amount (default 5)')
    parser.add_argument('--product', default='ETH-USDC', help='Product ID')
    parser.add_argument('--side', default='BUY', choices=['BUY', 'SELL'])
    args = parser.parse_args()
    
    # Load private key
    private_key = os.environ.get('ETH_PRIVATE_KEY')
    if not private_key:
        # Try secrets file
        secret_path = '/run/secrets/eth_private_key'
        if os.path.exists(secret_path):
            with open(secret_path, 'r') as f:
                private_key = f.read().strip()
        else:
            print("Error: ETH_PRIVATE_KEY not set and secret file not found")
            sys.exit(1)
    
    rpc_url = os.environ.get('BASE_RPC_URL', 'https://mainnet.base.org')
    trading_mode = 'live'  # force live for this test
    
    print("=== Ethereum Executor Live Swap Test ===")
    print(f"RPC: {rpc_url}")
    print(f"Mode: {trading_mode}")
    
    executor = EthereumExecutor(rpc_url, private_key, trading_mode)
    
    print("\n1. Checking balances...")
    balances = executor.get_balances()
    usdc_balance = balances['cash'].get('USDC', 0.0)
    eth_balance = balances['crypto'].get('ETH', 0.0)
    print(f"USDC: ${usdc_balance:.2f}")
    print(f"ETH: {eth_balance:.6f}")
    
    if args.side == 'BUY' and usdc_balance < args.amount:
        print("Error: Insufficient USDC balance")
        sys.exit(1)
    
    print(f"\n2. Preparing to {args.side} {args.product} with ${args.amount}...")
    
    # Get current price
    details = executor.get_product_details(args.product)
    if not details:
        print("Error: Could not get price")
        sys.exit(1)
    price = float(details['price'])
    print(f"Current price: ${price:.2f}")
    
    # Estimate output
    if args.side == 'BUY':
        output_amount = args.amount / price
        print(f"Estimated receive: {output_amount:.6f} {args.product.split('-')[0]}")
    else:
        output_amount = args.amount * price
        print(f"Estimated receive: ${output_amount:.2f} USDC")
    
    if not args.yes:
        confirm = input("\nProceed with swap? (yes/no): ")
        if confirm.lower() != 'yes':
            print("Aborted.")
            sys.exit(0)
    
    print("\n3. Executing swap...")
    if args.side == 'BUY':
        result = executor.place_market_order(args.product, args.side, amount_quote_currency=args.amount)
    else:
        # For SELL, need amount_base_currency; assume we want to sell all ETH? We'll just sell a small amount.
        # Not implemented for this test.
        print("SELL not implemented in this test")
        sys.exit(1)
    
    if result and result.get('success'):
        print(f"Swap successful!")
        if 'filled_base' in result:
            print(f"Filled: {result['filled_base']:.6f}")
        if 'filled_quote' in result:
            print(f"Filled: ${result['filled_quote']:.2f}")
    else:
        print(f"Swap failed: {result}")
        sys.exit(1)
    
    print("\n4. Waiting 10 seconds for blockchain confirmation...")
    time.sleep(10)
    
    print("\n5. Checking balances after swap...")
    balances = executor.get_balances()
    usdc_after = balances['cash'].get('USDC', 0.0)
    eth_after = balances['crypto'].get('ETH', 0.0)
    print(f"USDC after: ${usdc_after:.2f}")
    print(f"ETH after: {eth_after:.6f}")
    
    print("\n=== Test Complete ===")

if __name__ == '__main__':
    main()