#!/usr/bin/env python3
import sys

with open('ethereum_executor.py', 'r') as f:
    lines = f.readlines()

# Find the method start and end
start = None
for i, line in enumerate(lines):
    if line.strip().startswith('def _get_amount_out_minimum'):
        start = i
        break

if start is None:
    print("Method not found")
    sys.exit(1)

# Find end of method (next method definition or end of class)
end = None
for i in range(start + 1, len(lines)):
    if lines[i].strip().startswith('def ') and not lines[i].strip().startswith('def _'):
        end = i
        break
if end is None:
    end = len(lines)

# New method content
new_method = '''    def _get_amount_out_minimum(self, token_in, token_out, amount_in, fee, slippage_bps=SLIPPAGE_BPS):
        """Calculate amountOutMinimum with slippage using Quoter."""
        try:
            amount_out = self.get_quote(token_in, token_out, amount_in, fee)
            if amount_out is None:
                # Fallback to pool price calculation
                logging.warning("Quote failed, falling back to pool price calculation")
                for pool_id, pool_addr in POOLS.items():
                    if token_in.upper() in pool_id and token_out.upper() in pool_id:
                        pool_address = pool_addr
                        pool_contract = self.w3.eth.contract(address=pool_address, abi=UNISWAP_V3_POOL_ABI)
                        slot0 = pool_contract.functions.slot0().call()
                        sqrtPriceX96 = slot0[0]
                        price_ratio = (Decimal(sqrtPriceX96) / Decimal(2**96)) ** 2
                        # Determine token0/token1 ordering
                        zero_for_one = pool_id.startswith(token_in.split("-")[0])
                        token_in_contract = self.w3.eth.contract(address=Web3.to_checksum_address(token_in), abi=ERC20_ABI)
                        token_out_contract = self.w3.eth.contract(address=Web3.to_checksum_address(token_out), abi=ERC20_ABI)
                        decimals_in = token_in_contract.functions.decimals().call()
                        decimals_out = token_out_contract.functions.decimals().call()
                        if zero_for_one:
                            factor = price_ratio * Decimal(10 ** (decimals_in - decimals_out))
                        else:
                            factor = (Decimal(1) / price_ratio) * Decimal(10 ** (decimals_in - decimals_out))
                        amount_out = int(Decimal(amount_in) * factor)
                        break
                else:
                    # No pool found, assume 1% slippage
                    amount_out = int(amount_in * 0.99)
            
            # Apply slippage
            min_out = int(Decimal(amount_out) * (Decimal(1) - Decimal(slippage_bps) / Decimal(10000)))
            return min_out
        except Exception as e:
            logging.warning(f"Could not calculate amountOutMinimum: {e}")
            return int(amount_in * 0.99)
'''

# Replace lines[start:end] with new_method lines
new_lines = lines[:start] + [new_method + '\n'] + lines[end:]

with open('ethereum_executor.py', 'w') as f:
    f.writelines(new_lines)

print("Replaced _get_amount_out_minimum method.")