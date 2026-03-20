#!/usr/bin/env python3
"""Debug a Base (chain 8453) transaction by hash.

Usage:
    debug-tx 0xabc123...

Fetches the transaction and receipt from a Base RPC, decodes Uniswap V3
SwapRouter02 calldata if applicable, and prints a human-readable summary
including the revert reason when available.
"""
import sys
from decimal import Decimal
from web3 import Web3

# ── RPC endpoints (public, no key needed) ──────────────────────────────────
RPCS = [
    "https://mainnet.base.org",
    "https://base.blockpi.network/v1/rpc/public",
    "https://1rpc.io/base",
    "https://base.meowrpc.com",
]

# ── Known contracts & tokens on Base ───────────────────────────────────────
SWAP_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481".lower()

TOKEN_NAMES = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": ("USDC", 6),
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": ("USDC.e", 6),
    "0x4200000000000000000000000000000000000006": ("WETH", 18),
    "0x0555e30da8f98308edb960aa94c0db47230d2b9c": ("WBTC", 8),
    "0x4ed4e281562193f5c8c11259d3e21839951e7d23": ("DEGEN", 18),
    "0x9401811a062933285c64d72a25e8e3cf24f3ffbe": ("AERO", 18),
    "0x88fb150bdc53a65fe94dea0c9ba0a6daf8c6e196": ("LINK", 18),
}

# Uniswap V3 exactInputSingle function selector
EXACT_INPUT_SINGLE_SIG = "0x414bf389"

# ERC-20 approve function selector
APPROVE_SIG = "0x095ea7b3"


def fmt_token(address, raw_amount):
    """Format a raw token amount with symbol and human-readable value."""
    addr = address.lower()
    if addr in TOKEN_NAMES:
        name, decimals = TOKEN_NAMES[addr]
        human = Decimal(raw_amount) / Decimal(10 ** decimals)
        return f"{human:,.{min(decimals, 8)}f} {name}"
    return f"{raw_amount} (unknown token {address[:10]}...)"


def decode_exact_input_single(input_data):
    """Decode exactInputSingle calldata into a readable dict."""
    # Skip the 4-byte selector, then ABI-decode the tuple
    # Params: (address tokenIn, address tokenOut, uint24 fee, address recipient,
    #          uint256 deadline, uint256 amountIn, uint256 amountOutMinimum,
    #          uint160 sqrtPriceLimitX96)
    data = bytes.fromhex(input_data[10:])  # strip 0x + 4-byte selector

    def read_uint(offset):
        return int.from_bytes(data[offset:offset+32], 'big')

    def read_addr(offset):
        return Web3.to_checksum_address("0x" + data[offset+12:offset+32].hex())

    token_in = read_addr(0)
    token_out = read_addr(32)
    fee = read_uint(64)
    recipient = read_addr(96)
    deadline = read_uint(128)
    amount_in = read_uint(160)
    amount_out_min = read_uint(192)
    sqrt_price_limit = read_uint(224)

    return {
        "tokenIn": token_in,
        "tokenOut": token_out,
        "fee": fee,
        "recipient": recipient,
        "deadline": deadline,
        "amountIn": amount_in,
        "amountOutMinimum": amount_out_min,
        "sqrtPriceLimitX96": sqrt_price_limit,
    }


def get_revert_reason(w3, tx, block_number):
    """Try to extract the revert reason by replaying the transaction."""
    try:
        # Replay the call at the block the tx was mined in
        call_params = {
            "from": tx["from"],
            "to": tx["to"],
            "data": tx["input"],
            "value": tx["value"],
            "gas": tx["gas"],
        }
        # Some RPCs support gasPrice in call replay
        if "gasPrice" in tx:
            call_params["gasPrice"] = tx["gasPrice"]

        w3.eth.call(call_params, block_number)
        return "(call succeeded on replay — state-dependent failure)"
    except Exception as e:
        msg = str(e)
        # Try to extract the revert reason from common formats
        if "execution reverted" in msg:
            # Look for a reason string
            if "execution reverted:" in msg:
                return msg.split("execution reverted:")[1].strip()
            # Try to decode the revert data
            if "0x" in msg:
                hex_start = msg.find("0x")
                hex_data = msg[hex_start:].split('"')[0].split("'")[0].strip()
                if len(hex_data) > 10:
                    # Check for Error(string) selector: 0x08c379a0
                    if hex_data.startswith("0x08c379a0") and len(hex_data) >= 138:
                        try:
                            str_len = int(hex_data[74:138], 16)
                            reason = bytes.fromhex(hex_data[138:138 + str_len * 2]).decode("utf-8", errors="replace")
                            return reason
                        except Exception:
                            pass
                    # Known Uniswap error selectors
                    known_errors = {
                        "0xe0a67858": "STF (SafeTransferFrom failed — likely insufficient balance or allowance)",
                        "0x739dbe52": "TF (TransferFailed — output transfer to recipient failed)",
                        "0x": "Empty revert (out of gas or low-level failure)",
                    }
                    selector = hex_data[:10]
                    if selector in known_errors:
                        return known_errors[selector]
                    return f"Revert data: {hex_data[:80]}..."
            return "execution reverted (no reason provided)"
        return f"Replay error: {msg[:200]}"


