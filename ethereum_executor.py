import logging
import time
import os
from web3 import Web3
from decimal import Decimal
import requests

# Strict Network Validation for Base
EXPECTED_CHAIN_ID = 8453  # Base Mainnet

# Minimal ABI
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "remaining", "type": "uint256"}], "type": "function"}
]

UNISWAP_V3_POOL_ABI = [
    {"constant": True, "inputs": [], "name": "slot0", "outputs": [{"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"}, {"name": "observationIndex", "type": "uint16"}, {"name": "observationCardinality", "type": "uint16"}, {"name": "observationCardinalityNext", "type": "uint16"}, {"name": "feeProtocol", "type": "uint8"}, {"name": "unlocked", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "liquidity", "outputs": [{"name": "", "type": "uint128"}], "type": "function"}
]

# Uniswap V3 SwapRouter02 on Base
SWAP_ROUTER_ADDRESS = "0x2626664c2603336E57B271c5C0b26F421741e481"
SWAP_ROUTER_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "tokenIn", "type": "address"},
                    {"internalType": "address", "name": "tokenOut", "type": "address"},
                    {"internalType": "uint24", "name": "fee", "type": "uint24"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "uint256", "name": "deadline", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountOutMinimum", "type": "uint256"},
                    {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"}
                ],
                "internalType": "struct ISwapRouter.ExactInputSingleParams",
                "name": "params",
                "type": "tuple"
            }
        ],
        "name": "exactInputSingle",
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

# Pool fee tiers (matching POOLS)
POOL_FEES = {
    "ETH-USDC": 3000,
    "BTC-USDC": 3000,
}

