import logging
import time
import os
from web3 import Web3
from decimal import Decimal
import requests

# Strict Network Validation for Base
EXPECTED_CHAIN_ID = 8453  # Base Mainnet

# Minimal ABI for ERC-20 tokens. We only need balanceOf (check holdings),
# decimals (convert between human and raw units), approve (grant spending
# permission to the router), and allowance (check existing permission).
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "remaining", "type": "uint256"}], "type": "function"}
]

# Uniswap V3 Pool ABI — only need slot0 (current price as sqrtPriceX96 + tick)
# and liquidity (active liquidity at current tick). Used for price fallback
# when the Quoter contract is unavailable.
UNISWAP_V3_POOL_ABI = [
    {"constant": True, "inputs": [], "name": "slot0", "outputs": [{"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"}, {"name": "observationIndex", "type": "uint16"}, {"name": "observationCardinality", "type": "uint16"}, {"name": "observationCardinalityNext", "type": "uint16"}, {"name": "feeProtocol", "type": "uint8"}, {"name": "unlocked", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "liquidity", "outputs": [{"name": "", "type": "uint128"}], "type": "function"}
]

# SwapRouter02 ABI — this contract's exactInput uses a 4-field struct
# (path, recipient, amountIn, amountOutMinimum). The path is packed bytes:
# tokenIn_address(20) + pool_fee(3) + tokenOut_address(20) = 43 bytes for
# a single-hop swap. web3.py encodes the struct into ABI calldata automatically.
# Note: the standard SwapRouter02 uses exactInputSingle with 8 fields + selector
# 0x414bf389, but the Base deployment uses exactInput with 4 fields + 0xb858183f.
SWAP_ROUTER_ADDRESS = "0x2626664c2603336E57B271c5C0b26F421741e481"
SWAP_ROUTER_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "bytes", "name": "path", "type": "bytes"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountOutMinimum", "type": "uint256"}
                ],
                "internalType": "struct IV3SwapRouter.ExactInputParams",
                "name": "params",
                "type": "tuple"
            }
        ],
        "name": "exactInput",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function"
    }
]

# Uniswap V3 Quoter on Base
QUOTER_ADDRESS = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
QUOTER_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "tokenIn", "type": "address"},
            {"internalType": "address", "name": "tokenOut", "type": "address"},
            {"internalType": "uint24", "name": "fee", "type": "uint24"},
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"}
        ],
        "name": "quoteExactInputSingle",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# Base Token Registry
TOKENS = {
    "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",   # Native USDC
    "USDC.e": "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca", # Bridged USDC
    "WETH": "0x4200000000000000000000000000000000000006",
    "BTC": "0x0555E30da8f98308EdB960aa94C0Db47230d2B9c",    # WBTC
    "DEGEN": "0x4ed4E281562193f5C8c11259D3e21839951e7d23",
    "AERO": "0x9401811A062933285c64D72A25e8e3cf24f3fFBE",
    "LINK": "0x88fb150bdc53a65fe94dea0c9ba0a6daf8c6e196",    # Chainlink
}

# Tokens to check during balance scan (minimal set to avoid rate limits)
BALANCE_SCAN_TOKENS = ["USDC", "WETH"]

# Known Uniswap V3 Pools on Base
POOLS = {
    "ETH-USDC": "0x6c561B446416E1A00E8E93E221854d6eA4171372", # WETH/USDC (Native) 0.3%
    "BTC-USDC": "0x49e30c322E2474B3767de9FC4448C1e9ceD6552f", # WBTC/USDC 0.3%
}

# Pool fee tiers (matching POOLS).
# Uniswap V3 fee tiers are in hundredths of a basis point:
#   500 = 0.05%, 3000 = 0.30%, 10000 = 1.00%
# Each token pair has separate pools at different fee tiers with different
# liquidity. We use the 0.3% pools which have the deepest liquidity for
# major pairs on Base.
POOL_FEES = {
    "ETH-USDC": 3000,
    "BTC-USDC": 3000,
}

UNISWAP_V3_FACTORY_ADDRESS = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
UNISWAP_V3_FACTORY_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "tokenA", "type": "address"},
            {"internalType": "address", "name": "tokenB", "type": "address"},
            {"internalType": "uint24", "name": "fee", "type": "uint24"}
        ],
        "name": "getPool",
        "outputs": [{"internalType": "address", "name": "pool", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }
]

FEE_TIERS = [500, 3000, 10000]  # 0.05%, 0.30%, 1.00%

# Slippage tolerance in basis points (1 bps = 0.01%, so 50 bps = 0.5%).
# This is how much worse than the quoted price we're willing to accept.
# Too tight (e.g. 10 bps) → transactions revert on normal price movement.
# Too loose (e.g. 500 bps) → vulnerable to sandwich attacks / MEV extraction.
SLIPPAGE_BPS = 50  # 0.5%
# Minimum ETH balance required for gas (in ETH)
MIN_GAS_ETH = float(os.environ.get("MIN_GAS_ETH", "0.001"))
# Transaction deadline (seconds from now)
TX_DEADLINE_SECONDS = 120