def main():
    if len(sys.argv) < 2:
        print("Usage: debug-tx <transaction_hash>")
        print("Example: debug-tx 0xaddfd33ea6b66019f49d9f5f25f504caa4a78e2ebd1ec3d116ac2e460db12fd0")
        sys.exit(1)

    tx_hash = sys.argv[1]
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash

    # Connect to an RPC
    w3 = None
    for rpc in RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
            w3.eth.block_number  # quick connectivity check
            print(f"Connected to: {rpc}")
            break
        except Exception:
            continue

    if not w3 or not w3.is_connected():
        print("ERROR: Could not connect to any Base RPC endpoint")
        sys.exit(1)

    # Fetch transaction
    print(f"\nFetching transaction {tx_hash}...")
    try:
        tx = w3.eth.get_transaction(tx_hash)
    except Exception as e:
        print(f"ERROR: Transaction not found: {e}")
        sys.exit(1)

    # Fetch receipt
    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
    except Exception as e:
        print(f"ERROR: Receipt not found (tx may be pending): {e}")
        receipt = None

    # ── Basic info ──
    print("\n" + "=" * 60)
    print("TRANSACTION DETAILS")
    print("=" * 60)
    print(f"  Hash:       {tx_hash}")
    print(f"  From:       {tx['from']}")
    print(f"  To:         {tx['to']}")
    print(f"  Value:      {Web3.from_wei(tx['value'], 'ether')} ETH")
    print(f"  Gas Limit:  {tx['gas']:,}")
    print(f"  Gas Price:  {Web3.from_wei(tx.get('gasPrice', 0), 'gwei'):.4f} gwei")
    print(f"  Nonce:      {tx['nonce']}")
    print(f"  Block:      {tx.get('blockNumber', 'pending')}")

    if receipt:
        status = "SUCCESS" if receipt["status"] == 1 else "FAILED"
        print(f"\n  Status:     {status}")
        print(f"  Gas Used:   {receipt['gasUsed']:,} / {tx['gas']:,} ({100*receipt['gasUsed']/tx['gas']:.1f}%)")
        fee_eth = Web3.from_wei(receipt["gasUsed"] * tx.get("gasPrice", 0), "ether")
        print(f"  Tx Fee:     {fee_eth:.8f} ETH")

    # ── Decode calldata ──
    input_hex = tx["input"].hex() if isinstance(tx["input"], bytes) else tx["input"]
    if not input_hex.startswith("0x"):
        input_hex = "0x" + input_hex

    if input_hex[:10] == EXACT_INPUT_SINGLE_SIG and tx["to"].lower() == SWAP_ROUTER:
        print("\n" + "-" * 60)
        print("UNISWAP V3 exactInputSingle DECODED")
        print("-" * 60)
        try:
            params = decode_exact_input_single(input_hex)
            print(f"  Token In:       {params['tokenIn']}")
            print(f"                  → {fmt_token(params['tokenIn'], params['amountIn'])}")
            print(f"  Token Out:      {params['tokenOut']}")
            print(f"  Amount Out Min: {fmt_token(params['tokenOut'], params['amountOutMinimum'])}")
            print(f"  Fee Tier:       {params['fee']} ({params['fee']/10000:.2f}%)")
            print(f"  Recipient:      {params['recipient']}")
            print(f"  Deadline:       {params['deadline']}")
            print(f"  sqrtPriceLimit: {params['sqrtPriceLimitX96']}")

            # Sanity check: is amountOutMinimum reasonable?
            in_addr = params["tokenIn"].lower()
            out_addr = params["tokenOut"].lower()
            if in_addr in TOKEN_NAMES and out_addr in TOKEN_NAMES:
                _, dec_in = TOKEN_NAMES[in_addr]
                _, dec_out = TOKEN_NAMES[out_addr]
                human_in = float(Decimal(params["amountIn"]) / Decimal(10 ** dec_in))
                human_out_min = float(Decimal(params["amountOutMinimum"]) / Decimal(10 ** dec_out))
                if human_out_min > human_in * 1000:
                    print(f"\n  ⚠ WARNING: amountOutMinimum ({human_out_min:,.2f}) is suspiciously high")
                    print(f"    relative to amountIn ({human_in:,.6f}). This likely caused the revert.")
                    print(f"    The slippage calculation may have a decimal mismatch bug.")
        except Exception as e:
            print(f"  (Could not decode calldata: {e})")

    elif input_hex[:10] == APPROVE_SIG:
        print("\n  Method: ERC-20 approve()")
    elif len(input_hex) > 10:
        print(f"\n  Method selector: {input_hex[:10]}")

    # ── Revert reason ──
    if receipt and receipt["status"] == 0:
        print("\n" + "-" * 60)
        print("REVERT REASON")
        print("-" * 60)
        reason = get_revert_reason(w3, tx, receipt["blockNumber"])
        print(f"  {reason}")

    # ── Logs ──
    if receipt and receipt.get("logs"):
        print(f"\n  Event logs: {len(receipt['logs'])} log(s) emitted")
    elif receipt:
        print("\n  Event logs: none (expected for failed transactions)")

    print()


if __name__ == "__main__":
    main()
