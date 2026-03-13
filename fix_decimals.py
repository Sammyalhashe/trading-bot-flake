#!/usr/bin/env python3
import sys

with open('ethereum_executor.py', 'r') as f:
    lines = f.readlines()

# Find the line numbers for the four lines we want to replace
# Look for "# Get decimals" comment
for i, line in enumerate(lines):
    if line.strip() == '# Get decimals':
        start = i + 1  # next line
        # Replace lines[start:start+4] with new lines
        lines[start] = '            usdc_decimals = self._get_decimals(usdc_addr)\n'
        lines[start+1] = '            token_decimals = self._get_decimals(token_addr)\n'
        lines[start+2] = ''  # remove usdc_contract line
        lines[start+3] = ''  # remove token_contract line
        # Also need to remove the empty lines later; we'll just leave them as empty strings.
        # However, we should also remove the two lines that call .call() (they are the same lines we already replaced?)
        # Wait: we already replaced lines[start] and lines[start+1] with new lines, but lines[start+2] and lines[start+3] are the .call() lines.
        # Actually the original four lines are:
        # 1: usdc_contract = ...
        # 2: token_contract = ...
        # 3: usdc_decimals = usdc_contract...
        # 4: token_decimals = token_contract...
        # We want to replace all four with two lines.
        # So we need to delete two lines. Let's set lines[start+2] and lines[start+3] to empty, and also shift later lines?
        # Better to delete them by removing from list. Let's do that.
        del lines[start+2]  # removes usdc_decimals = ...
        del lines[start+2]  # now token_decimals = ... is at start+2 (since we removed one)
        # Now we have two extra empty lines at start and start+1 (the ones we set to empty). Let's remove them.
        # Actually we set lines[start] and lines[start+1] to new lines, and lines[start+2] and lines[start+3] are now removed.
        # The empty lines we set earlier are not needed; we can just keep the new lines.
        # Let's ensure there are no extra blank lines.
        print(f"Replaced decimals fetching at line {i+1}")
        break

with open('ethereum_executor.py', 'w') as f:
    f.writelines(lines)

print("Done.")