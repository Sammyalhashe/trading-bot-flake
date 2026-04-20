import pytest
from unittest.mock import MagicMock, patch
import sys
import os

# Add parent dir to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from executors.coinbase_executor import CoinbaseExecutor


def _acct(currency, available, hold="0"):
    """Helper to build a mock Coinbase account entry."""
    return {
        "currency": currency,
        "available_balance": {"value": available},
        "hold": {"value": hold},
    }


class TestCoinbaseExecutorDustFiltering:
    """Test that dust balances are properly filtered from get_balances()."""

    @pytest.fixture
    def mock_executor(self):
        """Create a CoinbaseExecutor with mocked API calls."""
        with patch.object(CoinbaseExecutor, '__init__', lambda self, *a, **k: None):
            executor = CoinbaseExecutor.__new__(CoinbaseExecutor)
            executor.product_details_cache = {}
            executor.portfolio_uuid = None
            return executor

    def test_dust_balances_filtered_out(self, mock_executor):
        """Balances worth less than $5 should be filtered out."""
        mock_executor.request = MagicMock(return_value={
            "accounts": [
                _acct("USDC", "1000.0"),
                _acct("BTC", "0.1"),       # $10k+ worth
                _acct("SHIB", "1000.0"),   # Only $1 worth (dust)
                _acct("DOGE", "0.5"),      # Only $0.10 worth (dust)
            ],
            "has_next": False
        })

        def mock_get_product_details(product_id):
            prices = {
                "BTC-USDC": {"price": "100000.0"},
                "SHIB-USDC": {"price": "0.001"},
                "DOGE-USDC": {"price": "0.20"},
            }
            return prices.get(product_id)

        mock_executor.get_product_details = mock_get_product_details

        balances = mock_executor.get_balances()

        assert balances["available"]["cash"]["USDC"] == 1000.0, "USDC should be in cash"
        assert "BTC" in balances["available"]["crypto"], "BTC worth $10k+ should be included"
        assert "SHIB" not in balances["available"]["crypto"], "SHIB worth $1 should be filtered as dust"
        assert "DOGE" not in balances["available"]["crypto"], "DOGE worth $0.10 should be filtered as dust"

    def test_eth_always_included(self, mock_executor):
        """ETH/WETH should always be included regardless of value (needed for gas)."""
        mock_executor.request = MagicMock(return_value={
            "accounts": [
                _acct("USDC", "1000.0"),
                _acct("ETH", "0.001"),   # Only $3 worth
                _acct("WETH", "0.002"),  # Only $6 worth
            ],
            "has_next": False
        })

        def mock_get_product_details(product_id):
            return {"price": "3000.0"} if "ETH" in product_id else None

        mock_executor.get_product_details = mock_get_product_details

        balances = mock_executor.get_balances()

        assert "ETH" in balances["available"]["crypto"], "ETH should always be included (gas token)"
        assert "WETH" in balances["available"]["crypto"], "WETH should always be included (gas token)"
        assert balances["available"]["crypto"]["ETH"] == 0.001
        assert balances["available"]["crypto"]["WETH"] == 0.002

    def test_significant_balances_included(self, mock_executor):
        """Balances worth $5 or more should be included."""
        mock_executor.request = MagicMock(return_value={
            "accounts": [
                _acct("USDC", "100.0"),
                _acct("BTC", "0.0001"),  # Exactly $5 worth
                _acct("SOL", "0.5"),     # $100 worth
            ],
            "has_next": False
        })

        def mock_get_product_details(product_id):
            prices = {
                "BTC-USDC": {"price": "50000.0"},
                "SOL-USDC": {"price": "200.0"},
            }
            return prices.get(product_id)

        mock_executor.get_product_details = mock_get_product_details

        balances = mock_executor.get_balances()

        assert "BTC" in balances["available"]["crypto"], "BTC worth exactly $5 should be included"
        assert "SOL" in balances["available"]["crypto"], "SOL worth $100 should be included"
        assert balances["available"]["crypto"]["BTC"] == 0.0001
        assert balances["available"]["crypto"]["SOL"] == 0.5

    def test_dust_threshold_constant(self, mock_executor):
        """Verify the dust threshold is set to $5."""
        import inspect
        source = inspect.getsource(CoinbaseExecutor.get_balances)
        assert "DUST_THRESHOLD_USD = 5.0" in source, "Dust threshold should be $5"

    def test_position_count_excludes_dust(self, mock_executor):
        """Integration test: verify position counting ignores dust."""
        accounts = [_acct("USDC", "10000.0")]

        dust_tokens = ["SHIB", "PEPE", "FLOKI", "WOJAK", "TURBO", "MEME", "DOGE",
                       "SAMO", "BONK", "WIF", "POPCAT", "MOG", "Brett", "TOSHI",
                       "MOCHI", "PONKE", "ANALOS", "MANEKI", "MYRO", "WEN", "PONZI", "SCAM"]
        for token in dust_tokens:
            accounts.append(_acct(token, "100.0"))

        accounts.append(_acct("BTC", "0.01"))
        accounts.append(_acct("ETH", "0.5"))

        mock_executor.request = MagicMock(return_value={
            "accounts": accounts,
            "has_next": False
        })

        def mock_get_product_details(product_id):
            if any(t in product_id for t in dust_tokens):
                return {"price": "0.0001"}
            if "BTC" in product_id:
                return {"price": "50000.0"}
            if "ETH" in product_id:
                return {"price": "3000.0"}
            return None

        mock_executor.get_product_details = mock_get_product_details

        balances = mock_executor.get_balances()
        crypto = balances["available"]["crypto"]

        positions = sum(1 for a in crypto if a not in ("USD", "USDC") and crypto[a] > 0)

        assert positions == 2, f"Expected 2 positions, got {positions}. Dust should be filtered."
        assert "BTC" in crypto
        assert "ETH" in crypto
        assert "SHIB" not in crypto
        assert "PEPE" not in crypto

    def test_missing_product_details_skips_token(self, mock_executor):
        """Tokens without product details should be skipped (except ETH/WETH)."""
        mock_executor.request = MagicMock(return_value={
            "accounts": [
                _acct("USDC", "1000.0"),
                _acct("UNKNOWN", "100.0"),
            ],
            "has_next": False
        })

        mock_executor.get_product_details = MagicMock(return_value=None)

        balances = mock_executor.get_balances()

        assert "UNKNOWN" not in balances["available"]["crypto"], "Tokens without product details should be skipped"

    def test_pagination_handled(self, mock_executor):
        """Verify pagination works with dust filtering."""
        page1_response = {
            "accounts": [
                _acct("USDC", "1000.0"),
                _acct("BTC", "0.1"),
            ],
            "has_next": True,
            "cursor": "page2-cursor"
        }

        page2_response = {
            "accounts": [
                _acct("DUST", "1000.0"),  # $1 worth
                _acct("ETH", "1.0"),      # $3000 worth
            ],
            "has_next": False
        }

        mock_executor.request = MagicMock(side_effect=[page1_response, page2_response])

        def mock_get_product_details(product_id):
            prices = {
                "BTC-USDC": {"price": "50000.0"},
                "DUST-USDC": {"price": "0.001"},
                "ETH-USDC": {"price": "3000.0"},
            }
            return prices.get(product_id)

        mock_executor.get_product_details = mock_get_product_details

        balances = mock_executor.get_balances()

        assert mock_executor.request.call_count == 2
        assert "BTC" in balances["available"]["crypto"]
        assert "ETH" in balances["available"]["crypto"]
        assert "DUST" not in balances["available"]["crypto"]


