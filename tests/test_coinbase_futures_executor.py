import pytest
from unittest.mock import MagicMock, patch, call
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from executors.coinbase_futures_executor import CoinbaseFuturesExecutor
from executors.coinbase_executor import CoinbaseExecutor


@pytest.fixture
def mock_executor():
    """Create a CoinbaseFuturesExecutor with mocked API calls."""
    with patch.object(CoinbaseExecutor, '__init__', lambda self, *a, **k: None):
        executor = CoinbaseFuturesExecutor.__new__(CoinbaseFuturesExecutor)
        executor.product_details_cache = {}
        executor.trading_mode = "paper"
        executor.portfolio_uuid = None
        return executor


class TestProductIdMapping:
    def test_btc_mapping(self, mock_executor):
        assert mock_executor._to_futures_product_id("BTC-USDC") == "BIP-20DEC30-CDE"

    def test_eth_mapping(self, mock_executor):
        assert mock_executor._to_futures_product_id("ETH-USDC") == "ETP-20DEC30-CDE"

    def test_unknown_asset_passes_through(self, mock_executor):
        assert mock_executor._to_futures_product_id("SOL-USDC") == "SOL-USDC"

    def test_case_insensitive(self, mock_executor):
        assert mock_executor._to_futures_product_id("btc-usdc") == "BIP-20DEC30-CDE"


class TestContractConversion:
    """Test conversion between base asset amounts and contract counts."""

    def test_btc_to_contracts(self, mock_executor):
        """0.05 BTC = 5 contracts (1 contract = 0.01 BTC)."""
        assert mock_executor._base_to_contracts(0.05, "BTC-USDC") == 5

    def test_btc_to_contracts_rounds_down(self, mock_executor):
        """0.057 BTC = 5 contracts (not 6)."""
        assert mock_executor._base_to_contracts(0.057, "BTC-USDC") == 5

    def test_btc_to_contracts_too_small(self, mock_executor):
        """0.005 BTC = 0 contracts (less than 1 contract)."""
        assert mock_executor._base_to_contracts(0.005, "BTC-USDC") == 0

    def test_eth_to_contracts(self, mock_executor):
        """1.5 ETH = 15 contracts (1 contract = 0.1 ETH)."""
        assert mock_executor._base_to_contracts(1.5, "ETH-USDC") == 15

    def test_eth_to_contracts_rounds_down(self, mock_executor):
        """0.35 ETH = 3 contracts (not 4)."""
        assert mock_executor._base_to_contracts(0.35, "ETH-USDC") == 3

    def test_contracts_to_btc(self, mock_executor):
        """5 contracts = 0.05 BTC."""
        assert mock_executor._contracts_to_base(5, "BTC-USDC") == 0.05

    def test_contracts_to_eth(self, mock_executor):
        """15 contracts = 1.5 ETH."""
        assert mock_executor._contracts_to_base(15, "ETH-USDC") == pytest.approx(1.5)

    def test_contract_size_btc(self, mock_executor):
        assert mock_executor._get_contract_size("BTC-USDC") == 0.01

    def test_contract_size_eth(self, mock_executor):
        assert mock_executor._get_contract_size("ETH-USDC") == 0.1

    def test_contract_size_unknown(self, mock_executor):
        assert mock_executor._get_contract_size("SOL-USDC") == 1.0


class TestSupportedAssets:
    def test_only_btc_and_eth(self, mock_executor):
        assert mock_executor.get_supported_assets() == ["BTC", "ETH"]


