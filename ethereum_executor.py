import logging
import time
import os
from web3 import Web3
from decimal import Decimal

# Strict Network Validation for Base
EXPECTED_CHAIN_ID = 8453  # Base Mainnet

# Minimal ABI
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"}
]

UNISWAP_V3_POOL_ABI = [
    {"constant": True, "inputs": [], "name": "slot0", "outputs": [{"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"}, {"name": "observationIndex", "type": "uint16"}, {"name": "observationCardinality", "type": "uint16"}, {"name": "observationCardinalityNext", "type": "uint16"}, {"name": "feeProtocol", "type": "uint8"}, {"name": "unlocked", "type": "bool"}], "type": "function"}
]

# Base Token Registry
TOKENS = {
    "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",   # Native USDC
    "USDC.e": "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca", # Bridged USDC
    "WETH": "0x4200000000000000000000000000000000000006",
    "BTC": "0xcbB7C919d9600a40748358403e5Ff15d0d670081",    # cbBTC
    "DEGEN": "0x4ed4E281562193f5C8c11259D3e21839951e7d23",
    "AERO": "0x9401811A062933285c64D72A25e8e3cf24f3fFBE",
}

# Known Uniswap V3 Pools on Base
POOLS = {
    "ETH-USDC": "0x4c36388be6f416a29c8d8eee81c771ce6be14b18", # WETH/USDC (Native) 0.05%
    "BTC-USDC": "0x12745348866297371569477B73738018e6e8772a", # cbBTC Placeholder
}

class EthereumExecutor:
    """Handles interaction with Base blockchain via Web3 with dynamic balance detection."""
    def __init__(self, rpc_url, private_key, trading_mode="paper"):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.private_key = private_key
        self.trading_mode = trading_mode
        
        try:
            chain_id = self.w3.eth.chain_id
            if chain_id != EXPECTED_CHAIN_ID:
                raise RuntimeError(f"NETWORK MISMATCH! Expected Chain ID {EXPECTED_CHAIN_ID} (Base), but got {chain_id}.")
            logging.info(f"Connected to Base Network (Chain ID: {chain_id})")
        except Exception as e:
            logging.error(f"Failed to validate network: {e}")
            raise

        self.account = self.w3.eth.account.from_key(private_key) if private_key else None
        if self.account:
            logging.info(f"Using Base Wallet: {self.account.address}")
        
    def _check_balance(self, balances, symbol, address):
        """Helper to fetch and add ERC20 balance if non-zero."""
        try:
            addr = Web3.to_checksum_address(address)
            if self.w3.eth.get_code(addr) != b"":
                contract = self.w3.eth.contract(address=addr, abi=ERC20_ABI)
                raw_bal = contract.functions.balanceOf(self.account.address).call()
                if raw_bal > 0:
                    decimals = contract.functions.decimals().call()
                    val = float(raw_bal) / (10 ** decimals)
                    
                    if symbol == "USDC" or symbol == "USDC.e":
                        balances["cash"]["USDC"] = balances["cash"].get("USDC", 0.0) + val
                    elif symbol == "WETH":
                        # Map WETH to ETH for the trading strategy
                        balances["crypto"]["ETH"] = balances["crypto"].get("ETH", 0.0) + val
                    else:
                        balances["crypto"][symbol] = val
        except Exception as e:
            logging.debug(f"Could not check balance for {symbol} ({address}): {e}")

    def get_balances(self):
        """Fetch ETH and all registered ERC20 balances on Base."""
        if not self.account:
            return {"cash": {"USDC": 0.0}, "crypto": {}}
        
        balances = {"cash": {"USDC": 0.0}, "crypto": {}}
        
        # 1. Native ETH (Gas)
        try:
            eth_bal = self.w3.eth.get_balance(self.account.address)
            balances["crypto"]["ETH_NATIVE"] = float(self.w3.from_wei(eth_bal, 'ether'))
        except: pass
        
        # 2. Registered Tokens
        for symbol, addr in TOKENS.items():
            self._check_balance(balances, symbol, addr)
            
        # 3. Extra Tokens from Environment (Format: "0xaddr:SYM,0xaddr:SYM")
        extra = os.environ.get("EXTRA_TOKENS")
        if extra:
            for item in extra.split(","):
                try:
                    addr, sym = item.split(":")
                    self._check_balance(balances, sym.strip(), addr.strip())
                except: pass
        
        return balances

    def get_market_data(self, product_id, window):
        return None

    def get_product_details(self, product_id):
        """Fetch current price from Uniswap V3 Pool on Base."""
        if product_id not in POOLS:
            return None
            
        pool_address = Web3.to_checksum_address(POOLS[product_id])
        try:
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
        except:
            return None

    def get_token_address(self, product_id):
        """Helper to find on-chain address for a product (e.g., ETH-USDC)."""
        asset = product_id.split("-")[0]
        # Check standard TOKENS
        if asset in TOKENS:
            return TOKENS[asset]
        # Check EXTRA_TOKENS
        extra = os.environ.get("EXTRA_TOKENS")
        if extra:
            for item in extra.split(","):
                try:
                    addr, sym = item.split(":")
                    if sym.strip().upper() == asset.upper():
                        return addr.strip()
                except: pass
        return None

    def place_market_order(self, product_id, side, amount_quote_currency=None, amount_base_currency=None):
        addr = self.get_token_address(product_id)
        if not addr:
            logging.warning(f"EthereumExecutor: Asset {product_id} not found in Base registry. Skipping.")
            return None

        logging.info(f"EthereumExecutor: [BASE] Placing {side} for {product_id} ({addr}) on-chain.")
        if self.trading_mode == "live":
            logging.error("On-chain Swaps not yet fully implemented.")
            return None
        return {"success": True}

    def place_limit_order(self, product_id, side, price, amount_quote_currency=None, amount_base_currency=None):
        return self.place_market_order(product_id, side, amount_quote_currency, amount_base_currency)
