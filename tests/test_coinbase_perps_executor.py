import pytest
from unittest.mock import MagicMock, patch, call
import sys
import os

# Add parent dir to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from executors.coinbase_perps_executor import CoinbasePerpsExecutor
from executors.coinbase_executor import CoinbaseExecutor


@pytest.fixture
def mock_executor():
    """Create a CoinbasePerpsExecutor with mocked API calls."""
    with patch.object(CoinbaseExecutor, '__init__', lambda self, *a, **k: None):
        executor = CoinbasePerpsExecutor.__new__(CoinbasePerpsExecutor)
        executor.product_details_cache = {}
        executor.portfolio_uuid = "test-portfolio-uuid"
        executor.trading_mode = "paper"
        return executor


class TestProductIdMapping:
    """Test that spot product IDs are correctly mapped to perps."""

    def test_btc_mapping(self, mock_executor):
        assert mock_executor._to_perp_product_id("BTC-USDC") == "BTC-PERP-INTX"

    def test_eth_mapping(self, mock_executor):
        assert mock_executor._to_perp_product_id("ETH-USDC") == "ETH-PERP-INTX"

    def test_unknown_asset_passes_through(self, mock_executor):
        assert mock_executor._to_perp_product_id("SOL-USDC") == "SOL-USDC"

    def test_case_insensitive(self, mock_executor):
        assert mock_executor._to_perp_product_id("btc-usdc") == "BTC-PERP-INTX"

    def test_reverse_mapping(self, mock_executor):
        assert mock_executor._from_perp_product_id("BTC-PERP-INTX") == "BTC-USDC"
        assert mock_executor._from_perp_product_id("ETH-PERP-INTX") == "ETH-USDC"


class TestSupportedAssets:
    def test_only_btc_and_eth(self, mock_executor):
        assets = mock_executor.get_supported_assets()
        assert assets == ["BTC", "ETH"]


class TestGetBalances:
    """Test that perps portfolio and positions are parsed into the standard format."""

    def test_balances_with_positions(self, mock_executor):
        """Portfolio with margin and an open BTC long."""
        mock_executor.request = MagicMock(side_effect=[
            # Portfolio summary
            {
                "portfolio": {
                    "available_margin": {"value": "5000.00"},
                    "total_balance": {"value": "10000.00"},
                }
            },
            # Positions
            {
                "positions": [
                    {
                        "product_id": "BTC-PERP-INTX",
                        "net_size": "0.05",
                    }
                ]
            },
        ])

        balances = mock_executor.get_balances()

        assert balances["available"]["cash"]["USDC"] == 5000.0
        assert balances["total"]["cash"]["USDC"] == 10000.0
        assert balances["available"]["crypto"]["BTC"] == 0.05
        assert balances["total"]["crypto"]["BTC"] == 0.05

    def test_balances_no_positions(self, mock_executor):
        """Portfolio with margin but no open positions."""
        mock_executor.request = MagicMock(side_effect=[
            {"portfolio": {"available_margin": {"value": "8000.00"}, "total_balance": {"value": "8000.00"}}},
            {"positions": []},
        ])

        balances = mock_executor.get_balances()

        assert balances["available"]["cash"]["USDC"] == 8000.0
        assert balances["available"]["crypto"] == {}

    def test_short_positions_ignored(self, mock_executor):
        """Short positions (negative net_size) are ignored in long-only mode."""
        mock_executor.request = MagicMock(side_effect=[
            {"portfolio": {"available_margin": {"value": "5000.00"}, "total_balance": {"value": "10000.00"}}},
            {
                "positions": [
                    {"product_id": "BTC-PERP-INTX", "net_size": "-0.05"},
                    {"product_id": "ETH-PERP-INTX", "net_size": "1.5"},
                ]
            },
        ])

        balances = mock_executor.get_balances()

        assert "BTC" not in balances["available"]["crypto"]
        assert balances["available"]["crypto"]["ETH"] == 1.5

    def test_unsupported_product_ignored(self, mock_executor):
        """Positions in non-mapped products are ignored."""
        mock_executor.request = MagicMock(side_effect=[
            {"portfolio": {"available_margin": {"value": "5000.00"}, "total_balance": {"value": "5000.00"}}},
            {
                "positions": [
                    {"product_id": "SOL-PERP-INTX", "net_size": "10.0"},
                ]
            },
        ])

        balances = mock_executor.get_balances()

        assert "SOL" not in balances["available"]["crypto"]

    def test_api_failure_returns_empty(self, mock_executor):
        """API failures return zero balances."""
        mock_executor.request = MagicMock(return_value=None)

        balances = mock_executor.get_balances()

        assert balances["available"]["cash"]["USDC"] == 0.0
        assert balances["available"]["crypto"] == {}

    def test_correct_endpoints_called(self, mock_executor):
        """Verify the right API paths are called."""
        mock_executor.request = MagicMock(side_effect=[
            {"portfolio": {"available_margin": {"value": "0"}, "total_balance": {"value": "0"}}},
            {"positions": []},
        ])

        mock_executor.get_balances()

        calls = mock_executor.request.call_args_list
        assert calls[0] == call("GET", "/api/v3/brokerage/intx/portfolio/test-portfolio-uuid")
        assert calls[1] == call("GET", "/api/v3/brokerage/intx/positions/test-portfolio-uuid")