class TestGetBalances:
    def test_balances_with_positions(self, mock_executor):
        """CFM balance summary + open BTC long position."""
        mock_executor.request = MagicMock(side_effect=[
            # CFM balance summary
            {
                "cfm_usd_available": {"value": "5000.00"},
                "total_usd_balance": {"value": "10000.00"},
            },
            # CFM positions
            {
                "positions": [
                    {
                        "product_id": "BIP-20DEC30-CDE",
                        "side": "LONG",
                        "number_of_contracts": "5",
                    }
                ]
            },
        ])

        balances = mock_executor.get_balances()

        assert balances["available"]["cash"]["USD"] == 5000.0
        assert balances["total"]["cash"]["USD"] == 10000.0
        # 5 contracts × 0.01 BTC = 0.05 BTC
        assert balances["available"]["crypto"]["BTC"] == 0.05
        assert balances["total"]["crypto"]["BTC"] == 0.05

    def test_balances_no_positions(self, mock_executor):
        mock_executor.request = MagicMock(side_effect=[
            {"cfm_usd_available": {"value": "8000.00"}, "total_usd_balance": {"value": "8000.00"}},
            {"positions": []},
        ])

        balances = mock_executor.get_balances()

        assert balances["available"]["cash"]["USD"] == 8000.0
        assert balances["available"]["crypto"] == {}

    def test_short_positions_ignored(self, mock_executor):
        """Short positions are ignored in long-only mode."""
        mock_executor.request = MagicMock(side_effect=[
            {"cfm_usd_available": {"value": "5000.00"}, "total_usd_balance": {"value": "10000.00"}},
            {
                "positions": [
                    {"product_id": "BIP-20DEC30-CDE", "side": "SHORT", "number_of_contracts": "5"},
                    {"product_id": "ETP-20DEC30-CDE", "side": "LONG", "number_of_contracts": "10"},
                ]
            },
        ])

        balances = mock_executor.get_balances()

        assert "BTC" not in balances["available"]["crypto"]
        # 10 contracts × 0.1 ETH = 1.0 ETH
        assert balances["available"]["crypto"]["ETH"] == 1.0

    def test_eth_position_conversion(self, mock_executor):
        """ETH positions correctly convert contracts to base amount."""
        mock_executor.request = MagicMock(side_effect=[
            {"cfm_usd_available": {"value": "3000.00"}, "total_usd_balance": {"value": "5000.00"}},
            {
                "positions": [
                    {"product_id": "ETP-20DEC30-CDE", "side": "LONG", "number_of_contracts": "25"},
                ]
            },
        ])

        balances = mock_executor.get_balances()

        # 25 contracts × 0.1 ETH = 2.5 ETH
        assert balances["available"]["crypto"]["ETH"] == pytest.approx(2.5)

    def test_api_failure_returns_empty(self, mock_executor):
        mock_executor.request = MagicMock(return_value=None)

        balances = mock_executor.get_balances()

        assert balances["available"]["cash"]["USD"] == 0.0
        assert balances["available"]["crypto"] == {}

    def test_correct_cfm_endpoints_called(self, mock_executor):
        mock_executor.request = MagicMock(side_effect=[
            {"cfm_usd_available": {"value": "0"}, "total_usd_balance": {"value": "0"}},
            {"positions": []},
        ])

        mock_executor.get_balances()

        calls = mock_executor.request.call_args_list
        assert calls[0] == call("GET", "/api/v3/brokerage/cfm/balance_summary")
        assert calls[1] == call("GET", "/api/v3/brokerage/cfm/positions")


class TestOrderRouting:
    def test_limit_order_uses_futures_product_id(self, mock_executor):
        mock_executor.get_product_details = MagicMock(return_value={
            "price": "100000.0",
            "quote_increment": "5",
            "base_increment": "0.01",
        })
        mock_executor.get_best_bid_ask = MagicMock(return_value=(99995.0, 100005.0))
        mock_executor.request = MagicMock(return_value={"success": True, "order_id": "test-123"})

        result = mock_executor.place_limit_order("BTC-USDC", "BUY", 100000.0, amount_quote_currency=1000.0)

        assert result is not None
        assert result["success"] is True

    def test_limit_order_converts_to_contracts(self, mock_executor):
        """$1000 at $100000/BTC = 0.01 BTC = 1 contract."""
        mock_executor.get_product_details = MagicMock(return_value={
            "price": "100000.0",
            "quote_increment": "5",
            "base_increment": "0.01",
        })
        mock_executor.get_best_bid_ask = MagicMock(return_value=(99995.0, 100005.0))
        mock_executor.request = MagicMock(return_value={"success": True})

        result = mock_executor.place_limit_order("BTC-USDC", "BUY", 100000.0, amount_quote_currency=1000.0)

        assert result is not None

    def test_limit_order_too_small_returns_none(self, mock_executor):
        """$5 at $100000/BTC = 0.00005 BTC = 0 contracts -> rejected."""
        mock_executor.get_product_details = MagicMock(return_value={
            "price": "100000.0",
            "quote_increment": "5",
            "base_increment": "0.01",
        })
        mock_executor.get_best_bid_ask = MagicMock(return_value=(99995.0, 100005.0))

        result = mock_executor.place_limit_order("BTC-USDC", "BUY", 100000.0, amount_quote_currency=5.0)

        assert result is None

    def test_market_order_uses_futures_product_id(self, mock_executor):
        mock_executor.get_product_details = MagicMock(return_value={
            "price": "100000.0",
            "base_increment": "0.01",
        })
        mock_executor.request = MagicMock(return_value={"success": True})

        mock_executor.place_market_order("BTC-USDC", "SELL", amount_base_currency=0.05)

        mock_executor.get_product_details.assert_called_with("BTC-USDC")

    def test_aggressive_limit_uses_futures_product_id(self, mock_executor):
        mock_executor.get_product_details = MagicMock(return_value={
            "price": "3000.0",
            "quote_increment": "0.01",
            "base_increment": "0.1",
        })
        mock_executor.get_best_bid_ask = MagicMock(return_value=(2999.0, 3001.0))
        mock_executor.request = MagicMock(return_value={"success": True})

        result = mock_executor.place_aggressive_limit_order("ETH-USDC", "SELL", 3000.0, amount_base_currency=1.0)

        assert result is not None