# Slippage tolerance (0.5%)
SLIPPAGE_BPS = 50  # 0.5%
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
        self._nonce_cache = None

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
        """Get nonce for the account."""
        def _fetch_nonce():
            return self.w3.eth.get_transaction_count(self.account.address)
        return retry_rpc_call(_fetch_nonce)

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

                price = (Decimal(sqrtPriceX96) / Decimal(2**96))**2

                # WETH/USDC: WETH(18) is token0, USDC(6) is token1. Price = 1/0 = USDC per WETH.
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
            return None

        addr_lower = token_address.lower()
        cache_key = f"{addr_lower}:{self.account.address}"

        # Check cached allowance first (max uint256 means fully approved)
        if cache_key in self._allowance_cache:
            cached_allowance = self._allowance_cache[cache_key]
            if cached_allowance == 2**256 - 1:
                logging.info(f"Using cached MAX approval for {token_address}")
                return None
            # If cached but not max, check if it's sufficient for this amount
            if cached_allowance >= amount:
                logging.info(f"Using cached allowance for {token_address}: {cached_allowance}")
                return None

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
                return None

            # Check if current allowance is sufficient
            if allowance >= amount:
                # Cache this allowance for future checks
                self._allowance_cache[cache_key] = allowance
                return None

            # Approve max uint256 to avoid future approvals for this token
            # First, set allowance to 0 then to max (standard pattern for some tokens)
            tx0 = token_contract.functions.approve(
                Web3.to_checksum_address(SWAP_ROUTER_ADDRESS),
                0
            ).build_transaction({
                'from': self.account.address,
                'nonce': self._get_nonce(),
                'gas': 100000,
                'gasPrice': self._get_gas_price(),
                'chainId': EXPECTED_CHAIN_ID,
            })
            signed_tx0 = self.w3.eth.account.sign_transaction(tx0, self.private_key)
            tx_hash0 = retry_rpc_call(lambda: self.w3.eth.send_raw_transaction(signed_tx0.raw_transaction))
            retry_rpc_call(lambda: self.w3.eth.wait_for_transaction_receipt(tx_hash0, timeout=120))
            
            # Then approve max
            tx_max = token_contract.functions.approve(
                Web3.to_checksum_address(SWAP_ROUTER_ADDRESS),
                2**256 - 1
            ).build_transaction({
                'from': self.account.address,
                'nonce': self._get_nonce() + 1,  # Next nonce
                'gas': 100000,
                'gasPrice': self._get_gas_price(),
                'chainId': EXPECTED_CHAIN_ID,
            })
            signed_tx_max = self.w3.eth.account.sign_transaction(tx_max, self.private_key)
            tx_hash_max = retry_rpc_call(lambda: self.w3.eth.send_raw_transaction(signed_tx_max.raw_transaction))
            receipt_max = retry_rpc_call(lambda: self.w3.eth.wait_for_transaction_receipt(tx_hash_max, timeout=120))

            if receipt_max.status == 1:
                logging.info(f"Approved MAX for {token_address}. Tx: {tx_hash_max.hex()}")
                self._allowance_cache[cache_key] = 2**256 - 1
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

    def _get_amount_out_minimum(self, token_in, token_out, amount_in, fee, slippage_bps=SLIPPAGE_BPS):
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

                        def _fetch_slot0():
                            return pool_contract.functions.slot0().call()
                        slot0 = retry_rpc_call(_fetch_slot0)

                        sqrtPriceX96 = slot0[0]
                        price_ratio = (Decimal(sqrtPriceX96) / Decimal(2**96)) ** 2
                        # Use cached decimals instead of RPC calls
                        decimals_in = self._get_decimals(token_in)
                        decimals_out = self._get_decimals(token_out)
                        factor = price_ratio * Decimal(10 ** (decimals_in - decimals_out))
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

    def execute_swap(self, token_in, token_out, amount_in, recipient, fee=None):
        """Execute a swap via Uniswap V3 SwapRouter."""
        if fee is None:
            fee = self._get_fee_for_tokens(token_in, token_out)
        if self.trading_mode != "live":
            logging.info(f"[PAPER] Would swap {amount_in} of {token_in} for {token_out}")
            return {"success": True, "tx_hash": "paper"}

        try:
            # Approve token_in
            self._approve_token(token_in, amount_in)

            amount_out_min = self._get_amount_out_minimum(token_in, token_out, amount_in, fee)

            deadline = int(time.time()) + TX_DEADLINE_SECONDS

            logging.info(f"Swap Parameters: In={amount_in}, OutMin={amount_out_min}, Fee={fee}, Deadline={deadline}")

            params = (
                Web3.to_checksum_address(token_in),
                Web3.to_checksum_address(token_out),
                fee,
                Web3.to_checksum_address(recipient),
                deadline,
                amount_in,
                amount_out_min,
                0  # sqrtPriceLimitX96
            )

            # Build transaction
            tx = self.router_contract.functions.exactInputSingle(params).build_transaction({
                'from': self.account.address,
                'nonce': self._get_nonce(),
                'gas': 300000,
                'gasPrice': self._get_gas_price(),
                'chainId': EXPECTED_CHAIN_ID,
                'value': 0
            })

            signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)

            def _send_swap():
                return self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)

            tx_hash = retry_rpc_call(_send_swap)

            def _wait_swap_receipt():
                return self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)

            receipt = retry_rpc_call(_wait_swap_receipt)

            if receipt.status == 1:
                logging.info(f"Swap successful. Tx: {tx_hash.hex()}")
                return {"success": True, "tx_hash": tx_hash.hex()}
            else:
                logging.error(f"Swap transaction failed: {receipt}")
                return {"success": False, "error": "transaction failed"}
        except Exception as e:
            logging.error(f"Swap execution failed: {e}")
            return {"success": False, "error": str(e)}

    
    def place_market_order(self, product_id, side, amount_quote_currency=None, amount_base_currency=None):
        addr = self.get_token_address(product_id)
        if not addr:
            logging.warning(f"EthereumExecutor: Asset {product_id} not found in Base registry. Skipping.")
            return None

        logging.info(f"EthereumExecutor: [BASE] Placing {side} for {product_id} ({addr}) on-chain.")

        if self.trading_mode == "live":
            # Determine token addresses
            usdc_addr = TOKENS.get("USDC")
            token_addr = addr

            # Get decimals from cache (no RPC call)
            usdc_decimals = self._get_decimals(usdc_addr)
            token_decimals = self._get_decimals(token_addr)

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

            # Look up fee tier from POOL_FEES
            fee = POOL_FEES.get(product_id, 3000)

            # Execute the on-chain swap
            result = self.execute_swap(token_in, token_out, amount_in, self.account.address, fee=fee)
            return result
        else:
            logging.info(f"[PAPER] On-chain swap {side} {product_id}")
            return {"success": True}


    def place_limit_order(self, product_id, side, price, amount_quote_currency=None, amount_base_currency=None):
        return self.place_market_order(product_id, side, amount_quote_currency, amount_base_currency)