class TestOrderRouting:
    """Test that orders are routed to perps products."""

    def test_limit_order_uses_perp_product_id(self, mock_executor):
        """place_limit_order translates BTC-USDC -> BTC-PERP-INTX."""
        mock_executor.get_product_details = MagicMock(return_value={
            "price": "100000.0",
            "quote_increment": "0.01",
            "base_increment": "0.0001",
        })
        mock_executor.get_best_bid_ask = MagicMock(return_value=(99999.0, 100001.0))
        mock_executor.cancel_open_orders = MagicMock()
        mock_executor.request = MagicMock(return_value={"success": True, "order_id": "test-123"})

        result = mock_executor.place_limit_order("BTC-USDC", "BUY", 100000.0, amount_quote_currency=1000.0)

        assert result is not None
        # Verify get_product_details was called with the perp product ID
        mock_executor.get_product_details.assert_called_with("BTC-PERP-INTX")

    def test_market_order_uses_perp_product_id(self, mock_executor):
        """place_market_order translates product ID."""
        mock_executor.get_product_details = MagicMock(return_value={
            "price": "100000.0",
            "base_increment": "0.0001",
        })
        mock_executor.request = MagicMock(return_value={"success": True})

        mock_executor.place_market_order("BTC-USDC", "SELL", amount_base_currency=0.01)

        # In paper mode, get_product_details is called with the perp product ID
        mock_executor.get_product_details.assert_called_with("BTC-PERP-INTX")

    def test_aggressive_limit_order_uses_perp_product_id(self, mock_executor):
        """place_aggressive_limit_order (used for stop losses) translates product ID."""
        mock_executor.get_product_details = MagicMock(return_value={
            "price": "100000.0",
            "quote_increment": "0.01",
            "base_increment": "0.0001",
        })
        mock_executor.get_best_bid_ask = MagicMock(return_value=(99999.0, 100001.0))
        mock_executor.request = MagicMock(return_value={"success": True})

        mock_executor.place_aggressive_limit_order("ETH-USDC", "SELL", 3000.0, amount_base_currency=1.0)

        # Verify get_product_details was called with the perp product ID
        mock_executor.get_product_details.assert_called_with("ETH-PERP-INTX")


class TestMarketData:
    """Test that market data queries use perps products."""

    def test_get_market_data_uses_perp_id(self, mock_executor):
        """get_market_data translates product ID."""
        mock_executor.request = MagicMock(return_value={
            "candles": [
                ["1700000000", "99000", "101000", "99500", "100500", "100.5"],
                ["1700003600", "100000", "102000", "100500", "101500", "120.3"],
            ]
        })

        df = mock_executor.get_market_data("BTC-USDC", 50)

        # Verify API was called with perp product ID
        call_args = mock_executor.request.call_args
        assert "BTC-PERP-INTX" in call_args[0][1]

    def test_get_product_details_uses_perp_id(self, mock_executor):
        """get_product_details translates product ID."""
        mock_executor.request = MagicMock(return_value={"price": "100000.0"})

        mock_executor.get_product_details("BTC-USDC")

        call_args = mock_executor.request.call_args
        assert "BTC-PERP-INTX" in call_args[0][1]


class TestInheritance:
    """Test that CoinbasePerpsExecutor properly inherits from CoinbaseExecutor."""

    def test_is_subclass(self):
        assert issubclass(CoinbasePerpsExecutor, CoinbaseExecutor)

    def test_check_order_filled_inherited(self, mock_executor):
        """check_order_filled should be inherited directly (same orders API)."""
        assert hasattr(mock_executor, 'check_order_filled')
        # It should be the parent's method, not overridden
        assert mock_executor.check_order_filled.__func__ is CoinbaseExecutor.check_order_filled

    def test_build_ws_jwt_inherited(self, mock_executor):
        """WebSocket JWT building should be inherited."""
        assert hasattr(mock_executor, 'build_ws_jwt')


class TestExecutorProtocol:
    """Test that the executor satisfies the TradingExecutor protocol."""

    def test_has_all_required_methods(self, mock_executor):
        required = [
            'get_balances', 'get_product_details', 'get_market_data',
            'place_limit_order', 'place_market_order', 'cancel_open_orders',
            'get_supported_assets',
        ]
        for method in required:
            assert hasattr(mock_executor, method), f"Missing required method: {method}"
            assert callable(getattr(mock_executor, method)), f"{method} is not callable"