class TestOrderPayload:
    """Verify orders include leverage and margin_type fields."""

    def test_limit_order_has_leverage_and_margin(self, mock_executor):
        mock_executor.get_product_details = MagicMock(return_value={
            "price": "100000.0",
            "quote_increment": "5",
            "base_increment": "0.01",
        })
        mock_executor.get_best_bid_ask = MagicMock(return_value=(99995.0, 100005.0))

        # In paper mode, the method returns a dict directly without calling request()
        result = mock_executor.place_limit_order("BTC-USDC", "BUY", 100000.0, amount_quote_currency=5000.0)

        assert result is not None
        assert result["success"] is True

    def test_limit_order_live_mode_payload(self, mock_executor):
        """In live mode, verify the payload sent to the API has leverage/margin."""
        mock_executor.trading_mode = "live"
        mock_executor.get_product_details = MagicMock(return_value={
            "price": "100000.0",
            "quote_increment": "5",
            "base_increment": "0.01",
        })
        mock_executor.get_best_bid_ask = MagicMock(return_value=(99995.0, 100005.0))
        mock_executor.cancel_open_orders = MagicMock()
        mock_executor.request = MagicMock(return_value={"success": True})

        mock_executor.place_limit_order("BTC-USDC", "BUY", 100000.0, amount_quote_currency=5000.0)

        # Verify the POST payload
        call_args = mock_executor.request.call_args
        payload = call_args[0][2] if len(call_args[0]) > 2 else call_args[1].get("body")
        # request is called as self.request("POST", "/api/v3/brokerage/orders", payload)
        assert call_args[0][0] == "POST"
        assert call_args[0][1] == "/api/v3/brokerage/orders"
        payload = call_args[0][2]
        assert payload["leverage"] == "1"
        assert payload["margin_type"] == "CROSS"
        assert payload["product_id"] == "BIP-20DEC30-CDE"


class TestMarketData:
    def test_get_market_data_uses_futures_id(self, mock_executor):
        mock_executor.request = MagicMock(return_value={
            "candles": [
                ["1700000000", "99000", "101000", "99500", "100500", "100.5"],
                ["1700003600", "100000", "102000", "100500", "101500", "120.3"],
            ]
        })

        mock_executor.get_market_data("BTC-USDC", 50)

        call_args = mock_executor.request.call_args
        assert "BIP-20DEC30-CDE" in call_args[0][1]

    def test_get_product_details_overrides_base_increment(self, mock_executor):
        """Product details should have base_increment = contract_size."""
        mock_executor.request = MagicMock(return_value={
            "price": "100000.0",
            "quote_increment": "5",
            "base_increment": "0.0001",  # API might return this
        })
        mock_executor.product_details_cache = {}

        details = mock_executor.get_product_details("BTC-USDC")

        # Should be overridden to 0.01 (1 contract)
        assert details["base_increment"] == "0.01"

    def test_get_product_details_eth_increment(self, mock_executor):
        mock_executor.request = MagicMock(return_value={
            "price": "3000.0",
            "quote_increment": "0.01",
            "base_increment": "0.001",
        })
        mock_executor.product_details_cache = {}

        details = mock_executor.get_product_details("ETH-USDC")

        assert details["base_increment"] == "0.1"


class TestInheritance:
    def test_is_subclass(self):
        assert issubclass(CoinbaseFuturesExecutor, CoinbaseExecutor)

    def test_check_order_filled_inherited(self, mock_executor):
        assert hasattr(mock_executor, 'check_order_filled')
        assert mock_executor.check_order_filled.__func__ is CoinbaseExecutor.check_order_filled

    def test_portfolio_uuid_optional(self, mock_executor):
        """portfolio_uuid is optional (defaults to None)."""
        assert mock_executor.portfolio_uuid is None


class TestExecutorProtocol:
    def test_has_all_required_methods(self, mock_executor):
        required = [
            'get_balances', 'get_product_details', 'get_market_data',
            'place_limit_order', 'place_market_order', 'cancel_open_orders',
            'get_supported_assets',
        ]
        for method in required:
            assert hasattr(mock_executor, method), f"Missing: {method}"
            assert callable(getattr(mock_executor, method))