# Known token decimals (avoids RPC calls)
KNOWN_DECIMALS = {
    "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913": 6,    # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": 6,    # USDC.e
    "0x4200000000000000000000000000000000000006": 18,   # WETH
    "0x0555E30da8f98308EdB960aa94C0Db47230d2B9c": 8,    # WBTC
    "0x4ed4E281562193f5C8c11259D3e21839951e7d23": 18,   # DEGEN
    "0x9401811A062933285c64D72A25e8e3cf24f3fFBE": 18,   # AERO
    "0x88fb150bdc53a65fe94dea0c9ba0a6daf8c6e196": 18,   # LINK
}

# Fallback RPC endpoints for Base
FALLBACK_RPCS = [
    "https://mainnet.base.org",
    "https://base.blockpi.network/v1/rpc/public",
    "https://1rpc.io/base",
    "https://base.meowrpc.com",
]


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
                logging.warning(f"RPC call failed (attempt {attempt + 1}/{max_retries}), retrying in {delay:.1f}s: {error_str[:100]}")
                time.sleep(delay)
            else:
                raise
    raise last_exception


class EthereumExecutor:
    """Handles interaction with Base blockchain via Web3 with dynamic balance detection."""
    def __init__(self, rpc_url, private_key, trading_mode="paper"):
        self.primary_rpc_url = rpc_url
        self.private_key = private_key
        self.trading_mode = trading_mode

        # RPC failover tracking
        self._current_rpc_index = 0
        self._rpc_endpoints = [rpc_url] + [r for r in FALLBACK_RPCS if r != rpc_url]

        # Caches to minimize RPC calls
        self._decimals_cache = {}
        self._allowance_cache = {}
        self._gas_price_cache = None
        self._gas_price_cache_time = 0
        self._next_nonce = None  # Local nonce tracker to avoid RPC race conditions
        self._route_cache = {}

        # Initialize Web3 with primary RPC
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={'timeout': 30}))

        # Skip chain_id validation in live mode to save an RPC call
        # We trust the RPC URL is correct for Base
        logging.info(f"Connected to RPC: {rpc_url} (chain validation skipped to reduce RPC calls)")

        self.account = self.w3.eth.account.from_key(private_key) if private_key else None
        if self.account:
            logging.info(f"Using Base Wallet: {self.account.address}")

        # Contracts
        self.router_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(SWAP_ROUTER_ADDRESS),
            abi=SWAP_ROUTER_ABI
        )
        self.quoter_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(QUOTER_ADDRESS),
            abi=QUOTER_ABI
        )
        self.factory_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(UNISWAP_V3_FACTORY_ADDRESS),
            abi=UNISWAP_V3_FACTORY_ABI
        )

    def _rotate_rpc(self):
        """Rotate to the next available RPC endpoint."""
        old_index = self._current_rpc_index
        self._current_rpc_index = (self._current_rpc_index + 1) % len(self._rpc_endpoints)
        new_rpc = self._rpc_endpoints[self._current_rpc_index]
        logging.warning(f"Rotating RPC from index {old_index} to {self._current_rpc_index}: {new_rpc}")
        self.w3 = Web3(Web3.HTTPProvider(new_rpc, request_kwargs={'timeout': 30}))
        # Recreate contracts with new provider
        self.router_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(SWAP_ROUTER_ADDRESS),
            abi=SWAP_ROUTER_ABI
        )
        self.quoter_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(QUOTER_ADDRESS),
            abi=QUOTER_ABI
        )
        self.factory_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(UNISWAP_V3_FACTORY_ADDRESS),
            abi=UNISWAP_V3_FACTORY_ABI
        )

    def _call_with_failover(self, func):
        """Execute a read-only RPC call with failover across RPC endpoints.

        Only use this for read calls (get_balances, get_product_details, get_quote),
        NOT for write calls (execute_swap, _approve_token) to avoid nonce issues.
        """
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
                    logging.warning(f"RPC connection error, attempting failover: {str(e)[:100]}")
                    self._rotate_rpc()
                else:
                    raise
        raise last_exception

    def _get_decimals(self, token_address):
        """Get token decimals with caching - uses known values to avoid RPC calls."""
        addr_lower = token_address.lower()
        # Check memory cache first
        if addr_lower in self._decimals_cache:
            return self._decimals_cache[addr_lower]
        # Check known decimals (no RPC needed)
        if token_address in KNOWN_DECIMALS:
            dec = KNOWN_DECIMALS[token_address]
            self._decimals_cache[addr_lower] = dec
            return dec
        # Fallback to RPC call with retry
        try:
            def _fetch_decimals():
                contract = self.w3.eth.contract(
                    address=Web3.to_checksum_address(token_address),
                    abi=ERC20_ABI
                )
                return contract.functions.decimals().call()
            dec = retry_rpc_call(_fetch_decimals)
            self._decimals_cache[addr_lower] = dec
            return dec
        except Exception as e:
            logging.error(f"Failed to get decimals for {token_address}: {e}")
            return 18  # Default to 18

    def _get_gas_price(self):
        """Get gas price with caching (refresh every 30 seconds)."""
        now = time.time()
        if self._gas_price_cache and (now - self._gas_price_cache_time) < 30:
            return self._gas_price_cache
        try:
            def _fetch_gas():
                return self.w3.eth.gas_price
            price = retry_rpc_call(_fetch_gas)
            self._gas_price_cache = price
            self._gas_price_cache_time = now
            return price
        except Exception as e:
            logging.warning(f"Failed to get gas price: {e}")
            return self._gas_price_cache or 1000000000  # 1 gwei fallback

    def _get_nonce(self):
        """Get nonce for the account. Uses local tracker if set, otherwise fetches from RPC."""
        if self._next_nonce is not None:
            nonce = self._next_nonce
            self._next_nonce = None  # Consume it — next call fetches from RPC
            return nonce
        def _fetch_nonce():
            return self.w3.eth.get_transaction_count(self.account.address)
        return retry_rpc_call(_fetch_nonce)

    def _invalidate_gas_cache(self):
        """Force fresh gas price on next call."""
        self._gas_price_cache = None
        self._gas_price_cache_time = 0

    def _check_balance(self, balances, symbol, address):
        """Helper to fetch and add ERC20 balance if non-zero. Uses cached decimals."""
        try:
            addr = Web3.to_checksum_address(address)

            # Check if contract exists (skip if we know it does to save RPC)
            # For known tokens, we know they exist on Base
            def _fetch_balance():
                contract = self.w3.eth.contract(address=addr, abi=ERC20_ABI)
                return contract.functions.balanceOf(self.account.address).call()

            raw_bal = retry_rpc_call(_fetch_balance)
            if raw_bal > 0:
                decimals = self._get_decimals(address)  # Uses cache, no RPC
                val = float(raw_bal) / (10 ** decimals)

                if symbol == "USDC" or symbol == "USDC.e":
                    balances["cash"]["USDC"] = balances["cash"].get("USDC", 0.0) + val
                elif symbol == "WETH":
                    balances["crypto"]["ETH"] = balances["crypto"].get("ETH", 0.0) + val
                else:
                    balances["crypto"][symbol] = val
        except Exception as e:
            logging.debug(f"Could not check balance for {symbol} ({address}): {e}")

    def get_balances(self):
        """Fetch ETH and key ERC20 balances on Base. Minimizes RPC calls.
        Uses RPC failover for connection errors."""
        def _do_get_balances():
            if not self.account:
                return {"cash": {"USDC": 0.0}, "crypto": {}}

            balances = {"cash": {"USDC": 0.0}, "crypto": {}}

            # 1. Native ETH (Gas) - 1 RPC call
            try:
                def _fetch_eth_balance():
                    return self.w3.eth.get_balance(self.account.address)
                eth_bal = retry_rpc_call(_fetch_eth_balance)
                balances["crypto"]["ETH_NATIVE"] = float(self.w3.from_wei(eth_bal, 'ether'))
            except Exception as e:
                logging.debug(f"Could not fetch native ETH balance: {e}")

            # 2. Only check essential tokens (USDC, WETH) to minimize RPC calls
            # Other tokens can be checked on-demand if needed
            for symbol in BALANCE_SCAN_TOKENS:
                if symbol in TOKENS:
                    self._check_balance(balances, symbol, TOKENS[symbol])

            return balances

        return self._call_with_failover(_do_get_balances)

    def get_market_data(self, product_id, window):
        return None

    def get_product_details(self, product_id):
        """Fetch current price from Uniswap V3 Pool on Base with retry logic.
        Uses RPC failover for connection errors."""
        if product_id not in POOLS:
            return None

        def _do_get_product_details():
            pool_address = Web3.to_checksum_address(POOLS[product_id])

            def _fetch_price():
                pool_contract = self.w3.eth.contract(address=pool_address, abi=UNISWAP_V3_POOL_ABI)
                slot0 = pool_contract.functions.slot0().call()
                sqrtPriceX96 = slot0[0]

                # Uniswap V3 stores price as sqrt(token1/token0) * 2^96.
                # To recover the actual price ratio (token1 per token0):
                #   price = (sqrtPriceX96 / 2^96)^2
                # This gives the price in raw smallest-unit terms (wei/wei).
                price = (Decimal(sqrtPriceX96) / Decimal(2**96))**2

                # Adjust for decimal differences between token0 and token1.
                # The raw price is in (token1_smallest_units / token0_smallest_units).
                # To get human-readable price (e.g. USDC per ETH), multiply by
                # 10^(token0_decimals - token1_decimals):
                #   ETH-USDC: 10^(18-6) = 10^12
                #   BTC-USDC: 10^(8-6)  = 10^2
                if product_id == "ETH-USDC":
                    adjusted_price = float(price * Decimal(10**12))
                elif product_id == "BTC-USDC":
                    adjusted_price = float(price * Decimal(10**2))
                else:
                    adjusted_price = float(price)

                return {"price": str(adjusted_price), "quote_increment": "0.01", "base_increment": "0.00000001"}

            try:
                return retry_rpc_call(_fetch_price, max_retries=5, base_delay=2.0)
            except Exception as e:
                logging.error(f"Error fetching price for {product_id} from {pool_address}: {e}")
                return None

        return self._call_with_failover(_do_get_product_details)

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

    def _approve_token(self, token_address, amount):
        """Approve router to spend tokens. Skips if already approved (cached).
        
        Uses max uint256 for approval to avoid repeated approvals across different amounts.
        Cache persists across runs to minimize RPC calls.
        """
        if self.trading_mode != "live" or not self.account:
            return True

        addr_lower = token_address.lower()
        cache_key = f"{addr_lower}:{self.account.address}"

        # Check cached allowance first (max uint256 means fully approved)
        if cache_key in self._allowance_cache:
            cached_allowance = self._allowance_cache[cache_key]
            if cached_allowance == 2**256 - 1:
                logging.info(f"Using cached MAX approval for {token_address}")
                return True
            # If cached but not max, check if it's sufficient for this amount
            if cached_allowance >= amount:
                logging.info(f"Using cached allowance for {token_address}: {cached_allowance}")
                return True

        try:
            token_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(token_address),
                abi=ERC20_ABI
            )

            # Check current allowance (with retry)
            def _check_allowance():
                return token_contract.functions.allowance(
                    self.account.address,
                    Web3.to_checksum_address(SWAP_ROUTER_ADDRESS)
                ).call()

            allowance = retry_rpc_call(_check_allowance)
            self._allowance_cache[cache_key] = allowance
            logging.info(f"Current allowance for {token_address} on router: {allowance}")

            # If already max approved, cache and return
            if allowance == 2**256 - 1:
                self._allowance_cache[cache_key] = 2**256 - 1
                return True

            # Check if current allowance is sufficient
            if allowance >= amount:
                # Cache this allowance for future checks
                self._allowance_cache[cache_key] = allowance
                return True

            # Capture nonce once to avoid race conditions
            base_nonce = self._get_nonce()

            # Approve max uint256 to avoid future approvals for this token
            # Some tokens (like USDT) require resetting to 0 first if allowance is non-zero
            if allowance > 0:
                logging.info(f"Resetting allowance for {token_address} from {allowance} to 0 before MAX approval")
                tx0 = token_contract.functions.approve(
                    Web3.to_checksum_address(SWAP_ROUTER_ADDRESS),
                    0
                ).build_transaction({
                    'from': self.account.address,
                    'nonce': base_nonce,
                    'gas': 100000,
                    'gasPrice': self._get_gas_price(),
                    'chainId': EXPECTED_CHAIN_ID,
                })
                signed_tx0 = self.w3.eth.account.sign_transaction(tx0, self.private_key)
                
                def _send_reset():
                    try:
                        return self.w3.eth.send_raw_transaction(signed_tx0.raw_transaction)
                    except Exception as e:
                        if "in-flight" in str(e).lower():
                            logging.warning("In-flight transaction limit reached during reset, waiting 10s...")
                            time.sleep(10)
                            return self.w3.eth.send_raw_transaction(signed_tx0.raw_transaction)
                        raise

                tx_hash0 = retry_rpc_call(_send_reset)
                retry_rpc_call(lambda: self.w3.eth.wait_for_transaction_receipt(tx_hash0, timeout=120))
                base_nonce += 1

            # Then approve max with sequential nonce
            logging.info(f"Approving MAX for {token_address} (nonce={base_nonce})")
            tx_max = token_contract.functions.approve(
                Web3.to_checksum_address(SWAP_ROUTER_ADDRESS),
                2**256 - 1
            ).build_transaction({
                'from': self.account.address,
                'nonce': base_nonce,
                'gas': 100000,
                'gasPrice': self._get_gas_price(),
                'chainId': EXPECTED_CHAIN_ID,
            })
            signed_tx_max = self.w3.eth.account.sign_transaction(tx_max, self.private_key)
            
            def _send_max():
                try:
                    return self.w3.eth.send_raw_transaction(signed_tx_max.raw_transaction)
                except Exception as e:
                    if "in-flight" in str(e).lower():
                        logging.warning("In-flight transaction limit reached during MAX approval, waiting 10s...")
                        time.sleep(10)
                        return self.w3.eth.send_raw_transaction(signed_tx_max.raw_transaction)
                    raise

            tx_hash_max = retry_rpc_call(_send_max)
            receipt_max = retry_rpc_call(lambda: self.w3.eth.wait_for_transaction_receipt(tx_hash_max, timeout=120))

            if receipt_max.status == 1:
                logging.info(f"Approved MAX for {token_address}. Tx: {tx_hash_max.hex()}")
                self._allowance_cache[cache_key] = 2**256 - 1
                # Set local nonce so execute_swap doesn't need to fetch from RPC
                self._next_nonce = base_nonce + 1
                return tx_hash_max.hex()
            else:
                logging.error(f"Approve MAX transaction failed: {receipt_max}")
                return None
        except Exception as e:
            logging.error(f"Approve failed: {e}")
            return None

    def get_quote(self, token_in, token_out, amount_in, fee):
        """Get expected output amount using Quoter with retry.
        Uses RPC failover for connection errors."""
        def _do_get_quote():
            try:
                def _fetch_quote():
                    return self.quoter_contract.functions.quoteExactInputSingle(
                        Web3.to_checksum_address(token_in),
                        Web3.to_checksum_address(token_out),
                        fee,
                        amount_in,
                        0  # sqrtPriceLimitX96
                    ).call()
                return retry_rpc_call(_fetch_quote)
            except Exception as e:
                logging.warning(f"Quote failed: {e}")
                return None

        return self._call_with_failover(_do_get_quote)

    def _estimate_from_pool(self, pool_address, token_in, token_out, amount_in):
        """Estimate output by reading sqrtPriceX96 from a pool's slot0.

        Returns the expected raw output amount, or None if the pool can't be read.
        The price math uses sqrtPriceX96² = token1_raw/token0_raw, so:
          token0→token1: out = in * price_ratio
          token1→token0: out = in / price_ratio
        """
        try:
            pool_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(pool_address),
                abi=UNISWAP_V3_POOL_ABI
            )
            def _fetch():
                return pool_contract.functions.slot0().call()
            slot0 = retry_rpc_call(_fetch)
            sqrtPriceX96 = slot0[0]
            price_ratio = (Decimal(sqrtPriceX96) / Decimal(2**96)) ** 2
            if token_in.lower() < token_out.lower():
                return int(Decimal(amount_in) * price_ratio)
            else:
                return int(Decimal(amount_in) / price_ratio)
        except Exception as e:
            logging.warning(f"Failed to estimate from pool {pool_address}: {e}")
            return None

    def _get_amount_out_minimum(self, token_in, token_out, amount_in, fee, slippage_bps=SLIPPAGE_BPS, route=None):
        """Calculate the minimum acceptable output for a swap (slippage protection).

        This value becomes the amountOutMinimum param in Uniswap's exactInputSingle.
        If the pool can't deliver at least this much, the transaction reverts on-chain,
        protecting against sandwich attacks and excessive price impact.

        Strategy (in priority order):
          1. Ask the Uniswap Quoter contract for the expected output (most accurate).
          2. Fall back to reading the pool's sqrtPriceX96 from slot0 and computing
             the price manually, adjusting for decimal differences between tokens.
          3. Last resort: estimate from amount_in scaled by the decimal difference
             between tokens, minus a conservative 2% buffer.

        After getting the expected output, subtract slippage_bps (default 50 = 0.5%)
        to allow for normal price movement between quote and execution.
        """
        # Multi-hop: chain estimates through each hop, apply slippage only at the end
        if route and route["type"] == "multi":
            hops = route["hops"]
            current_amount = amount_in
            for i, hop in enumerate(hops):
                hop_slippage = slippage_bps if i == len(hops) - 1 else 0
                current_amount = self._get_amount_out_minimum(
                    hop["token_in"], hop["token_out"], current_amount,
                    hop["fee"], slippage_bps=hop_slippage
                )
            return current_amount
        try:
            # --- Primary: use Quoter contract (simulates the swap off-chain) ---
            amount_out = self.get_quote(token_in, token_out, amount_in, fee)

            if amount_out is not None:
                logging.info(f"Quoter returned: {amount_out} for {amount_in} of {token_in} -> {token_out}")
            else:
                # --- Fallback: read pool price from slot0 ---
                logging.warning("Quote failed, falling back to pool price calculation")
                pool_found = False

                # First try: use route info if available (covers dynamically discovered pools)
                if route:
                    for hop in route["hops"]:
                        hop_match = (
                            hop["token_in"].lower() == token_in.lower() and
                            hop["token_out"].lower() == token_out.lower()
                        )
                        if hop_match and "pool" in hop:
                            amount_out = self._estimate_from_pool(hop["pool"], token_in, token_out, amount_in)
                            if amount_out is not None:
                                pool_found = True
                                break

                # Second try: check hardcoded POOLS dict (backward compat)
                if not pool_found:
                    addr_to_symbol = {}
                    for sym, addr in TOKENS.items():
                        addr_to_symbol[addr.lower()] = sym
                    sym_in = addr_to_symbol.get(token_in.lower(), "")
                    sym_out = addr_to_symbol.get(token_out.lower(), "")
                    if sym_in == "WETH":
                        sym_in = "ETH"
                    if sym_out == "WETH":
                        sym_out = "ETH"

                    for pool_id, pool_addr in POOLS.items():
                        parts = set(pool_id.split("-"))
                        if parts == {sym_in, sym_out}:
                            result = self._estimate_from_pool(pool_addr, token_in, token_out, amount_in)
                            if result is not None:
                                amount_out = result
                                pool_found = True
                            break

                if not pool_found:
                    logging.warning("No pool found for fallback price, using decimal-adjusted estimate")
                    decimals_in = self._get_decimals(token_in)
                    decimals_out = self._get_decimals(token_out)
                    decimal_scale = Decimal(10 ** (decimals_out - decimals_in))
                    amount_out = int(Decimal(amount_in) * decimal_scale * Decimal("0.98"))

            # Apply slippage tolerance: reduce expected output by slippage_bps basis points.
            # 1 basis point (bps) = 0.01%, so 50 bps = 0.5%.
            # Formula: min_out = expected * (1 - bps/10000) = expected * 0.995.
            # If the pool's actual output drops below this between quote and execution
            # (due to other trades or MEV), the tx reverts instead of giving a bad price.
            min_out = int(Decimal(amount_out) * (Decimal(1) - Decimal(slippage_bps) / Decimal(10000)))
            logging.info(f"amountOutMinimum: {min_out} (expected: {amount_out}, slippage: {slippage_bps}bps)")
            return min_out
        except Exception as e:
            logging.warning(f"Could not calculate amountOutMinimum: {e}")
            # Emergency fallback: scale by decimal difference with 2% buffer
            decimals_in = self._get_decimals(token_in)
            decimals_out = self._get_decimals(token_out)
            decimal_scale = Decimal(10 ** (decimals_out - decimals_in))
            return int(Decimal(amount_in) * decimal_scale * Decimal("0.98"))

    def _get_fee_for_tokens(self, token_in, token_out):
        """Look up the pool fee tier for a token pair from POOL_FEES."""
        # Build reverse lookup: token address -> symbol
        addr_to_symbol = {}
        for sym, addr in TOKENS.items():
            addr_to_symbol[addr.lower()] = sym
        # Map WETH -> ETH for pool ID matching
        sym_in = addr_to_symbol.get(token_in.lower(), "")
        sym_out = addr_to_symbol.get(token_out.lower(), "")
        if sym_in == "WETH":
            sym_in = "ETH"
        if sym_out == "WETH":
            sym_out = "ETH"
        # Try both orderings (e.g., ETH-USDC or USDC-ETH)
        for pool_id, fee in POOL_FEES.items():
            parts = pool_id.split("-")
            if set(parts) == {sym_in, sym_out}:
                return fee
        # Default to 3000 (0.3%) if no pool found
        logging.warning(f"No pool fee found for {token_in}/{token_out}, defaulting to 3000")
        return 3000

    def _find_pool(self, token_a, token_b, fee):
        """Look up if a Uniswap V3 pool exists for token pair at given fee tier.
        Returns pool address if it exists and has liquidity, else None."""
        try:
            def _fetch():
                return self.factory_contract.functions.getPool(
                    Web3.to_checksum_address(token_a),
                    Web3.to_checksum_address(token_b),
                    fee
                ).call()
            pool_addr = retry_rpc_call(_fetch)
            if pool_addr == "0x0000000000000000000000000000000000000000":
                return None
            pool_contract = self.w3.eth.contract(address=pool_addr, abi=UNISWAP_V3_POOL_ABI)
            def _fetch_liq():
                return pool_contract.functions.liquidity().call()
            liquidity = retry_rpc_call(_fetch_liq)
            return pool_addr if liquidity > 0 else None
        except Exception:
            return None

    def _find_route(self, token_in, token_out):
        """Find the best swap route between two tokens.
        Prefers single-hop, falls back to multi-hop via WETH.
        Caches results per token pair."""
        cache_key = (token_in.lower(), token_out.lower())
        if cache_key in self._route_cache:
            return self._route_cache[cache_key]

        weth = TOKENS["WETH"]

        # Don't try multi-hop if one side is already WETH
        if token_in.lower() == weth.lower() or token_out.lower() == weth.lower():
            for fee in FEE_TIERS:
                pool = self._find_pool(token_in, token_out, fee)
                if pool:
                    route = {"type": "single", "pool": pool, "hops": [{"token_in": token_in, "token_out": token_out, "fee": fee}]}
                    self._route_cache[cache_key] = route
                    return route
            self._route_cache[cache_key] = None
            return None

        # Try single-hop first
        for fee in FEE_TIERS:
            pool = self._find_pool(token_in, token_out, fee)
            if pool:
                logging.info(f"Found direct pool for {token_in[:10]}→{token_out[:10]} at fee {fee}")
                route = {"type": "single", "pool": pool, "hops": [{"token_in": token_in, "token_out": token_out, "fee": fee}]}
                self._route_cache[cache_key] = route
                return route

        # Fallback: multi-hop via WETH
        logging.info(f"No direct pool found, trying multi-hop via WETH for {token_in[:10]}→{token_out[:10]}")
        for fee1 in FEE_TIERS:
            hop1_pool = self._find_pool(token_in, weth, fee1)
            if hop1_pool:
                for fee2 in FEE_TIERS:
                    hop2_pool = self._find_pool(weth, token_out, fee2)
                    if hop2_pool:
                        logging.info(f"Found multi-hop route: {token_in[:10]}→WETH (fee {fee1})→{token_out[:10]} (fee {fee2})")
                        route = {
                            "type": "multi",
                            "hops": [
                                {"token_in": token_in, "token_out": weth, "fee": fee1, "pool": hop1_pool},
                                {"token_in": weth, "token_out": token_out, "fee": fee2, "pool": hop2_pool},
                            ]
                        }
                        self._route_cache[cache_key] = route
                        return route

        logging.error(f"No route found for {token_in[:10]}→{token_out[:10]}")
        self._route_cache[cache_key] = None
        return None

    def execute_swap(self, token_in, token_out, amount_in, recipient, fee=None):
        """Execute a token swap on-chain via Uniswap V3 SwapRouter02 exactInput.

        Uses exactInput((bytes,address,uint256,uint256)) with a packed
        path encoding for single-hop or multi-hop swaps through Uniswap V3 pools.

        Flow:
          1. Pre-flight: verify wallet has enough ETH to pay gas fees.
          2. Approve: grant the SwapRouter permission to spend token_in.
          3. Route: find single-hop or multi-hop via WETH.
          4. Quote: _get_amount_out_minimum for slippage protection.
          5. Build the exactInput call with packed path.
          6. Submit with retry logic for transient nonce/gas-price issues.
          7. Wait for receipt and check success.
        """
        if self.trading_mode != "live":
            logging.info(f"[PAPER] Would swap {amount_in} of {token_in} for {token_out}")
            return {"success": True, "tx_hash": "paper"}

        try:
            # Pre-flight: check minimum gas balance
            try:
                eth_bal = self.w3.eth.get_balance(self.account.address)
                eth_balance = float(self.w3.from_wei(eth_bal, 'ether'))
                if eth_balance < MIN_GAS_ETH:
                    logging.error(f"Insufficient ETH for gas: {eth_balance:.6f} ETH < {MIN_GAS_ETH} ETH minimum")
                    return {"success": False, "error": "insufficient gas"}
            except Exception as e:
                logging.warning(f"Could not check gas balance: {e}, proceeding anyway")

            # Find the best route (single-hop or multi-hop via WETH)
            route = self._find_route(token_in, token_out)
            if route is None:
                logging.error(f"No swap route found for {token_in}→{token_out}")
                return {"success": False, "error": "no route found"}

            # ERC-20 approve: the router needs permission to pull our tokens
            approval_result = self._approve_token(token_in, amount_in)
            if approval_result is None:
                logging.error(f"Token approval failed for {token_in}, cannot execute swap")
                return {"success": False, "error": "approval failed"}

            # Calculate the minimum output we'll accept (slippage protection)
            first_hop_fee = route["hops"][0]["fee"]
            amount_out_min = self._get_amount_out_minimum(
                token_in, token_out, amount_in, first_hop_fee, route=route
            )

            # Sanity check: amountOutMinimum must be > 0 or the swap is meaningless
            if amount_out_min <= 0:
                logging.error(f"amountOutMinimum is {amount_out_min}, using 0 (no slippage protection)")
                amount_out_min = 0

            dec_in = self._get_decimals(token_in)
            dec_out = self._get_decimals(token_out)
            human_in = amount_in / (10 ** dec_in)
            human_out_min = amount_out_min / (10 ** dec_out) if amount_out_min > 0 else 0

            # Build the V3 path based on route type
            if route["type"] == "single":
                hop = route["hops"][0]
                path = (
                    bytes.fromhex(hop["token_in"][2:])
                    + int(hop["fee"]).to_bytes(3, 'big')
                    + bytes.fromhex(hop["token_out"][2:])
                )
                logging.info(f"Swap (single-hop): {human_in:.6f} -> min {human_out_min:.8f} (raw: {amount_in} -> {amount_out_min}), fee={hop['fee']}")
            else:
                hops = route["hops"]
                path = (
                    bytes.fromhex(hops[0]["token_in"][2:])
                    + int(hops[0]["fee"]).to_bytes(3, 'big')
                    + bytes.fromhex(hops[0]["token_out"][2:])
                    + int(hops[1]["fee"]).to_bytes(3, 'big')
                    + bytes.fromhex(hops[1]["token_out"][2:])
                )
                logging.info(f"Swap (multi-hop): {human_in:.6f} -> min {human_out_min:.8f} (raw: {amount_in} -> {amount_out_min})")

            # exactInput params — web3.py ABI-encodes this 4-field struct into calldata
            # with the function selector 0xb858183f automatically.
            #   path:            packed route through Uniswap V3 pools (see above)
            #   recipient:       address that receives the output tokens
            #   amountIn:        input amount in token's smallest unit (e.g. 500000 = 0.5 USDC)
            #   amountOutMinimum: revert if the pool can't deliver at least this much
            #                     (set by _get_amount_out_minimum with slippage applied)
            params = (
                path,
                Web3.to_checksum_address(recipient),
                amount_in,
                amount_out_min,
            )

            # Retry loop handles transient errors: stale nonce or gas price too low
            tx_hash = None
            for attempt in range(4):
                try:
                    current_nonce = self._get_nonce()
                    tx = self.router_contract.functions.exactInput(params).build_transaction({
                        'from': self.account.address,
                        'nonce': current_nonce,
                        'gas': 500000,
                        'gasPrice': self._get_gas_price(),
                        'chainId': EXPECTED_CHAIN_ID,
                        'value': 0,
                    })

                    signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)

                    def _send_swap():
                        try:
                            return self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                        except Exception as e:
                            error_msg = str(e).lower()
                            if "in-flight" in error_msg:
                                logging.warning("In-flight transaction limit reached during swap, waiting 10s...")
                                time.sleep(10)
                                return self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                            raise

                    tx_hash = retry_rpc_call(_send_swap)
                    break
                except Exception as e:
                    error_msg = str(e).lower()
                    if "underpriced" in error_msg or "nonce" in error_msg:
                        logging.warning(f"Nonce/underpriced error (attempt {attempt+1}), retrying: {e}")
                        if "underpriced" in error_msg:
                            self._invalidate_gas_cache()
                        time.sleep(2)
                        continue
                    raise

            if not tx_hash:
                return {"success": False, "error": "failed to send transaction after retries"}

            def _wait_swap_receipt():
                return self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)

            receipt = retry_rpc_call(_wait_swap_receipt)

            if receipt.status == 1:
                logging.info(f"Swap successful. Tx: {tx_hash.hex()}")
                return {"success": True, "confirmed": True, "tx_hash": tx_hash.hex()}
            else:
                tx_hex = tx_hash.hex()
                gas_used = receipt.get('gasUsed', '?')
                logging.error(
                    f"Swap reverted. Tx: {tx_hex} | "
                    f"Gas used: {gas_used}/500000 | "
                    f"In={amount_in} OutMin={amount_out_min} Route={route['type']} | "
                    f"https://basescan.org/tx/{tx_hex}"
                )
                return {"success": False, "error": "transaction failed", "tx_hash": tx_hex}
        except Exception as e:
            logging.error(f"Swap execution failed: {e}")
            return {"success": False, "error": str(e)}

    def place_market_order(self, product_id, side, amount_quote_currency=None, amount_base_currency=None):
        """Convert a high-level buy/sell into an on-chain swap.

        For BUY:  swap USDC -> token. amount_quote_currency is the USDC amount to spend.
        For SELL: swap token -> USDC. amount_base_currency is the token amount to sell.
                  If only amount_quote_currency is given, we look up the current price
                  to calculate how many tokens that corresponds to.

        Amounts are passed in human-readable units (e.g. 10.5 USDC, 0.005 ETH) and
        converted to raw integer units here using each token's decimals.
        """
        addr = self.get_token_address(product_id)
        if not addr:
            logging.warning(f"EthereumExecutor: Asset {product_id} not found in Base registry. Skipping.")
            return None

        logging.info(f"EthereumExecutor: [BASE] Placing {side} for {product_id} ({addr}) on-chain.")

        if self.trading_mode == "live":
            usdc_addr = TOKENS.get("USDC")
            token_addr = addr

            usdc_decimals = self._get_decimals(usdc_addr)    # 6 for USDC
            token_decimals = self._get_decimals(token_addr)   # 18 for WETH, 8 for WBTC

            # Determine amount in wei
            if side == "BUY":
                if not amount_quote_currency:
                    logging.error("BUY requires amount_quote_currency")
                    return None
                amount_in = int(amount_quote_currency * (10 ** usdc_decimals))
                token_in = usdc_addr
                token_out = token_addr
            else:  # SELL
                if not amount_base_currency and amount_quote_currency:
                    # Calculate amount_base_currency from amount_quote_currency using current price
                    details = self.get_product_details(product_id)
                    if details and 'price' in details:
                        amount_base_currency = float(amount_quote_currency) / float(details['price'])
                        logging.info(f"EthereumExecutor: Calculated sell amount_base_currency={amount_base_currency} from quote={amount_quote_currency} @ price={details['price']}")
                
                if not amount_base_currency:
                    logging.error("SELL requires amount_base_currency or amount_quote_currency (with price)")
                    return None
                amount_in = int(amount_base_currency * (10 ** token_decimals))
                token_in = token_addr
                token_out = usdc_addr

            # Execute the on-chain swap (route is determined dynamically by execute_swap)
            result = self.execute_swap(token_in, token_out, amount_in, self.account.address)
            return result
        else:
            logging.info(f"[PAPER] On-chain swap {side} {product_id}")
            return {"success": True}


    def place_limit_order(self, product_id, side, price, amount_quote_currency=None, amount_base_currency=None):
        return self.place_market_order(product_id, side, amount_quote_currency, amount_base_currency)
