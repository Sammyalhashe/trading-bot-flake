"""Coinbase CFM (Financial Markets) futures executor.

Targets US-accessible nano Bitcoin/Ether perp-style futures contracts
via the CFM endpoints. These are contract-based (not fractional crypto)
with hourly funding rates and USD settlement.

Products:
- BTC: BIP-20DEC30-CDE (1 contract = 0.01 BTC)
- ETH: ETP-20DEC30-CDE (1 contract = 0.1 ETH)

Uses the same JWT auth and order endpoint as spot, but with:
- CFM-specific product IDs
- Contract-based sizing (whole contracts only)
- leverage + margin_type fields on orders
- CFM balance/position endpoints instead of account balances
"""

import logging
import math
import time
import uuid

from .coinbase_executor import CoinbaseExecutor


class CoinbaseFuturesExecutor(CoinbaseExecutor):
    """Executor for Coinbase CFM futures (US).

    Accepts spot-format product IDs (BTC-USDC) from the trading bot and
    internally maps them to CFM futures format (BIP-20DEC30-CDE).
    Converts between base asset amounts and contract counts transparently.
    """

    FUTURES_PRODUCTS = {
        "BTC": {"product_id": "BIP-20DEC30-CDE", "contract_size": 0.01},
        "ETH": {"product_id": "ETP-20DEC30-CDE", "contract_size": 0.1},
    }

    def __init__(self, api_json_file, trading_mode="paper"):
        super().__init__(api_json_file, trading_mode)

    def _to_futures_product_id(self, product_id):
        """Convert spot product ID (BTC-USDC) to CFM futures ID."""
        base = product_id.split("-")[0].upper()
        info = self.FUTURES_PRODUCTS.get(base)
        if not info:
            logging.warning(f"No futures product mapping for {product_id}, using as-is")
            return product_id
        return info["product_id"]

    def _get_contract_size(self, product_id):
        """Get the contract size in base asset for a product."""
        base = product_id.split("-")[0].upper()
        info = self.FUTURES_PRODUCTS.get(base)
        return info["contract_size"] if info else 1.0

    def _base_to_contracts(self, base_amount, product_id):
        """Convert base asset amount to whole number of contracts (rounds down)."""
        contract_size = self._get_contract_size(product_id)
        return int(base_amount / contract_size)

    def _contracts_to_base(self, contracts, product_id):
        """Convert contract count to base asset amount."""
        contract_size = self._get_contract_size(product_id)
        return contracts * contract_size

    def get_supported_assets(self):
        """Only BTC and ETH futures available."""
        return ["BTC", "ETH"]

    def get_balances(self):
        """Get CFM futures account balance and open positions.

        Returns in standard format for run_executor_strategy() compatibility:
        {
            "available": {"cash": {"USD": margin, "USDC": 0}, "crypto": {"BTC": size}},
            "total": {"cash": {"USD": margin, "USDC": 0}, "crypto": {"BTC": size}}
        }

        Positions are reported in base asset terms (not contracts) so the bot's
        portfolio valuation works correctly.
        """
        balances = {
            "available": {"cash": {"USD": 0.0, "USDC": 0.0}, "crypto": {}},
            "total": {"cash": {"USD": 0.0, "USDC": 0.0}, "crypto": {}},
        }

        # Get CFM balance summary
        summary = self.request("GET", "/api/v3/brokerage/cfm/balance_summary")
        if summary:
            # Available margin for new trades
            available = float(summary.get("cfm_usd_available", {}).get("value", 0))
            # Total account value (margin + unrealized PnL)
            total = float(summary.get("total_usd_balance", {}).get("value", 0))
            balances["available"]["cash"]["USD"] = available
            balances["total"]["cash"]["USD"] = total

        # Get open positions
        positions = self.request("GET", "/api/v3/brokerage/cfm/positions")
        if positions and "positions" in positions:
            for pos in positions["positions"]:
                product_id = pos.get("product_id", "")
                side = pos.get("side", "").upper()
                number_of_contracts = abs(int(pos.get("number_of_contracts", 0)))

                if number_of_contracts == 0:
                    continue
                if side != "LONG":  # long-only for now
                    continue

                # Find which base asset this is
                base = None
                for asset, info in self.FUTURES_PRODUCTS.items():
                    if info["product_id"] == product_id:
                        base = asset
                        break
                if not base:
                    continue

                # Convert contracts back to base asset amount
                base_amount = self._contracts_to_base(number_of_contracts, f"{base}-USDC")
                balances["available"]["crypto"][base] = base_amount
                balances["total"]["crypto"][base] = base_amount

        return balances

    def get_market_data(self, product_id, window):
        """Get candle data using futures product ID."""
        futures_id = self._to_futures_product_id(product_id)
        return super().get_market_data(futures_id, window)

    def get_product_details(self, product_id):
        """Get product details, with contract-appropriate increments."""
        futures_id = self._to_futures_product_id(product_id)
        details = super().get_product_details(futures_id)
        if details:
            # Override base_increment to be 1 contract worth of base asset
            # This ensures the bot's rounding logic produces whole contracts
            contract_size = self._get_contract_size(product_id)
            details = dict(details)  # copy to avoid mutating cache
            details["base_increment"] = str(contract_size)
        return details

    def get_best_bid_ask(self, product_id):
        """Get best bid/ask using futures product ID."""
        futures_id = self._to_futures_product_id(product_id)
        return super().get_best_bid_ask(futures_id)

    def cancel_open_orders(self, product_id):
        """Cancel open orders using futures product ID."""
        futures_id = self._to_futures_product_id(product_id)
        return super().cancel_open_orders(futures_id)

    def place_limit_order(self, product_id, side, price, amount_quote_currency=None, amount_base_currency=None):
        """Place a limit order on the CFM futures market.

        Converts amounts to whole contracts and adds leverage/margin_type fields.
        """
        futures_id = self._to_futures_product_id(product_id)
        if self.trading_mode == "live":
            self.cancel_open_orders(product_id)

        details = self.get_product_details(product_id)
        if not details:
            return None

        best_bid, best_ask = self.get_best_bid_ask(product_id)
        tick = float(details['quote_increment'])

        if side == 'BUY':
            price = min(price, best_ask - tick if best_ask else price)
        else:
            price = max(price, best_bid + tick if best_bid else price)

        # Calculate base size and convert to contracts
        if amount_quote_currency:
            base_size = float(amount_quote_currency) / float(price)
        else:
            base_size = amount_base_currency

        if base_size is None:
            logging.error(f"Cannot place futures order for {product_id}: no amount provided.")
            return None

        # Round to whole contracts
        contracts = self._base_to_contracts(base_size, product_id)
        if contracts < 1:
            logging.warning(f"Order too small for 1 contract on {futures_id} (need {self._get_contract_size(product_id)} base, got {base_size:.6f})")
            return None

        # Convert back to base size in contract increments
        contract_base_size = self._contracts_to_base(contracts, product_id)
        rounded_price = self._round_to_increment(price, details['quote_increment'])

        order_id = str(uuid.uuid4())
        payload = {
            "client_order_id": order_id,
            "product_id": futures_id,
            "side": side,
            "leverage": "1",
            "margin_type": "CROSS",
            "order_configuration": {
                "limit_limit_gtc": {
                    "base_size": str(contract_base_size),
                    "limit_price": str(rounded_price),
                    "post_only": True,
                }
            },
        }

        logging.info(f"Placing FUTURES LIMIT {side} for {futures_id} at {rounded_price} ({contracts} contracts)")
        if self.trading_mode == "live":
            return self.request("POST", "/api/v3/brokerage/orders", payload)
        return {"success": True, "order_id": order_id}

    def place_aggressive_limit_order(self, product_id, side, price, amount_base_currency):
        """Place aggressive limit (no post_only) for stop-losses on futures."""
        futures_id = self._to_futures_product_id(product_id)
        if self.trading_mode == "live":
            self.cancel_open_orders(product_id)

        details = self.get_product_details(product_id)
        if not details:
            return None

        best_bid, best_ask = self.get_best_bid_ask(product_id)
        tick = float(details['quote_increment'])

        if side == 'SELL':
            price = min(price, best_bid - tick) if best_bid else price
        else:
            price = max(price, best_ask + tick) if best_ask else price

        contracts = self._base_to_contracts(amount_base_currency, product_id)
        if contracts < 1:
            return None

        contract_base_size = self._contracts_to_base(contracts, product_id)
        rounded_price = self._round_to_increment(price, details['quote_increment'])

        order_id = str(uuid.uuid4())
        payload = {
            "client_order_id": order_id,
            "product_id": futures_id,
            "side": side,
            "leverage": "1",
            "margin_type": "CROSS",
            "order_configuration": {
                "limit_limit_gtc": {
                    "base_size": str(contract_base_size),
                    "limit_price": str(rounded_price),
                    "post_only": False,
                }
            },
        }

        logging.info(f"Placing AGGRESSIVE FUTURES LIMIT {side} for {futures_id} at {rounded_price} ({contracts} contracts)")
        if self.trading_mode == "live":
            return self.request("POST", "/api/v3/brokerage/orders", payload)
        return {"success": True, "order_id": order_id}

    def place_market_order(self, product_id, side, amount_quote_currency=None, amount_base_currency=None):
        """Place a market order on the CFM futures market."""
        futures_id = self._to_futures_product_id(product_id)
        if self.trading_mode == "live":
            self.cancel_open_orders(product_id)

        details = self.get_product_details(product_id)
        if not details:
            return None

        if amount_quote_currency:
            base_size = float(amount_quote_currency) / float(details.get('price', 1))
        else:
            base_size = amount_base_currency

        if base_size is None:
            logging.error(f"Cannot place futures market order for {product_id}: no amount provided.")
            return None

        contracts = self._base_to_contracts(base_size, product_id)
        if contracts < 1:
            return None

        contract_base_size = self._contracts_to_base(contracts, product_id)

        order_id = str(uuid.uuid4())
        payload = {
            "client_order_id": order_id,
            "product_id": futures_id,
            "side": side,
            "leverage": "1",
            "margin_type": "CROSS",
            "order_configuration": {
                "market_market_ioc": {
                    "base_size": str(contract_base_size),
                }
            },
        }

        logging.info(f"Placing FUTURES MARKET {side} for {futures_id} ({contracts} contracts)")
        if self.trading_mode == "live":
            return self.request("POST", "/api/v3/brokerage/orders", payload)
        return {"success": True, "order_id": order_id}
