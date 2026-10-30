"""On-chain executor for Base mainnet, swapping through Aerodrome (Solidly-style v2 AMM) or the
0x Swap API v2 (AllowanceHolder flow).

Routers (ONCHAIN_ROUTER):
  aerodrome  (default) Router.swapExactTokensForTokens with an on-chain quote; approvals are
             for the exact sell amount.
  zerox      0x is an off-chain quote source only. Responses must carry a valid 0x Ed25519
             signature, target the pinned AllowanceHolder, and exec() into the Settler that the
             on-chain registry names; approvals are for the exact sell amount. Tokens outside
             TOKENS are refused. On any 0x failure buys are refused; sells into USDC (exits) fall
             back to the Aerodrome path with all of its guards.

RPC trust model:
  * Trusted RPC (``trusted_rpc_url`` arg, else ``BASE_TRUSTED_RPC_URL``, else the primary
    ``rpc_url``): every read that determines what we sign (quotes -> amountOutMinimum, decimals,
    allowances, nonces, gas, chain id) plus all writes and receipt lookups. It never fails over.
    Point this at a paid/private endpoint you control.
  * Read-only failover pool (primary ``rpc_url`` + ``FALLBACK_RPCS``): balance reads only.

Safety knobs (env):
  ONCHAIN_SLIPPAGE_BPS         slippage tolerance applied to the live quote (default 50 = 0.5%)
  ONCHAIN_TX_DEADLINE_SECONDS  router deadline relative to the latest block (default 120)
  ONCHAIN_MAX_SWAP_USD         cap on USDC spent per buy; sells are never capped (default 100)
  ONCHAIN_MAX_PRICE_DEVIATION_PCT  reject when the quoted fill is this much worse than the
                               caller's reference price, e.g. a thin/junk pool (default 2)
  ONCHAIN_MAX_PRIORITY_FEE_GWEI  ceiling on the RPC-suggested priority fee (default 0.1)
  ONCHAIN_KILL_SWITCH          "true" disables all live swaps
  ONCHAIN_KILL_SWITCH_FILE     if this file exists, all live swaps are disabled
                               (default $XDG_STATE_HOME/trading-bot/onchain_kill_switch)
                               Both are re-checked immediately before every signature.
  ONCHAIN_ROUTER               aerodrome | zerox (default aerodrome)
  ONCHAIN_ROUND_TRIP_FEE_PCT   round-trip cost used for this executor's fee floor and PnL
                               (default 0.006: pool fees + 0x fee + slippage, conservative)
  ZEROX_API_KEY                0x API key (zerox router)
  ZEROX_MAX_QUOTE_AGE_BLOCKS   refuse a 0x quote sampled more than this many blocks ago (default 3)
  ZEROX_SIGNING_KEY_ID / ZEROX_SIGNING_KEY_X  trusted 0x response-signing key (see zerox_client)
"""
import logging
import os
import threading
import time
from decimal import Decimal

from eth_abi import decode as abi_decode
from eth_abi.exceptions import DecodingError
from web3 import Web3
from web3.exceptions import TimeExhausted, TransactionNotFound

from .log_redaction import install_filter, redact, register_secret, register_url, url_host
from .zerox_client import ZeroExClient, ZeroExError

EXPECTED_CHAIN_ID = 8453  # Base Mainnet

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "remaining", "type": "uint256"}], "type": "function"}
]

# Verified on Base mainnet via eth_call: Router.defaultFactory() == PoolFactory, selectors
# 0x5509a1ac / 0xcac88ea9 present, and deadline/minOut reverts confirmed by simulation.
AERODROME_ROUTER_ADDRESS = "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"
AERODROME_POOL_FACTORY_ADDRESS = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"

_AERODROME_ROUTES_PARAM = {
    "components": [
        {"internalType": "address", "name": "from", "type": "address"},
        {"internalType": "address", "name": "to", "type": "address"},
        {"internalType": "bool", "name": "stable", "type": "bool"},
        {"internalType": "address", "name": "factory", "type": "address"}
    ],
    "internalType": "struct IRouter.Route[]",
    "name": "routes",
    "type": "tuple[]"
}

AERODROME_ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            _AERODROME_ROUTES_PARAM
        ],
        "name": "getAmountsOut",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            _AERODROME_ROUTES_PARAM,
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"}
        ],
        "name": "swapExactTokensForTokens",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

# Pinned per the 0x-settler README (Cancun chains incl. Base). Settler itself is never pinned:
# it is read from the registry's taker-submitted feature before every swap.
ALLOWANCE_HOLDER_ADDRESS = "0x0000000000001fF3684f28c67538d4D072C22734"
ALLOWANCE_HOLDER_EXEC_SELECTOR = "0x2213bc0b"  # exec(address,address,uint256,address,bytes)
# ISettlerTakerSubmitted.execute(AllowedSlippage(recipient, buyToken, minAmountOut), bytes[] actions, bytes32)
SETTLER_EXECUTE_SELECTOR = "0x1fff991f"
SETTLER_REGISTRY_ADDRESS = "0x00000000000004533Fe15556B1E086BB1A72cEae"
SETTLER_TAKER_SUBMITTED_FEATURE = 2
SETTLER_REGISTRY_ABI = [
    {"inputs": [{"name": "tokenId", "type": "uint256"}], "name": "ownerOf", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "featureId", "type": "uint128"}], "name": "prev", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
]

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
MAX_SLIPPAGE_BPS = 300
MIN_GAS_ETH = float(os.environ.get("MIN_GAS_ETH", "0.001"))
TX_DEADLINE_SECONDS = int(os.environ.get("ONCHAIN_TX_DEADLINE_SECONDS", "120"))
# Waiting past the deadline means an unmined swap can no longer fill, so a timeout is final.
RECEIPT_TIMEOUT_SECONDS = TX_DEADLINE_SECONDS + 60
GAS_LIMIT_BUFFER_PCT = 125
MAX_SWAP_USD = Decimal(os.environ.get("ONCHAIN_MAX_SWAP_USD", "100"))
MAX_PRICE_DEVIATION_PCT = Decimal(os.environ.get("ONCHAIN_MAX_PRICE_DEVIATION_PCT", "2"))
MAX_PRIORITY_FEE_WEI = Web3.to_wei(Decimal(os.environ.get("ONCHAIN_MAX_PRIORITY_FEE_GWEI", "0.1")), "gwei")
ONCHAIN_ROUTER = os.environ.get("ONCHAIN_ROUTER", "aerodrome").strip().lower()
ONCHAIN_ROUTERS = ("aerodrome", "zerox")
ONCHAIN_ROUND_TRIP_FEE_PCT = Decimal(os.environ.get("ONCHAIN_ROUND_TRIP_FEE_PCT", "0.006"))
ZEROX_MAX_QUOTE_AGE_BLOCKS = int(os.environ.get("ZEROX_MAX_QUOTE_AGE_BLOCKS", "3"))
PRICE_PROBE_USDC = 10
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