class TestPortfolioFiltering:
    """Test that portfolio_uuid scopes balances and orders."""

    @pytest.fixture
    def portfolio_executor(self):
        """Create a CoinbaseExecutor with a portfolio_uuid set."""
        with patch.object(CoinbaseExecutor, '__init__', lambda self, *a, **k: None):
            executor = CoinbaseExecutor.__new__(CoinbaseExecutor)
            executor.product_details_cache = {}
            executor.portfolio_uuid = "test-portfolio-uuid-123"
            executor.trading_mode = "paper"
            return executor

    @pytest.fixture
    def no_portfolio_executor(self):
        """Create a CoinbaseExecutor without a portfolio_uuid."""
        with patch.object(CoinbaseExecutor, '__init__', lambda self, *a, **k: None):
            executor = CoinbaseExecutor.__new__(CoinbaseExecutor)
            executor.product_details_cache = {}
            executor.portfolio_uuid = None
            executor.trading_mode = "paper"
            return executor

    def test_get_balances_includes_portfolio_filter(self, portfolio_executor):
        """When portfolio_uuid is set, accounts query includes retail_portfolio_id."""
        portfolio_executor.request = MagicMock(return_value={
            "accounts": [_acct("USDC", "1000.0")],
            "has_next": False,
        })

        portfolio_executor.get_balances()

        call_path = portfolio_executor.request.call_args[0][1]
        assert "retail_portfolio_id=test-portfolio-uuid-123" in call_path

    def test_get_balances_no_filter_without_portfolio(self, no_portfolio_executor):
        """When portfolio_uuid is None, accounts query has no retail_portfolio_id."""
        no_portfolio_executor.request = MagicMock(return_value={
            "accounts": [_acct("USDC", "1000.0")],
            "has_next": False,
        })

        no_portfolio_executor.get_balances()

        call_path = no_portfolio_executor.request.call_args[0][1]
        assert "retail_portfolio_id" not in call_path

    def test_limit_order_includes_portfolio_id(self, portfolio_executor):
        """Limit orders include retail_portfolio_id when portfolio is set."""
        portfolio_executor.get_product_details = MagicMock(return_value={
            "price": "100000.0",
            "quote_increment": "0.01",
            "base_increment": "0.0001",
        })
        portfolio_executor.get_best_bid_ask = MagicMock(return_value=(99999.0, 100001.0))

        # Paper mode returns dict directly — check it doesn't crash
        result = portfolio_executor.place_limit_order("BTC-USDC", "BUY", 100000.0, amount_quote_currency=1000.0)
        assert result is not None

    def test_limit_order_live_payload_has_portfolio(self, portfolio_executor):
        """In live mode, order payload includes retail_portfolio_id."""
        portfolio_executor.trading_mode = "live"
        portfolio_executor.get_product_details = MagicMock(return_value={
            "price": "100000.0",
            "quote_increment": "0.01",
            "base_increment": "0.0001",
        })
        portfolio_executor.get_best_bid_ask = MagicMock(return_value=(99999.0, 100001.0))
        portfolio_executor.cancel_open_orders = MagicMock()
        portfolio_executor.request = MagicMock(return_value={"success": True})

        portfolio_executor.place_limit_order("BTC-USDC", "BUY", 100000.0, amount_quote_currency=1000.0)

        payload = portfolio_executor.request.call_args[0][2]
        assert payload["retail_portfolio_id"] == "test-portfolio-uuid-123"

    def test_no_portfolio_id_in_order_when_none(self, no_portfolio_executor):
        """Orders should NOT include retail_portfolio_id when portfolio is None."""
        no_portfolio_executor.trading_mode = "live"
        no_portfolio_executor.get_product_details = MagicMock(return_value={
            "price": "100000.0",
            "quote_increment": "0.01",
            "base_increment": "0.0001",
        })
        no_portfolio_executor.get_best_bid_ask = MagicMock(return_value=(99999.0, 100001.0))
        no_portfolio_executor.cancel_open_orders = MagicMock()
        no_portfolio_executor.request = MagicMock(return_value={"success": True})

        no_portfolio_executor.place_limit_order("BTC-USDC", "BUY", 100000.0, amount_quote_currency=1000.0)

        payload = no_portfolio_executor.request.call_args[0][2]
        assert "retail_portfolio_id" not in payload

    def test_market_order_includes_portfolio_id(self, portfolio_executor):
        """Market orders include retail_portfolio_id when portfolio is set."""
        portfolio_executor.trading_mode = "live"
        portfolio_executor.get_product_details = MagicMock(return_value={
            "price": "100000.0",
            "base_increment": "0.0001",
        })
        portfolio_executor.cancel_open_orders = MagicMock()
        portfolio_executor.request = MagicMock(return_value={"success": True})

        portfolio_executor.place_market_order("BTC-USDC", "SELL", amount_base_currency=0.01)

        payload = portfolio_executor.request.call_args[0][2]
        assert payload["retail_portfolio_id"] == "test-portfolio-uuid-123"

    def test_aggressive_limit_includes_portfolio_id(self, portfolio_executor):
        """Aggressive limit orders include retail_portfolio_id."""
        portfolio_executor.trading_mode = "live"
        portfolio_executor.get_product_details = MagicMock(return_value={
            "price": "100000.0",
            "quote_increment": "0.01",
            "base_increment": "0.0001",
        })
        portfolio_executor.get_best_bid_ask = MagicMock(return_value=(99999.0, 100001.0))
        portfolio_executor.cancel_open_orders = MagicMock()
        portfolio_executor.request = MagicMock(return_value={"success": True})

        portfolio_executor.place_aggressive_limit_order("BTC-USDC", "SELL", 100000.0, amount_base_currency=0.01)

        payload = portfolio_executor.request.call_args[0][2]
        assert payload["retail_portfolio_id"] == "test-portfolio-uuid-123"
