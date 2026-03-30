import pytest
from unittest.mock import MagicMock, patch
import sys
import os

# Add parent dir to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from executors.coinbase_executor import CoinbaseExecutor


class TestCoinbaseExecutorDustFiltering:
    """Test that dust balances are properly filtered from get_balances()."""

    @pytest.fixture
    def mock_executor(self):
        """Create a CoinbaseExecutor with mocked API calls."""
        with patch.object(CoinbaseExecutor, '__init__', lambda self, *a, **k: None):
            executor = CoinbaseExecutor.__new__(CoinbaseExecutor)
            executor.product_details_cache = {}
            return executor

    def test_dust_balances_filtered_out(self, mock_executor):
        """Balances worth less than $5 should be filtered out."""
        # Mock API response with mix of real and dust positions
        mock_executor.request = MagicMock(return_value={
            "accounts": [
                {"currency": "USDC", "available_balance": {"value": "1000.0"}},
                {"currency": "BTC", "available_balance": {"value": "0.1"}},  # $10k+ worth
                {"currency": "SHIB", "available_balance": {"value": "1000.0"}},  # Only $1 worth (dust)
                {"currency": "DOGE", "available_balance": {"value": "0.5"}},  # Only $0.10 worth (dust)
            ],
            "has_next": False
        })

        # Mock product details for pricing
        def mock_get_product_details(product_id):
            prices = {
                "BTC-USDC": {"price": "100000.0"},
                "SHIB-USDC": {"price": "0.001"},  # 1000 SHIB = $1
                "DOGE-USDC": {"price": "0.20"},   # 0.5 DOGE = $0.10
            }
            return prices.get(product_id)

        mock_executor.get_product_details = mock_get_product_details

        # Get balances
        balances = mock_executor.get_balances()

        # Assertions
        assert balances["cash"]["USDC"] == 1000.0, "USDC should be in cash"
        assert "BTC" in balances["crypto"], "BTC worth $10k+ should be included"
        assert "SHIB" not in balances["crypto"], "SHIB worth $1 should be filtered as dust"
        assert "DOGE" not in balances["crypto"], "DOGE worth $0.10 should be filtered as dust"

    def test_eth_always_included(self, mock_executor):
        """ETH/WETH should always be included regardless of value (needed for gas)."""
        mock_executor.request = MagicMock(return_value={
            "accounts": [
                {"currency": "USDC", "available_balance": {"value": "1000.0"}},
                {"currency": "ETH", "available_balance": {"value": "0.001"}},  # Only $3 worth
                {"currency": "WETH", "available_balance": {"value": "0.002"}},  # Only $6 worth
            ],
            "has_next": False
        })

        def mock_get_product_details(product_id):
            return {"price": "3000.0"} if "ETH" in product_id else None

        mock_executor.get_product_details = mock_get_product_details

        balances = mock_executor.get_balances()

        # ETH and WETH should be included even though worth < $5
        assert "ETH" in balances["crypto"], "ETH should always be included (gas token)"
        assert "WETH" in balances["crypto"], "WETH should always be included (gas token)"
        assert balances["crypto"]["ETH"] == 0.001
        assert balances["crypto"]["WETH"] == 0.002

    def test_significant_balances_included(self, mock_executor):
        """Balances worth $5 or more should be included."""
        mock_executor.request = MagicMock(return_value={
            "accounts": [
                {"currency": "USDC", "available_balance": {"value": "100.0"}},
                {"currency": "BTC", "available_balance": {"value": "0.0001"}},  # Exactly $5 worth
                {"currency": "SOL", "available_balance": {"value": "0.5"}},  # $100 worth
            ],
            "has_next": False
        })

        def mock_get_product_details(product_id):
            prices = {
                "BTC-USDC": {"price": "50000.0"},  # 0.0001 BTC = $5
                "SOL-USDC": {"price": "200.0"},    # 0.5 SOL = $100
            }
            return prices.get(product_id)

        mock_executor.get_product_details = mock_get_product_details

        balances = mock_executor.get_balances()

        assert "BTC" in balances["crypto"], "BTC worth exactly $5 should be included"
        assert "SOL" in balances["crypto"], "SOL worth $100 should be included"
        assert balances["crypto"]["BTC"] == 0.0001
        assert balances["crypto"]["SOL"] == 0.5

    def test_dust_threshold_constant(self, mock_executor):
        """Verify the dust threshold is set to $5."""
        # This test documents the threshold value
        # If we change it, this test will remind us to update docs
        import inspect
        source = inspect.getsource(CoinbaseExecutor.get_balances)
        assert "DUST_THRESHOLD_USD = 5.0" in source, "Dust threshold should be $5"

    def test_position_count_excludes_dust(self, mock_executor):
        """Integration test: verify position counting ignores dust."""
        # Simulate 22 dust positions + 2 real positions
        accounts = [{"currency": "USDC", "available_balance": {"value": "10000.0"}}]

        # Add 22 dust positions (old trades, leftovers, etc.)
        dust_tokens = ["SHIB", "PEPE", "FLOKI", "WOJAK", "TURBO", "MEME", "DOGE",
                       "SAMO", "BONK", "WIF", "POPCAT", "MOG", "Brett", "TOSHI",
                       "MOCHI", "PONKE", "ANALOS", "MANEKI", "MYRO", "WEN", "PONZI", "SCAM"]
        for token in dust_tokens:
            accounts.append({"currency": token, "available_balance": {"value": "100.0"}})

        # Add 2 real positions
        accounts.append({"currency": "BTC", "available_balance": {"value": "0.01"}})
        accounts.append({"currency": "ETH", "available_balance": {"value": "0.5"}})

        mock_executor.request = MagicMock(return_value={
            "accounts": accounts,
            "has_next": False
        })

        def mock_get_product_details(product_id):
            # Dust tokens worth $0.01 each
            if any(t in product_id for t in dust_tokens):
                return {"price": "0.0001"}  # 100 tokens = $0.01
            # Real positions
            if "BTC" in product_id:
                return {"price": "50000.0"}  # 0.01 BTC = $500
            if "ETH" in product_id:
                return {"price": "3000.0"}   # 0.5 ETH = $1500
            return None

        mock_executor.get_product_details = mock_get_product_details

        balances = mock_executor.get_balances()

        # Count positions (excluding USDC)
        positions = sum(1 for a in balances["crypto"] if a not in ("USD", "USDC") and balances["crypto"][a] > 0)

        # Should only count 2 real positions, not 22 dust
        assert positions == 2, f"Expected 2 positions, got {positions}. Dust should be filtered."
        assert "BTC" in balances["crypto"]
        assert "ETH" in balances["crypto"]
        # Verify dust is NOT in balances
        assert "SHIB" not in balances["crypto"]
        assert "PEPE" not in balances["crypto"]

    def test_missing_product_details_skips_token(self, mock_executor):
        """Tokens without product details should be skipped (except ETH/WETH)."""
        mock_executor.request = MagicMock(return_value={
            "accounts": [
                {"currency": "USDC", "available_balance": {"value": "1000.0"}},
                {"currency": "UNKNOWN", "available_balance": {"value": "100.0"}},
            ],
            "has_next": False
        })

        mock_executor.get_product_details = MagicMock(return_value=None)  # No product details

        balances = mock_executor.get_balances()

        assert "UNKNOWN" not in balances["crypto"], "Tokens without product details should be skipped"

    def test_pagination_handled(self, mock_executor):
        """Verify pagination works with dust filtering."""
        # First page
        page1_response = {
            "accounts": [
                {"currency": "USDC", "available_balance": {"value": "1000.0"}},
                {"currency": "BTC", "available_balance": {"value": "0.1"}},
            ],
            "has_next": True,
            "cursor": "page2-cursor"
        }

        # Second page
        page2_response = {
            "accounts": [
                {"currency": "DUST", "available_balance": {"value": "1000.0"}},  # $1 worth
                {"currency": "ETH", "available_balance": {"value": "1.0"}},  # $3000 worth
            ],
            "has_next": False
        }

        mock_executor.request = MagicMock(side_effect=[page1_response, page2_response])

        def mock_get_product_details(product_id):
            prices = {
                "BTC-USDC": {"price": "50000.0"},
                "DUST-USDC": {"price": "0.001"},  # 1000 DUST = $1
                "ETH-USDC": {"price": "3000.0"},
            }
            return prices.get(product_id)

        mock_executor.get_product_details = mock_get_product_details

        balances = mock_executor.get_balances()

        # Verify both pages were processed
        assert mock_executor.request.call_count == 2
        # Verify filtering worked across pages
        assert "BTC" in balances["crypto"]
        assert "ETH" in balances["crypto"]
        assert "DUST" not in balances["crypto"]
