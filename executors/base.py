"""Base protocol and interface for trading executors"""
from typing import Protocol, runtime_checkable
from decimal import Decimal
import pandas as pd


@runtime_checkable
class TradingExecutor(Protocol):
    """
    Protocol defining the executor interface.

    All executors must implement these methods to work with the trading bot.
    This protocol ensures type safety and prevents runtime errors from missing methods.
    """

    def get_balances(self) -> dict:
        """
        Get current balances (cash and crypto).

        Returns:
            dict: Format {"cash": {"USD": float, "USDC": float}, "crypto": {"BTC": float, ...}}
        """
        ...

    def get_product_details(self, product_id: str) -> dict:
        """
        Get product details (price, increments, etc.).

        Args:
            product_id: Product identifier (e.g., "BTC-USDC")

        Returns:
            dict: Format {"price": Decimal, "quote_increment": str, "base_min_size": str, ...}
        """
        ...

    def get_market_data(
        self,
        product_id: str,
        window: int = 300,
        granularity: str = "1h"
    ) -> pd.DataFrame:
        """
        Get historical market data (OHLCV).

        Args:
            product_id: Product identifier (e.g., "BTC-USDC")
            window: Number of candles to fetch (default: 300)
            granularity: Candle size string (default: "1h").
                Valid: "5m", "15m", "30m", "1h", "2h", "6h", "1d"

        Returns:
            pd.DataFrame: Columns ['start', 'low', 'high', 'open', 'close', 'volume']
        """
        ...

    def place_limit_order(
        self,
        product_id: str,
        side: str,
        limit_price: Decimal | float,
        amount_quote_currency: Decimal | float | None = None,
        amount_base_currency: Decimal | float | None = None
    ) -> dict:
        """
        Place limit order.

        Args:
            product_id: Product identifier (e.g., "BTC-USDC")
            side: "BUY" or "SELL"
            limit_price: Limit price
            amount_quote_currency: Amount in quote currency (USDC)
            amount_base_currency: Amount in base currency (BTC)

        Returns:
            dict: Order result with "success" field
        """
        ...

    def place_market_order(
        self,
        product_id: str,
        side: str,
        size: Decimal | float
    ) -> dict:
        """
        Place market order.

        Args:
            product_id: Product identifier (e.g., "BTC-USDC")
            side: "BUY" or "SELL"
            size: Order size

        Returns:
            dict: Order result with "success" field
        """
        ...

    def check_order_filled(self, order_id: str) -> dict | None:
        """
        Check if order filled. Returns {'price': float, 'fee': float} or None.

        Args:
            order_id: Order identifier

        Returns:
            Decimal: Fill price if filled, None otherwise
        """
        ...

    def cancel_open_orders(self, product_id: str | None = None) -> None:
        """
        Cancel open orders.

        Args:
            product_id: Product identifier (optional, cancels all if None)
        """
        ...

    def get_supported_assets(self) -> list[str]:
        """
        Get list of supported assets.

        Returns:
            list[str]: List of asset symbols (e.g., ["BTC", "ETH", "LINK"])
        """
        ...


def validate_executor(executor: object) -> None:
    """
    Validate that an object implements the TradingExecutor protocol.

    Args:
        executor: Object to validate

    Raises:
        TypeError: If executor is missing required methods
    """
    required_methods = [
        'get_balances',
        'get_product_details',
        'get_market_data',
        'place_limit_order',
        'place_market_order',
        'cancel_open_orders',
        'get_supported_assets'
    ]

    missing_methods = []
    for method in required_methods:
        if not hasattr(executor, method) or not callable(getattr(executor, method)):
            missing_methods.append(method)

    if missing_methods:
        raise TypeError(
            f"Executor {executor.__class__.__name__} missing required methods: "
            f"{', '.join(missing_methods)}"
        )
