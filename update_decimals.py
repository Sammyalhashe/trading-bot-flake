#!/usr/bin/env python3
import sys

with open('ethereum_executor.py', 'r') as f:
    lines = f.readlines()

# Find line number of place_market_order definition
pm_start = None
for i, line in enumerate(lines):
    if line.strip().startswith('def place_market_order'):
        pm_start = i
        break

if pm_start is None:
    print("place_market_order not found")
    sys.exit(1)

# Insert helper method before pm_start
helper = '''    def _get_decimals(self, token_address):
        """Get token decimals with caching."""
        addr = token_address.lower()
        if addr in self._decimals_cache:
            return self._decimals_cache[addr]
        # Check known decimals
        if token_address in self._known_decimals:
            dec = self._known_decimals[token_address]
            self._decimals_cache[addr] = dec
            return dec
        # Fallback to RPC call
        try:
            contract = self.w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
            dec = contract.functions.decimals().call()
            self._decimals_cache[addr] = dec
            return dec
        except Exception as e:
            logging.error(f"Failed to get decimals for {token_address}: {e}")
            # Default to 18
            return 18
'''

# Insert helper
lines.insert(pm_start, helper + '\n')

# Now need to adjust line numbers after insertion
# Find the new pm_start (should be pm_start + number of lines in helper)
new_pm_start = pm_start + helper.count('\n') + 1

# Find the lines for decimals fetching within place_market_order
# We'll replace from line containing "# Get decimals" to the two lines after
# Let's locate within the method (search from new_pm_start)
for i in range(new_pm_start, len(lines)):
    if lines[i].strip() == '# Get decimals':
        # Replace the next two lines (usdc_contract = ... and token_contract = ...) and the two decimals calls
        # Actually there are 4 lines: usdc_contract..., token_contract..., usdc_decimals..., token_decimals...
        # Let's replace lines[i+1] through lines[i+4]
        lines[i+1] = '            usdc_decimals = self._get_decimals(usdc_addr)\n'
        lines[i+2] = '            token_decimals = self._get_decimals(token_addr)\n'
        # Remove the two lines after that (the .call() lines) by setting them to empty
        lines[i+3] = ''  # usdc_decimals = usdc_contract...
        lines[i+4] = ''  # token_decimals = token_contract...
        # Need to also remove the empty lines later; we'll just leave them as empty strings, they'll be ignored.
        print(f"Replaced decimals fetching at lines {i+1} to {i+4}")
        break

with open('ethereum_executor.py', 'w') as f:
    f.writelines(lines)

print("Done.")