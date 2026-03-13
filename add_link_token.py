#!/usr/bin/env python3
import sys

with open('ethereum_executor.py', 'r') as f:
    lines = f.readlines()

# Find TOKENS dict and add LINK before the closing brace
in_tokens = False
for i, line in enumerate(lines):
    if line.strip() == 'TOKENS = {':
        in_tokens = True
        continue
    if in_tokens and line.strip() == '}':
        # Insert before this line
        lines.insert(i, '    "LINK": "0x88fb150bdc53a65fe94dea0c9ba0a6daf8c6e196",    # Chainlink\n')
        print(f"Inserted LINK at line {i+1}")
        break

with open('ethereum_executor.py', 'w') as f:
    f.writelines(lines)

print("Done.")