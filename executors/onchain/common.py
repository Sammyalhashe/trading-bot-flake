"""Constants, settings and helpers shared by the on-chain executor core and its swap routes.

A setting read by only one module is defined there instead, so patching it there takes effect.
"""
import logging
import os
import time

from web3 import Web3

from ..log_redaction import redact

EXPECTED_CHAIN_ID = 8453  # Base Mainnet

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "remaining", "type": "uint256"}], "type": "function"}
]

WETH_DEPOSIT_ABI = [{"inputs": [], "name": "deposit", "outputs": [], "stateMutability": "payable", "type": "function"}]

# Base Token Registry. With ONCHAIN_ROUTER=zerox this is also the allowlist of tradable tokens.
TOKENS = {
    "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",   # Native USDC
    "USDC.e": "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca", # Bridged USDC
    "WETH": "0x4200000000000000000000000000000000000006",
    "BTC": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",    # cbBTC
    "AERO": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
    "LINK": "0x88fb150bdc53a65fe94dea0c9ba0a6daf8c6e196",    # Chainlink
    "MORPHO": "0xbaa5cc21fd487b8fcc2f632f3f4e8d37262a0842", # Morpho DeFi token
    "ZRO": "0x6985884c4392d348587b19cb9eaaf157f13271cd",    # LayerZero token
    "VVV": "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf",    # Venice token
    "AAVE": "0x63706e401c06ac8513145b7687A14804d17f814b",
    "UNI": "0xc3De830EA07524a0761646a6a4e4be0e114a3C83",
}

# Dropped from TOKENS but possibly still held: symbol -> (address, dust threshold in raw units).
LEGACY_TOKENS = {
    "WBTC": ("0x0555E30da8f98308EdB960aa94C0Db47230d2B9c", 1000),
}

# Tokens to check during balance scan (minimal set to avoid rate limits)
BALANCE_SCAN_TOKENS = ["USDC", "WETH"]


def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


SLIPPAGE_BPS = int(os.environ.get("ONCHAIN_SLIPPAGE_BPS", "50"))
TX_DEADLINE_SECONDS = int(os.environ.get("ONCHAIN_TX_DEADLINE_SECONDS", "120"))
# Waiting past the deadline means an unmined swap can no longer fill, so a timeout is final.
RECEIPT_TIMEOUT_SECONDS = TX_DEADLINE_SECONDS + 60
ONCHAIN_KILL_SWITCH_FILE = os.environ.get(
    "ONCHAIN_KILL_SWITCH_FILE",
    os.path.join(
        os.environ.get("XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local", "state")),
        "trading-bot", "onchain_kill_switch",
    ),
)

# Known token decimals (avoids RPC calls)
KNOWN_DECIMALS = {
    "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913": 6,    # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": 6,    # USDC.e
    "0x4200000000000000000000000000000000000006": 18,   # WETH
    "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf": 8,    # cbBTC
    "0x940181a94A35A4569E4529A3CDfB74e38FD98631": 18,   # AERO
    "0x88fb150bdc53a65fe94dea0c9ba0a6daf8c6e196": 18,   # LINK
    "0xbaa5cc21fd487b8fcc2f632f3f4e8d37262a0842": 18,   # MORPHO
    "0x6985884c4392d348587b19cb9eaaf157f13271cd": 18,   # ZRO
    "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf": 18,   # VVV
    "0x63706e401c06ac8513145b7687A14804d17f814b": 18,   # AAVE
    "0xc3De830EA07524a0761646a6a4e4be0e114a3C83": 18,   # UNI
}

# Public endpoints: used only for balance reads, never for quotes or writes.
FALLBACK_RPCS = [
    "https://mainnet.base.org",
    "https://base.blockpi.network/v1/rpc/public",
    "https://1rpc.io/base",
    "https://base.meowrpc.com",
]


class KillSwitchEngaged(RuntimeError):
    pass



def onchain_kill_switch_engaged():
    return _env_flag("ONCHAIN_KILL_SWITCH") or os.path.exists(ONCHAIN_KILL_SWITCH_FILE)


def engage_onchain_kill_switch(reason):
    """Create the kill-switch file. Never raises: it runs after swaps have already confirmed."""
    try:
        directory = os.path.dirname(ONCHAIN_KILL_SWITCH_FILE)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(ONCHAIN_KILL_SWITCH_FILE, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {reason}\n")
    except OSError as e:
        logging.critical(f"Could not write kill switch {ONCHAIN_KILL_SWITCH_FILE} ({e}); set ONCHAIN_KILL_SWITCH=true. Reason: {reason}")


def _same_address(a, b):
    return isinstance(a, str) and isinstance(b, str) and a.lower() == b.lower()


def _receipt_fee_wei(receipt):
    """L2 execution fee plus Base's L1 data fee (l1Fee, hex or int) from a receipt; 0 if unknown."""
    if not receipt:
        return 0
    l1_fee = receipt.get("l1Fee") or 0
    l1_fee = int(l1_fee, 16) if isinstance(l1_fee, str) else int(l1_fee)
    return int(receipt.get("gasUsed") or 0) * int(receipt.get("effectiveGasPrice") or 0) + l1_fee


class _RawContractCall:
    """The estimate_gas/build_transaction surface _send_contract_tx expects, for API-built calldata."""
    def __init__(self, w3, to, data, api_gas=None):
        self._w3 = w3
        self.to = Web3.to_checksum_address(to)
        self.data = data
        self.api_gas = api_gas

    def estimate_gas(self, params):
        gas = self._w3.eth.estimate_gas({**params, "to": self.to, "data": self.data})
        if self.api_gas and gas > self.api_gas:
            logging.warning(f"Our gas estimate {gas} exceeds the API estimate {self.api_gas}")
        return gas

    def build_transaction(self, params):
        return {**params, "to": self.to, "data": self.data}


def retry_rpc_call(func, max_retries=4, base_delay=2.0):
    """Execute an RPC call with exponential backoff retry on 429/rate limit errors."""
    last_exception = None
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            last_exception = e
            error_str = str(e)
            is_rate_limit = "429" in error_str or "Too Many Requests" in error_str
            is_timeout = "timeout" in error_str.lower() or "timed out" in error_str.lower()

            if attempt < max_retries - 1 and (is_rate_limit or is_timeout):
                delay = base_delay * (2 ** attempt)
                logging.warning(f"RPC call failed (attempt {attempt + 1}/{max_retries}), retrying in {delay:.1f}s: {redact(error_str)[:100]}")
                time.sleep(delay)
            else:
                raise
    raise last_exception
