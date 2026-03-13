#!/usr/bin/env python3
import sys

with open('ethereum_executor.py', 'r') as f:
    lines = f.readlines()

# 1. Update TOKENS BTC address
for i, line in enumerate(lines):
    if line.strip().startswith('"BTC":'):
        lines[i] = '    "BTC": "0x0555E30da8f98308EdB960aa94C0Db47230d2B9c",    # WBTC\n'
        print(f"Updated TOKENS BTC line {i+1}")
        break

# 2. Update POOLS BTC-USDC address
for i, line in enumerate(lines):
    if line.strip().startswith('"BTC-USDC":'):
        lines[i] = '    "BTC-USDC": "0x49e30c322E2474B3767de9FC4448C1e9ceD6552f", # WBTC/USDC 0.3%\n'
        print(f"Updated POOLS BTC-USDC line {i+1}")
        break

# 3. Update POOL_FEES BTC-USDC fee
for i, line in enumerate(lines):
    if line.strip().startswith('"BTC-USDC":'):
        # Might be two occurrences (POOLS and POOL_FEES). We'll update the one after POOL_FEES
        # Let's search for POOL_FEES block
        pass

# Simpler: replace the POOL_FEES block entirely
in_pool_fees = False
for i, line in enumerate(lines):
    if line.strip() == 'POOL_FEES = {':
        in_pool_fees = True
        continue
    if in_pool_fees and line.strip().startswith('"BTC-USDC":'):
        lines[i] = '    "BTC-USDC": 3000,\n'
        print(f"Updated POOL_FEES BTC-USDC line {i+1}")
        break
    if in_pool_fees and line.strip() == '}':
        break

with open('ethereum_executor.py', 'w') as f:
    f.writelines(lines)

print("Done.")