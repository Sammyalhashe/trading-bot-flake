import pytest
from unittest.mock import MagicMock, patch
import json
import os
import sys
import time

# Add parent dir to path to import trading_bot
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trading_bot

@pytest.fixture
def mock_coinbase_request():
    with patch('trading_bot.coinbase_request') as mock:
        yield mock

@pytest.fixture
def mock_state():
    with patch('trading_bot.load_state') as mock_load, \
         patch('trading_bot.save_state') as mock_save, \
         patch('trading_bot.clear_entry_price') as mock_clear:
        mock_load.return_value = {"entry_prices": {}}
        yield mock_load, mock_save, mock_clear

def test_stop_loss_trigger(mock_coinbase_request, mock_state):
    """Test that stop loss triggers a sell when price drops."""
    # Force LIVE mode for test to ensure API is called
    with patch('trading_bot.TRADING_MODE', 'live'):
        mock_load, mock_save, mock_clear = mock_state
        
        # Setup State: Bought ETH at $3000
        mock_load.return_value = {"entry_prices": {"ETH-USDC": 3000.0}}
        
        def side_effect(method, path, body=None):
            if "accounts" in path:
                return {"accounts": [{"currency": "ETH", "available_balance": {"value": "1.0"}}, {"currency": "USDC", "available_balance": {"value": "1000.0"}}]}
            if "products/BTC-USDC/candles" in path:
                return {"candles": [[time.time()-i*3600, 100, 100, 100, 100, 100] for i in range(60)]}
            if "products/BTC-USDC" in path:
                return {"price": "100.0", "quote_increment": "0.01"}
            if "products/ETH-USDC/candles" in path:
                 return {"candles": [[time.time()-i*3600, 2800, 2800, 2800, 2800, 100] for i in range(60)]}
            if "products/ETH-USDC" in path: 
                return {"price": "2800.0", "quote_increment": "0.01", "base_increment": "0.001"}
            if "orders" in path and method == "POST":
                return {"id": "order_123"}
            return {}

        mock_coinbase_request.side_effect = side_effect

        trading_bot.run_bot()

        calls = mock_coinbase_request.mock_calls
        sell_calls = [c for c in calls if "orders" in str(c) and "'side': 'SELL'" in str(c) and "ETH-USDC" in str(c)]
        assert len(sell_calls) > 0, "Stop loss did not trigger sell order"

def test_bear_market_filter(mock_coinbase_request, mock_state):
    """Test that we DO NOT buy alts when BTC is bearish."""
    with patch('trading_bot.TRADING_MODE', 'live'):
        mock_load, mock_save, mock_clear = mock_state
        mock_load.return_value = {"entry_prices": {}}

        def side_effect(method, path, body=None):
            if "accounts" in path:
                return {"accounts": [{"currency": "USDC", "available_balance": {"value": "1000.0"}}]}
            if "products/BTC-USDC/candles" in path:
                # Bearish
                return {"candles": [[time.time()-i*3600, 50+i, 50+i, 50+i, 50+i, 100] for i in range(60)]}
            if "products/ETH-USDC/candles" in path:
                # Bullish signal for ETH
                return {"candles": [[time.time()-i*3600, 100-i, 100-i, 100-i, 100-i, 100] for i in range(60)]}
            if "products/BTC-USDC" in path: return {"price": "50.0"}
            if "products/ETH-USDC" in path: return {"price": "100.0", "quote_increment": "0.01", "base_increment": "0.001"}
            if "orders" in path: return {"id": "123"}
            return {}

        mock_coinbase_request.side_effect = side_effect

        trading_bot.run_bot()

        # Assert NO BUY order for ETH
        calls = mock_coinbase_request.mock_calls
        buy_calls = [c for c in calls if "orders" in str(c) and "'side': 'BUY'" in str(c) and "ETH-USDC" in str(c)]
        assert len(buy_calls) == 0, "Bot bought ETH despite BTC bearish trend!"