class ZeroExQuoteRejected(ZeroExError):
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
            logging.warning(f"Our gas estimate {gas} exceeds the 0x estimate {self.api_gas}")
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


def _make_web3(url):
    return Web3(Web3.HTTPProvider(url, request_kwargs={'timeout': 30}))


class EthereumExecutor:
    """Swaps ERC-20s on Base via the Aerodrome Router or the 0x Swap API (ONCHAIN_ROUTER)."""
    def __init__(self, rpc_url, private_key, trading_mode="paper", trusted_rpc_url=None):
        if ONCHAIN_ROUTER not in ONCHAIN_ROUTERS:
            raise ValueError(f"ONCHAIN_ROUTER={ONCHAIN_ROUTER!r} is not one of {ONCHAIN_ROUTERS}")
        self.onchain_router = ONCHAIN_ROUTER
        self.round_trip_fee_pct = ONCHAIN_ROUND_TRIP_FEE_PCT
        zerox_api_key = os.environ.get("ZEROX_API_KEY")
        install_filter()
        register_secret(zerox_api_key)
        register_secret(private_key)
        if private_key:
            register_secret(private_key.removeprefix("0x"))
        self.zerox = ZeroExClient(zerox_api_key) if self.onchain_router == "zerox" and zerox_api_key else None
        if self.onchain_router == "zerox" and self.zerox is None:
            logging.error("ONCHAIN_ROUTER=zerox but ZEROX_API_KEY is not set: buys refused, exits use Aerodrome")
        self._allowance_holder_verified = False
        self.primary_rpc_url = rpc_url
        self.trusted_rpc_url = trusted_rpc_url or os.environ.get("BASE_TRUSTED_RPC_URL") or rpc_url
        register_url(rpc_url)
        register_url(self.trusted_rpc_url)
        self.private_key = private_key
        self.trading_mode = trading_mode

        self._current_rpc_index = 0
        self._rpc_endpoints = [rpc_url] + [r for r in FALLBACK_RPCS if r != rpc_url]

        self._decimals_cache = {}
        self._reference_prices = {}
        self._next_nonce = None
        self._last_sent_hash = None
        self._nonce_lock = threading.RLock()
        self._chain_verified = False

        self.w3 = _make_web3(rpc_url)
        self.trusted_w3 = _make_web3(self.trusted_rpc_url)
        logging.info(f"Read RPC: {url_host(rpc_url)} | trusted (quotes/writes) RPC: {url_host(self.trusted_rpc_url)}")
        if trading_mode == "live" and self.trusted_rpc_url in FALLBACK_RPCS:
            logging.warning(
                "Trusted RPC is a public endpoint; set BASE_TRUSTED_RPC_URL to a private RPC "
                "so quotes that set amountOutMinimum cannot be spoofed or rate-limited."
            )

        self.account = self.trusted_w3.eth.account.from_key(private_key) if private_key else None
        if self.account:
            logging.info(f"Using Base Wallet: {self.account.address}")

        self.router_contract = self.trusted_w3.eth.contract(
            address=Web3.to_checksum_address(AERODROME_ROUTER_ADDRESS),
            abi=AERODROME_ROUTER_ABI
        )

    def _rotate_rpc(self):
        """Rotate the read-only provider to the next endpoint. The trusted provider never rotates."""
        old_index = self._current_rpc_index
        self._current_rpc_index = (self._current_rpc_index + 1) % len(self._rpc_endpoints)
        new_rpc = self._rpc_endpoints[self._current_rpc_index]
        logging.warning(f"Rotating read RPC from index {old_index} to {self._current_rpc_index}: {url_host(new_rpc)}")
        self.w3 = _make_web3(new_rpc)

    def _call_with_failover(self, func):
        """Run a read against self.w3 with failover. Only for reads that do not affect what we sign."""
        last_exception = None
        for _ in range(len(self._rpc_endpoints)):
            try:
                return func()
            except Exception as e:
                error_str = str(e).lower()
                is_connection_error = any(kw in error_str for kw in [
                    "connection", "timeout", "timed out", "connectionerror",
                    "maxretryerror", "remotedisconnected", "eof",
                ])
                if is_connection_error:
                    last_exception = e
                    logging.warning(f"RPC connection error, attempting failover: {redact(str(e))[:100]}")
                    self._rotate_rpc()
                else:
                    raise
        raise last_exception

    def _get_decimals(self, token_address):
        """Token decimals from cache/registry, else the trusted RPC. Raises rather than guessing."""
        addr_lower = token_address.lower()
        if addr_lower in self._decimals_cache:
            return self._decimals_cache[addr_lower]
        if token_address in KNOWN_DECIMALS:
            dec = KNOWN_DECIMALS[token_address]
            self._decimals_cache[addr_lower] = dec
            return dec

        def _fetch_decimals():
            contract = self.trusted_w3.eth.contract(
                address=Web3.to_checksum_address(token_address),
                abi=ERC20_ABI
            )
            return contract.functions.decimals().call()
        dec = retry_rpc_call(_fetch_decimals)
        self._decimals_cache[addr_lower] = dec
        return dec

    def _verify_chain(self):
        if self._chain_verified:
            return
        chain_id = retry_rpc_call(lambda: self.trusted_w3.eth.chain_id)
        if chain_id != EXPECTED_CHAIN_ID:
            raise RuntimeError(f"Trusted RPC is on chain {chain_id}, expected {EXPECTED_CHAIN_ID}")
        self._chain_verified = True

    def _latest_block(self):
        return retry_rpc_call(lambda: self.trusted_w3.eth.get_block("latest"))

    def _fee_fields(self):
        """EIP-1559 fees: 2x latest base fee headroom plus the node's tip, clamped to MAX_PRIORITY_FEE_WEI."""
        base_fee = self._latest_block()["baseFeePerGas"]
        suggested_tip = retry_rpc_call(lambda: self.trusted_w3.eth.max_priority_fee)
        priority_fee = min(suggested_tip, MAX_PRIORITY_FEE_WEI)
        if priority_fee < suggested_tip:
            logging.warning(f"RPC suggested priority fee {suggested_tip} wei; clamped to {MAX_PRIORITY_FEE_WEI}")
        return {"maxFeePerGas": 2 * base_fee + priority_fee, "maxPriorityFeePerGas": priority_fee}

    def _transaction_count(self, block_identifier):
        return retry_rpc_call(
            lambda: self.trusted_w3.eth.get_transaction_count(self.account.address, block_identifier)
        )

    def _get_nonce(self):
        """Pending nonce, floored by our own last broadcast in case the RPC's pending view lags.

        The floor is only trusted while that last broadcast is still known to the node; once it
        has vanished, the chain's pending count is authoritative again. Call with _nonce_lock held.
        """
        pending = self._transaction_count("pending")
        if self._next_nonce is None or self._next_nonce <= pending:
            return pending
        if self._last_sent_hash is not None and self._is_tx_known(self._last_sent_hash):
            return self._next_nonce
        logging.warning(f"Local nonce floor {self._next_nonce} > chain pending {pending} and last tx is gone; resyncing")
        self._next_nonce = None
        self._last_sent_hash = None
        return pending

    def _resync_nonce(self):
        with self._nonce_lock:
            self._last_sent_hash = None
            try:
                self._next_nonce = self._transaction_count("pending")
            except Exception as e:
                logging.warning(f"Nonce resync failed ({e}); next send will read pending nonce from chain")
                self._next_nonce = None

    def _has_unconfirmed_tx(self):
        if self._transaction_count("pending") > self._transaction_count("latest"):
            return True
        return self._last_sent_hash is not None and self._lookup_tx_status(self._last_sent_hash)[0] == "pending"

    def _is_tx_known(self, tx_hash):
        try:
            self.trusted_w3.eth.get_transaction(tx_hash)
            return True
        except TransactionNotFound:
            return False

    def _broadcast(self, signed_tx):
        try:
            return self.trusted_w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        except Exception as e:
            error_msg = str(e).lower()
            if "already known" in error_msg:
                return signed_tx.hash
            if "in-flight" in error_msg:
                logging.warning("In-flight transaction limit reached, waiting 10s...")
                time.sleep(10)
                return self.trusted_w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            raise

    def _send_contract_tx(self, contract_fn, max_attempts=3):
        """Simulate (estimate_gas), sign and broadcast. Returns the tx hash.

        A revert during estimation aborts before anything is broadcast. Re-signing with a fresh
        nonce only happens when the previously signed tx is provably unknown to the node. The
        nonce lock is held from nonce read to local increment so concurrent callers never share one.
        """
        with self._nonce_lock:
            return self._send_contract_tx_locked(contract_fn, max_attempts)

    def _send_contract_tx_locked(self, contract_fn, max_attempts):
        for attempt in range(max_attempts):
            nonce = self._get_nonce()
            gas_estimate = retry_rpc_call(
                lambda: contract_fn.estimate_gas({"from": self.account.address, "value": 0})
            )
            tx = contract_fn.build_transaction({
                "from": self.account.address,
                "nonce": nonce,
                "gas": gas_estimate * GAS_LIMIT_BUFFER_PCT // 100,
                "chainId": EXPECTED_CHAIN_ID,
                "value": 0,
                **self._fee_fields(),
            })
            if onchain_kill_switch_engaged():
                raise KillSwitchEngaged("kill switch engaged")
            signed_tx = self.account.sign_transaction(tx)
            try:
                retry_rpc_call(lambda: self._broadcast(signed_tx))
            except Exception as e:
                error_msg = str(e).lower()
                try:
                    known = self._is_tx_known(signed_tx.hash)
                except Exception as lookup_error:
                    logging.error(f"Broadcast outcome ambiguous ({e}); lookup failed ({lookup_error}). Reconciling by hash.")
                    known = True
                if known:
                    logging.warning(f"Broadcast raised but tx {signed_tx.hash.hex()} may be live: {e}")
                elif ("nonce too low" in error_msg or "underpriced" in error_msg) and attempt < max_attempts - 1:
                    logging.warning(f"Nonce/underpriced error (attempt {attempt + 1}), re-signing: {e}")
                    self._next_nonce = None
                    time.sleep(2)
                    continue
                else:
                    raise
            self._next_nonce = nonce + 1
            self._last_sent_hash = signed_tx.hash
            return signed_tx.hash
        raise RuntimeError("failed to broadcast transaction after retries")

    def _lookup_tx_status(self, tx_hash):
        """Returns one of confirmed / reverted / pending / dropped / unknown, plus the receipt if mined."""
        try:
            receipt = self.trusted_w3.eth.get_transaction_receipt(tx_hash)
            return ("confirmed" if receipt["status"] == 1 else "reverted"), receipt
        except TransactionNotFound:
            pass
        except Exception as e:
            logging.error(f"Receipt lookup failed for {tx_hash.hex()}: {e}")
            return "unknown", None
        try:
            return ("pending" if self._is_tx_known(tx_hash) else "dropped"), None
        except Exception as e:
            logging.error(f"Transaction lookup failed for {tx_hash.hex()}: {e}")
            return "unknown", None

    def _await_receipt(self, tx_hash, timeout):
        """Wait for a receipt; on timeout or RPC error, reconcile by hash instead of assuming failure."""
        try:
            receipt = self.trusted_w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
            return ("confirmed" if receipt["status"] == 1 else "reverted"), receipt
        except TimeExhausted:
            logging.warning(f"No receipt for {tx_hash.hex()} after {timeout}s, reconciling by hash")
        except Exception as e:
            logging.warning(f"Error waiting for receipt of {tx_hash.hex()}: {e}; reconciling by hash")
        status, receipt = self._lookup_tx_status(tx_hash)
        if status in ("dropped", "unknown"):
            self._resync_nonce()
        return status, receipt

    def _check_balance(self, balances, symbol, address):
        """Helper to fetch and add ERC20 balance if non-zero. Uses cached decimals."""
        try:
            addr = Web3.to_checksum_address(address)

            def _fetch_balance():
                contract = self.w3.eth.contract(address=addr, abi=ERC20_ABI)
                return contract.functions.balanceOf(self.account.address).call()

            raw_bal = retry_rpc_call(_fetch_balance)
            if raw_bal > 0:
                decimals = self._get_decimals(address)
                val = float(raw_bal) / (10 ** decimals)

                if symbol == "USDC" or symbol == "USDC.e":
                    balances["cash"]["USDC"] = balances["cash"].get("USDC", 0.0) + val
                elif symbol == "WETH":
                    balances["crypto"]["ETH"] = balances["crypto"].get("ETH", 0.0) + val
                else:
                    balances["crypto"][symbol] = val
        except Exception as e:
            logging.debug(f"Could not check balance for {symbol} ({address}): {e}")

    def get_balances(self, extra_tokens=None):
        """Fetch ETH and key ERC20 balances on Base. Minimizes RPC calls.
        Uses RPC failover for connection errors."""
        def _do_get_balances():
            if not self.account:
                return {
                    "available": {"cash": {"USDC": 0.0}, "crypto": {}},
                    "total": {"cash": {"USDC": 0.0}, "crypto": {}}
                }

            # For Ethereum, available and total are the same (no held limit orders)
            base_balances = {"cash": {"USDC": 0.0}, "crypto": {}}

            try:
                def _fetch_eth_balance():
                    return self.w3.eth.get_balance(self.account.address)
                eth_bal = retry_rpc_call(_fetch_eth_balance)
                base_balances["crypto"]["ETH_NATIVE"] = float(self.w3.from_wei(eth_bal, 'ether'))
            except Exception as e:
                logging.debug(f"Could not fetch native ETH balance: {e}")

            tokens_to_scan = set(BALANCE_SCAN_TOKENS)
            if extra_tokens:
                tokens_to_scan.update(extra_tokens)

            for symbol in tokens_to_scan:
                if symbol in TOKENS:
                    self._check_balance(base_balances, symbol, TOKENS[symbol])

            return {
                "available": base_balances,
                "total": base_balances
            }

        return self._call_with_failover(_do_get_balances)

    def warn_about_stranded_tokens(self):
        """Warn about held tokens that were removed from TOKENS; the bot no longer manages or sells them."""
        if not self.account:
            return
        for symbol, (address, dust) in LEGACY_TOKENS.items():
            try:
                raw = self._call_with_failover(lambda: self.w3.eth.contract(address=address, abi=ERC20_ABI)
                                               .functions.balanceOf(self.account.address).call())
            except Exception as e:
                logging.warning(f"Could not check for stranded {symbol} ({address}): {e}")
                continue
            if raw > dust:
                logging.warning(
                    f"STRANDED TOKEN: wallet holds {raw} raw units of {symbol} ({address}), which is no longer "
                    f"in the token registry. The bot will not manage or sell it; move or swap it manually."
                )

    def get_market_data(self, product_id, window):
        return None

    def get_product_details(self, product_id):
        """Approximate USDC price per token from a small live Aerodrome quote (includes pool fee)."""
        quote_currency = product_id.partition("-")[2]
        token = self.get_token_address(product_id)
        if not token or quote_currency.upper() != "USDC":
            return None
        try:
            probe_raw = PRICE_PROBE_USDC * 10 ** self._get_decimals(TOKENS["USDC"])
            quote = self.get_quote(TOKENS["USDC"], token, probe_raw)
            if quote is None:
                return None
            tokens_out = Decimal(quote[1]) / Decimal(10 ** self._get_decimals(token))
            price = Decimal(PRICE_PROBE_USDC) / tokens_out
            return {"price": str(float(price)), "quote_increment": "0.01", "base_increment": "0.00000001"}
        except Exception as e:
            logging.error(f"Error fetching Aerodrome price for {product_id}: {e}")
            return None

    def get_token_address(self, product_id):
        """Helper to find on-chain address for a product (e.g., ETH-USDC)."""
        asset = product_id.split("-")[0].upper()
        if asset == "ETH":
            return TOKENS["WETH"]
        if asset in TOKENS:
            return TOKENS[asset]
        extra = os.environ.get("EXTRA_TOKENS")
        if extra:
            for item in extra.split(","):
                try:
                    addr, sym = item.split(":")
                    if sym.strip().upper() == asset.upper():
                        return addr.strip()
                except: pass
        return None

    def _approve_token(self, token_address, amount, spender=AERODROME_ROUTER_ADDRESS):
        """Approve spender (default the Aerodrome Router) for exactly `amount`, always reading the
        live allowance. Returns True if already approved, the approval tx hash (hex) if one was
        mined, or None on failure."""
        if self.trading_mode != "live" or not self.account:
            return True

        spender = Web3.to_checksum_address(spender)

        try:
            token_contract = self.trusted_w3.eth.contract(
                address=Web3.to_checksum_address(token_address),
                abi=ERC20_ABI
            )

            allowance = retry_rpc_call(
                lambda: token_contract.functions.allowance(self.account.address, spender).call()
            )
            logging.info(f"Current allowance for {token_address} on {spender}: {allowance}")

            if allowance >= amount:
                return True

            # Some tokens (like USDT) require resetting to 0 first if allowance is non-zero
            if allowance > 0:
                logging.info(f"Resetting allowance for {token_address} from {allowance} to 0 before approving {amount}")
                reset_hash = self._send_contract_tx(token_contract.functions.approve(spender, 0))
                status, _ = self._await_receipt(reset_hash, RECEIPT_TIMEOUT_SECONDS)
                if status != "confirmed":
                    logging.error(f"Allowance reset not confirmed ({status}). Tx: {reset_hash.hex()}")
                    return None

            logging.info(f"Approving {amount} of {token_address} for {spender}")
            approve_hash = self._send_contract_tx(token_contract.functions.approve(spender, amount))
            status, receipt = self._await_receipt(approve_hash, RECEIPT_TIMEOUT_SECONDS)

            if status == "confirmed":
                logging.info(f"Approved {amount} for {token_address}. Tx: {approve_hash.hex()}")
                return approve_hash.hex()
            logging.error(f"Approve {amount} not confirmed ({status}): {receipt}")
            return None
        except KillSwitchEngaged:
            raise
        except Exception as e:
            logging.error(f"Approve failed: {e}")
            return None

    def _candidate_routes(self, token_in, token_out):
        factory = Web3.to_checksum_address(AERODROME_POOL_FACTORY_ADDRESS)
        a = Web3.to_checksum_address(token_in)
        b = Web3.to_checksum_address(token_out)
        weth = Web3.to_checksum_address(TOKENS["WETH"])
        candidates = [[(a, b, stable, factory)] for stable in (False, True)]
        if weth not in (a, b):
            candidates += [
                [(a, weth, stable_in, factory), (weth, b, stable_out, factory)]
                for stable_in in (False, True)
                for stable_out in (False, True)
            ]
        return candidates

    def get_quote(self, token_in, token_out, amount_in):
        """Best Aerodrome route by Router.getAmountsOut on the trusted RPC.

        Returns (routes, expected_amount_out) or None. Non-existent pools quote 0 on Aerodrome,
        so zero outputs are discarded rather than treated as a price.
        """
        best = None
        for routes in self._candidate_routes(token_in, token_out):
            try:
                amounts = retry_rpc_call(
                    lambda: self.router_contract.functions.getAmountsOut(amount_in, routes).call()
                )
            except Exception as e:
                logging.debug(f"getAmountsOut failed for {routes}: {e}")
                continue
            amount_out = amounts[-1]
            if amount_out > 0 and (best is None or amount_out > best[1]):
                best = (routes, amount_out)
        if best is None:
            logging.warning(f"No Aerodrome quote for {amount_in} of {token_in} -> {token_out}")
        return best

    def _amount_out_minimum(self, expected_out, slippage_bps=None):
        bps = SLIPPAGE_BPS if slippage_bps is None else slippage_bps
        if not 0 < bps <= MAX_SLIPPAGE_BPS:
            raise ValueError(f"slippage {bps} bps outside (0, {MAX_SLIPPAGE_BPS}]")
        return expected_out * (10_000 - bps) // 10_000

    def _exceeds_position_cap(self, token_in, amount_in):
        if token_in.lower() != TOKENS["USDC"].lower():
            return False
        usd = Decimal(amount_in) / Decimal(10 ** self._get_decimals(TOKENS["USDC"]))
        return usd > MAX_SWAP_USD

    def _adverse_price_deviation(self, token_in, token_out, amount_in, expected_out, reference_price):
        """Fraction by which the quoted fill is worse than reference_price (USDC per token).
        Returns None when the pair is not priced in USDC."""
        usdc = TOKENS["USDC"]
        usdc_unit = Decimal(10 ** self._get_decimals(usdc))
        if token_in.lower() == usdc.lower():
            tokens = Decimal(expected_out) / Decimal(10 ** self._get_decimals(token_out))
            fill_price = (Decimal(amount_in) / usdc_unit) / tokens
            return fill_price / reference_price - 1
        if token_out.lower() == usdc.lower():
            tokens = Decimal(amount_in) / Decimal(10 ** self._get_decimals(token_in))
            fill_price = (Decimal(expected_out) / usdc_unit) / tokens
            return 1 - fill_price / reference_price
        return None

    def _reference_price_refusal(self, token_in, token_out, amount_in, expected_out, reference_price, venue):
        """Error string when the quoted fill is worse than reference_price (USDC per token) by more
        than MAX_PRICE_DEVIATION_PCT, or cannot be compared with it; None when within bounds."""
        reference = Decimal(str(reference_price))
        deviation = self._adverse_price_deviation(token_in, token_out, amount_in, expected_out, reference) if reference > 0 else None
        if deviation is None or deviation * 100 > MAX_PRICE_DEVIATION_PCT:
            logging.error(
                f"{venue} quote {expected_out} for {amount_in} vs reference price {reference_price}: "
                f"adverse deviation {deviation} exceeds {MAX_PRICE_DEVIATION_PCT}%; refusing swap"
            )
            return "quote deviates from reference price"
        return None

    def execute_swap(self, token_in, token_out, amount_in, recipient, reference_price=None):
        """Exact-input swap through the configured ONCHAIN_ROUTER.

        Aerodrome: refuses to trade without a live quote; amountOutMin = quote less SLIPPAGE_BPS
        and the router enforces TX_DEADLINE_SECONDS. With reference_price (USDC per token), also
        refuses quotes worse than it by more than MAX_PRICE_DEVIATION_PCT. 0x: see
        _execute_zerox_swap; exits (sells into USDC) fall back to Aerodrome when 0x is unusable.
        A result with "pending": True means the outcome could not be confirmed; callers must not
        resubmit.
        """
        if self.trading_mode != "live":
            logging.info(f"[PAPER] Would swap {amount_in} of {token_in} for {token_out}")
            return {"success": True, "tx_hash": "paper"}

        if onchain_kill_switch_engaged():
            logging.error("On-chain kill switch engaged; refusing swap")
            return {"success": False, "error": "kill switch engaged"}
        if amount_in <= 0:
            return {"success": False, "error": "amount_in must be positive"}

        if self.onchain_router != "zerox":
            return self._execute_aerodrome_swap(token_in, token_out, amount_in, recipient, reference_price)

        registry = {address.lower() for address in TOKENS.values()}
        if token_in.lower() not in registry or token_out.lower() not in registry:
            logging.error(f"Swap {token_in} -> {token_out} involves a token outside the registry; refusing")
            return {"success": False, "error": "token not in registry allowlist"}
        result = self._execute_zerox_swap(token_in, token_out, amount_in, recipient, reference_price)
        if not result.pop("zerox_unavailable", False) or token_out.lower() != TOKENS["USDC"].lower():
            return result
        logging.warning(f"0x route unusable for exit sell ({result['error']}); falling back to Aerodrome")
        return self._execute_aerodrome_swap(token_in, token_out, amount_in, recipient, reference_price)

    def _preflight(self, token_in, amount_in):
        """Checks shared by both routers. Returns a refusal result, or None to proceed."""
        if self._exceeds_position_cap(token_in, amount_in):
            logging.error(f"Swap of {amount_in} raw USDC exceeds ONCHAIN_MAX_SWAP_USD={MAX_SWAP_USD}")
            return {"success": False, "error": "position size cap exceeded"}

        self._verify_chain()
        eth_balance = float(Web3.from_wei(
            retry_rpc_call(lambda: self.trusted_w3.eth.get_balance(self.account.address)), 'ether'
        ))
        if eth_balance < MIN_GAS_ETH:
            logging.error(f"Insufficient ETH for gas: {eth_balance:.6f} ETH < {MIN_GAS_ETH} ETH minimum")
            return {"success": False, "error": "insufficient gas"}
        return None

    def _in_flight_refusal(self):
        logging.error("Account has an unconfirmed transaction in flight; refusing to stack another swap")
        return {"success": False, "error": "unconfirmed transaction in flight"}

    def _execute_aerodrome_swap(self, token_in, token_out, amount_in, recipient, reference_price):
        try:
            refusal = self._preflight(token_in, amount_in)
            if refusal:
                return refusal

            # Held from the in-flight check through broadcast so concurrent swaps cannot both pass it.
            with self._nonce_lock:
                if self._has_unconfirmed_tx():
                    return self._in_flight_refusal()

                quote = self.get_quote(token_in, token_out, amount_in)
                if quote is None:
                    return {"success": False, "error": "no quote available; refusing to swap without slippage protection"}

                approval_result = self._approve_token(token_in, amount_in)
                if approval_result is None:
                    logging.error(f"Token approval failed for {token_in}, cannot execute swap")
                    return {"success": False, "error": "approval failed"}
                if approval_result is not True:
                    quote = self.get_quote(token_in, token_out, amount_in)
                    if quote is None:
                        return {"success": False, "error": "no quote available after approval"}

                routes, expected_out = quote
                if reference_price is not None:
                    refusal = self._reference_price_refusal(token_in, token_out, amount_in, expected_out, reference_price, "Aerodrome")
                    if refusal:
                        return {"success": False, "error": refusal}

                amount_out_min = self._amount_out_minimum(expected_out)
                if amount_out_min <= 0:
                    return {"success": False, "error": "amountOutMinimum rounds to zero; refusing swap"}

                deadline = self._latest_block()["timestamp"] + TX_DEADLINE_SECONDS
                logging.info(
                    f"Aerodrome swap: in={amount_in} expected={expected_out} min={amount_out_min} "
                    f"({SLIPPAGE_BPS}bps) hops={len(routes)} deadline={deadline}"
                )
                swap_fn = self.router_contract.functions.swapExactTokensForTokens(
                    amount_in, amount_out_min, routes, Web3.to_checksum_address(recipient), deadline
                )
                tx_hash = self._send_contract_tx(swap_fn)
        except Exception as e:
            logging.error(f"Swap not broadcast: {e}")
            return {"success": False, "error": str(e)}

        return self._await_swap(tx_hash, amount_in, amount_out_min)[0]

    def _execute_zerox_swap(self, token_in, token_out, amount_in, recipient, reference_price):
        """Exact-input swap with 0x AllowanceHolder calldata, signed and sent by us.

        Refuses unless the signed /quote targets the pinned AllowanceHolder, exec()s into the
        registry's current (or, during dwell, previous) Settler for our sell token and amount,
        sends no ETH, sets minBuyAmount at or above our slippage floor, was sampled within
        ZEROX_MAX_QUOTE_AGE_BLOCKS, and fills within MAX_PRICE_DEVIATION_PCT of reference_price.
        Failures attributable to 0x carry "zerox_unavailable" so execute_swap can fall back.
        """
        if not _same_address(recipient, self.account.address):
            return {"success": False, "error": "0x swaps must pay our own wallet"}
        if reference_price is None or Decimal(str(reference_price)) <= 0:
            logging.error(f"No usable reference price ({reference_price}); refusing unguarded 0x swap")
            return {"success": False, "error": "no reference price; refusing unguarded swap"}
        try:
            refusal = self._preflight(token_in, amount_in)
            if refusal:
                return refusal

            with self._nonce_lock:
                if self._has_unconfirmed_tx():
                    return self._in_flight_refusal()

                indicative = self._zerox_request("price", token_in, token_out, amount_in)
                self._check_zerox_response(indicative, token_in, token_out, amount_in, reference_price, firm=False)

                if self._approve_token(token_in, amount_in, spender=ALLOWANCE_HOLDER_ADDRESS) is None:
                    logging.error(f"Token approval failed for {token_in}, cannot execute swap")
                    return {"success": False, "error": "approval failed"}

                quote = self._zerox_request("quote", token_in, token_out, amount_in)
                our_min = self._check_zerox_response(quote, token_in, token_out, amount_in, reference_price, firm=True)
                swap_call = self._check_zerox_transaction(quote, token_in, token_out, amount_in, our_min)
                self._check_zerox_freshness(quote)
                logging.info(
                    f"0x swap: in={amount_in} buy={quote['buyAmount']} minBuy={quote['minBuyAmount']} "
                    f"ourMin={our_min} zeroExFee={(quote.get('fees') or {}).get('zeroExFee')} "
                    f"route={[f.get('source') for f in (quote.get('route') or {}).get('fills', [])]} zid={quote.get('zid')}"
                )
                tx_hash = self._send_contract_tx(swap_call)
        except ZeroExError as e:
            logging.error(f"0x route refused; nothing signed: {e}")
            return {"success": False, "error": f"0x: {e}", "zerox_unavailable": True}
        except Exception as e:
            logging.error(f"Swap not broadcast: {e}")
            return {"success": False, "error": str(e)}

        result, receipt = self._await_swap(tx_hash, amount_in, our_min)
        if result.get("confirmed"):
            self._verify_fill(token_out, receipt, our_min, result["tx_hash"])
        return result

    def _zerox_request(self, kind, token_in, token_out, amount_in):
        if self.zerox is None:
            raise ZeroExError("ZEROX_API_KEY is not set")
        params = {
            "chainId": EXPECTED_CHAIN_ID,
            "sellToken": token_in,
            "buyToken": token_out,
            "sellAmount": str(amount_in),
            "taker": self.account.address,
            "recipient": self.account.address,
            "slippagePpm": SLIPPAGE_BPS * 100,
        }
        return getattr(self.zerox, kind)(params)

    def _check_zerox_response(self, response, token_in, token_out, amount_in, reference_price, firm):
        """Validate a /price (firm=False) or /quote (firm=True) body. Returns our minimum output."""
        try:
            issues = response["issues"]
            if response.get("liquidityAvailable") is not True:
                raise ZeroExQuoteRejected("no liquidity available")
            if issues.get("balance") is not None:
                raise ZeroExQuoteRejected(f"insufficient sell-token balance: {issues['balance']}")
            allowance_issue = issues.get("allowance")
            if allowance_issue is not None and not _same_address(allowance_issue.get("spender"), ALLOWANCE_HOLDER_ADDRESS):
                raise ZeroExQuoteRejected(f"allowance spender {allowance_issue.get('spender')} is not AllowanceHolder")
            if not _same_address(response.get("allowanceTarget"), ALLOWANCE_HOLDER_ADDRESS):
                raise ZeroExQuoteRejected(f"allowanceTarget {response.get('allowanceTarget')} is not AllowanceHolder")
            if not (_same_address(response.get("sellToken"), token_in) and _same_address(response.get("buyToken"), token_out)
                    and int(response["sellAmount"]) == amount_in):
                raise ZeroExQuoteRejected("response does not match the requested order")
            buy_amount, min_buy = int(response["buyAmount"]), int(response["minBuyAmount"])
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise ZeroExQuoteRejected(f"malformed 0x response: {e!r}") from None
        if buy_amount <= 0 or min_buy <= 0:
            raise ZeroExQuoteRejected(f"non-positive buyAmount {buy_amount} / minBuyAmount {min_buy}")
        refusal = self._reference_price_refusal(token_in, token_out, amount_in, buy_amount, reference_price, "0x")
        if refusal:
            raise ZeroExQuoteRejected(refusal)
        our_min = self._amount_out_minimum(buy_amount)
        if firm:
            if issues.get("simulationIncomplete") is not False:
                raise ZeroExQuoteRejected("0x could not simulate the swap")
            if issues.get("invalidSourcesPassed"):
                raise ZeroExQuoteRejected(f"invalid sources: {issues['invalidSourcesPassed']}")
            if our_min <= 0 or min_buy < our_min:
                raise ZeroExQuoteRejected(f"minBuyAmount {min_buy} below our floor {our_min}")
        return our_min

    def _check_zerox_transaction(self, quote, token_in, token_out, amount_in, our_min):
        """Validate quote.transaction, including the Settler call it forwards, and return it as a
        call for _send_contract_tx."""
        try:
            tx = quote["transaction"]
            to, value, data = tx["to"], int(tx["value"]), tx["data"]
            api_gas = int(tx["gas"]) if tx.get("gas") else None
            if not data.lower().startswith(ALLOWANCE_HOLDER_EXEC_SELECTOR):
                raise ZeroExQuoteRejected("transaction.data is not AllowanceHolder.exec")
            operator, token, amount, target, settler_call = abi_decode(
                ["address", "address", "uint256", "address", "bytes"], bytes.fromhex(data[10:]))
            if "0x" + settler_call[:4].hex() != SETTLER_EXECUTE_SELECTOR:
                raise ZeroExQuoteRejected("exec() data is not Settler.execute")
            (recipient, buy_token, min_amount_out), _, _ = abi_decode(
                ["(address,address,uint256)", "bytes[]", "bytes32"], settler_call[4:])
        except (KeyError, TypeError, ValueError, AttributeError, DecodingError) as e:
            raise ZeroExQuoteRejected(f"malformed 0x transaction: {e!r}") from None
        if not _same_address(recipient, self.account.address):
            raise ZeroExQuoteRejected(f"Settler slippage recipient {recipient} is not our wallet")
        if not _same_address(buy_token, token_out):
            raise ZeroExQuoteRejected(f"Settler slippage buyToken {buy_token} is not {token_out}")
        if min_amount_out < our_min:
            raise ZeroExQuoteRejected(f"Settler slippage minAmountOut {min_amount_out} below our floor {our_min}")
        if not _same_address(to, ALLOWANCE_HOLDER_ADDRESS):
            raise ZeroExQuoteRejected(f"transaction.to {to} is not AllowanceHolder")
        if value != 0:
            raise ZeroExQuoteRejected(f"transaction.value {value} must be 0 for an ERC-20 sell")
        if not _same_address(token, token_in) or amount > amount_in:
            raise ZeroExQuoteRejected(f"exec() token/amount {token}/{amount} does not match our {token_in}/{amount_in}")
        if not _same_address(operator, target):
            raise ZeroExQuoteRejected(f"exec() operator {operator} differs from target {target}")
        self._require_allowance_holder_code()
        self._require_genuine_settler(target)
        return _RawContractCall(self.trusted_w3, ALLOWANCE_HOLDER_ADDRESS, data, api_gas)

    def _require_allowance_holder_code(self):
        if self._allowance_holder_verified:
            return
        code = retry_rpc_call(lambda: self.trusted_w3.eth.get_code(Web3.to_checksum_address(ALLOWANCE_HOLDER_ADDRESS)))
        if not code:
            raise ZeroExQuoteRejected("AllowanceHolder has no code on the trusted RPC's chain")
        self._allowance_holder_verified = True

    def _require_genuine_settler(self, target):
        registry = self.trusted_w3.eth.contract(
            address=Web3.to_checksum_address(SETTLER_REGISTRY_ADDRESS), abi=SETTLER_REGISTRY_ABI)
        feature = SETTLER_TAKER_SUBMITTED_FEATURE
        try:
            current = retry_rpc_call(lambda: registry.functions.ownerOf(feature).call())
        except Exception as e:
            raise ZeroExQuoteRejected(f"Settler paused or registry unreadable (ownerOf({feature}): {e})") from None
        if _same_address(current, target):
            return
        try:
            previous = retry_rpc_call(lambda: registry.functions.prev(feature).call())
        except Exception as e:
            raise ZeroExQuoteRejected(f"exec() target {target} is not Settler {current}; prev({feature}) failed: {e}") from None
        if not _same_address(previous, target):
            raise ZeroExQuoteRejected(f"exec() target {target} is neither Settler {current} nor previous {previous}")
        logging.info(f"0x targets the previous Settler {target} (registry dwell period)")

    def _check_zerox_freshness(self, quote):
        age = self._latest_block()["number"] - int(quote["blockNumber"])
        if age > ZEROX_MAX_QUOTE_AGE_BLOCKS:
            raise ZeroExQuoteRejected(f"quote sampled {age} blocks ago (max {ZEROX_MAX_QUOTE_AGE_BLOCKS})")

    def _token_balance(self, token, block_identifier):
        contract = self.trusted_w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        return retry_rpc_call(
            lambda: contract.functions.balanceOf(self.account.address).call(block_identifier=block_identifier))

    def _verify_fill(self, token_out, receipt, our_min, tx_hex):
        """Engage the kill switch if a confirmed 0x swap delivered less than our floor. Never resubmits.
        An unreadable balance is logged but does not engage it, so a flaky RPC cannot freeze exits."""
        block = receipt["blockNumber"]
        try:
            before = self._token_balance(token_out, block - 1)
            received = self._token_balance(token_out, block) - before
        except Exception as e:
            logging.critical(f"Could not read balances around block {block} for {tx_hex}; fill unverified: {e}")
            return
        if received < our_min:
            logging.critical(
                f"0x swap {tx_hex} delivered {received} of {token_out}, below our floor {our_min}; "
                f"engaging on-chain kill switch ({ONCHAIN_KILL_SWITCH_FILE})"
            )
            engage_onchain_kill_switch(f"0x fill shortfall tx={tx_hex} received={received} floor={our_min}")

    def _await_swap(self, tx_hash, amount_in, amount_out_min):
        """Reconcile a broadcast swap. Returns (result, receipt)."""
        tx_hex = tx_hash.hex()
        status, receipt = self._await_receipt(tx_hash, RECEIPT_TIMEOUT_SECONDS)
        if status == "confirmed":
            logging.info(f"Swap successful. Tx: {tx_hex}")
            return {"success": True, "confirmed": True, "tx_hash": tx_hex}, receipt
        if status == "reverted":
            logging.error(
                f"Swap reverted. Tx: {tx_hex} | gasUsed={receipt.get('gasUsed', '?')} | "
                f"In={amount_in} OutMin={amount_out_min} | https://basescan.org/tx/{tx_hex}"
            )
            return {"success": False, "error": "transaction reverted", "tx_hash": tx_hex}, receipt
        if status == "dropped":
            logging.error(f"Swap tx {tx_hex} was dropped from the mempool")
            return {"success": False, "error": "transaction dropped", "tx_hash": tx_hex}, receipt
        logging.error(f"Swap tx {tx_hex} unresolved ({status}); NOT resubmitting. Check https://basescan.org/tx/{tx_hex}")
        return {"success": False, "pending": True, "error": f"transaction {status}; do not resubmit", "tx_hash": tx_hex}, receipt

    def place_market_order(self, product_id, side, amount_quote_currency=None, amount_base_currency=None, reference_price=None):
        """Convert a high-level buy/sell into an on-chain swap.

        For BUY:  swap USDC -> token. amount_quote_currency is the USDC amount to spend.
        For SELL: swap token -> USDC. amount_base_currency is the token amount to sell.
                  If only amount_quote_currency is given, we look up the current price
                  to calculate how many tokens that corresponds to.

        Amounts are passed in human-readable units (e.g. 10.5 USDC, 0.005 ETH) and
        converted to raw integer units here using each token's decimals. reference_price
        (USDC per token) defaults to the last price seen by place_limit_order this run; live
        orders without any reference price are refused rather than run unguarded.
        """
        addr = self.get_token_address(product_id)
        if not addr:
            logging.warning(f"EthereumExecutor: Asset {product_id} not found in Base registry. Skipping.")
            return None

        logging.info(f"EthereumExecutor: [BASE] Placing {side} for {product_id} ({addr}) on-chain.")

        if self.trading_mode == "live":
            usdc_addr = TOKENS.get("USDC")
            token_addr = addr

            try:
                usdc_decimals = self._get_decimals(usdc_addr)
                token_decimals = self._get_decimals(token_addr)
            except Exception as e:
                logging.error(f"EthereumExecutor: could not read decimals for {product_id}: {e}")
                return {"success": False, "error": "decimals unavailable"}

            if side == "BUY":
                if not amount_quote_currency:
                    logging.error("BUY requires amount_quote_currency")
                    return None
                amount_in = int(amount_quote_currency * (10 ** usdc_decimals))
                token_in = usdc_addr
                token_out = token_addr
            else:  # SELL
                if amount_base_currency is not None and float(amount_base_currency) <= 0:
                    logging.info(f"EthereumExecutor: Skipping SELL for {product_id} - dust amount ({amount_base_currency})")
                    return {"success": True, "tx_hash": "dust_skip"}

                if not amount_base_currency and amount_quote_currency:
                    details = self.get_product_details(product_id)
                    if details and "price" in details and float(details["price"]) > 0:
                        amount_base_currency = float(amount_quote_currency) / float(details["price"])
                        logging.info(f"EthereumExecutor: Calculated sell amount_base_currency={amount_base_currency} from quote={amount_quote_currency} @ price={details['price']}")

                if amount_base_currency is not None and float(amount_base_currency) <= 0:
                    logging.info(f"EthereumExecutor: Skipping SELL for {product_id} - calculated dust amount ({amount_base_currency})")
                    return {"success": True, "tx_hash": "dust_skip"}

                if not amount_base_currency:
                    logging.error("SELL requires amount_base_currency or amount_quote_currency (with price)")
                    return None
                amount_in = int(amount_base_currency * (10 ** token_decimals))
                token_in = token_addr
                token_out = usdc_addr

            if reference_price is None:
                reference_price = self._reference_prices.get(product_id)
            if reference_price is None:
                logging.error(f"EthereumExecutor: no reference price for {product_id}; refusing unguarded on-chain swap")
                return {"success": False, "error": "no reference price; refusing unguarded swap"}
            return self.execute_swap(token_in, token_out, amount_in, self.account.address, reference_price=reference_price)
        else:
            logging.info(f"[PAPER] On-chain swap {side} {product_id}")
            return {"success": True}


    def cancel_open_orders(self, product_id=None):
        pass  # On-chain swaps are atomic; no open orders to cancel

    def place_limit_order(self, product_id, side, price, amount_quote_currency=None, amount_base_currency=None):
        # Remembered so the bot's price-less market-order fallback still gets the deviation guard.
        self._reference_prices[product_id] = price
        return self.place_market_order(product_id, side, amount_quote_currency, amount_base_currency, reference_price=price)

    def get_supported_assets(self):
        """Return list of assets supported by this executor on Base."""
        assets = []
        for symbol in TOKENS.keys():
            if symbol in ("USDC", "USDC.e"):
                continue  # cash, not tradable asset
            if symbol == "WETH":
                assets.append("ETH")
            else:
                assets.append(symbol)
        return assets
