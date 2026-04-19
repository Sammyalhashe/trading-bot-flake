"""Coinbase Perpetual Futures executor.

Inherits from CoinbaseExecutor, overriding product ID mapping and balance
queries to work with the Coinbase International Exchange (INTX) perpetuals.

Uses the same JWT auth and order endpoints, but with:
- Product IDs like BTC-PERP-INTX instead of BTC-USDC
- Portfolio-based margin/position tracking instead of account balances
- Lower fees (0.00% maker / 0.03% taker)
"""

import logging

from .coinbase_executor import CoinbaseExecutor


class CoinbasePerpsExecutor(CoinbaseExecutor):
    """Executor for Coinbase perpetual futures trading.

    Accepts spot-format product IDs (BTC-USDC) from the trading bot and
    internally maps them to perp format (BTC-PERP-INTX) for API calls.
    This keeps the executor compatible with run_executor_strategy() without
    any changes to the bot's product ID logic.
    """

    # Map of base asset -> perps product ID
    PERP_PRODUCTS = {
        "BTC": "BTC-PERP-INTX",
        "ETH": "ETH-PERP-INTX",
    }

    def __init__(self, api_json_file, portfolio_uuid, trading_mode="paper"):
        super().__init__(api_json_file, trading_mode)
        self.portfolio_uuid = portfolio_uuid

    def _to_perp_product_id(self, product_id):
        """Convert spot product ID (BTC-USDC) to perp (BTC-PERP-INTX)."""
        base = product_id.split("-")[0].upper()
        perp_id = self.PERP_PRODUCTS.get(base)
        if not perp_id:
            logging.warning(f"No perps product mapping for {product_id}, using as-is")
            return product_id
        return perp_id

    def _from_perp_product_id(self, perp_product_id):
        """Convert perp product ID (BTC-PERP-INTX) to spot format (BTC-USDC)."""
        base = perp_product_id.split("-")[0].upper()
        return f"{base}-USDC"

    def get_supported_assets(self):
        """Only BTC and ETH perps for now (most liquid)."""
        return ["BTC", "ETH"]

    def get_balances(self):
        """Get perpetuals portfolio margin and open positions.

        Returns balances in the same format as CoinbaseExecutor so
        run_executor_strategy() can process it without changes:
        {
            "available": {"cash": {"USD": 0, "USDC": margin}, "crypto": {"BTC": size}},
            "total": {"cash": {"USD": 0, "USDC": margin}, "crypto": {"BTC": size}}
        }
        """
        balances = {
            "available": {"cash": {"USD": 0.0, "USDC": 0.0}, "crypto": {}},
            "total": {"cash": {"USD": 0.0, "USDC": 0.0}, "crypto": {}},
        }

        # Get portfolio summary for margin/buying power
        summary = self.request(
            "GET",
            f"/api/v3/brokerage/intx/portfolio/{self.portfolio_uuid}",
        )
        if summary and "portfolio" in summary:
            portfolio = summary["portfolio"]
            # Available margin = buying power (what we can use for new trades)
            available_margin = float(portfolio.get("available_margin", {}).get("value", 0))
            # Total margin = portfolio value (collateral)
            total_margin = float(portfolio.get("total_balance", {}).get("value", 0))
            balances["available"]["cash"]["USDC"] = available_margin
            balances["total"]["cash"]["USDC"] = total_margin

        # Get open positions
        positions = self.request(
            "GET",
            f"/api/v3/brokerage/intx/positions/{self.portfolio_uuid}",
        )
        if positions and "positions" in positions:
            for pos in positions["positions"]:
                product_id = pos.get("product_id", "")
                base = product_id.split("-")[0].upper()
                if base not in self.PERP_PRODUCTS:
                    continue

                # net_size is positive for longs, negative for shorts
                net_size = float(pos.get("net_size", 0))
                if net_size > 0:  # long-only for now
                    balances["available"]["crypto"][base] = net_size
                    balances["total"]["crypto"][base] = net_size

        return balances

    def get_market_data(self, product_id, window):
        """Get candle data using perps product ID."""
        perp_id = self._to_perp_product_id(product_id)
        return super().get_market_data(perp_id, window)

    def get_product_details(self, product_id):
        """Get product details using perps product ID."""
        perp_id = self._to_perp_product_id(product_id)
        return super().get_product_details(perp_id)

    def get_best_bid_ask(self, product_id):
        """Get best bid/ask using perps product ID."""
        perp_id = self._to_perp_product_id(product_id)
        return super().get_best_bid_ask(perp_id)

    def cancel_open_orders(self, product_id):
        """Cancel open orders using perps product ID."""
        perp_id = self._to_perp_product_id(product_id)
        return super().cancel_open_orders(perp_id)

    def place_limit_order(self, product_id, side, price, amount_quote_currency=None, amount_base_currency=None):
        """Place a limit order on the perps market.

        Translates the spot product_id to perps format before delegating
        to the parent's order placement logic.
        """
        perp_id = self._to_perp_product_id(product_id)
        return super().place_limit_order(perp_id, side, price, amount_quote_currency, amount_base_currency)

    def place_aggressive_limit_order(self, product_id, side, price, amount_base_currency):
        """Place an aggressive limit order (no post_only) on the perps market."""
        perp_id = self._to_perp_product_id(product_id)
        return super().place_aggressive_limit_order(perp_id, side, price, amount_base_currency)

    def place_market_order(self, product_id, side, amount_quote_currency=None, amount_base_currency=None):
        """Place a market order on the perps market."""
        perp_id = self._to_perp_product_id(product_id)
        return super().place_market_order(perp_id, side, amount_quote_currency, amount_base_currency)
