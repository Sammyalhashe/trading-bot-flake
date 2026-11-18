import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from executors.paper_executor import PaperLedgerExecutor  # noqa: E402

FEE = 0.001


@pytest.fixture
def real():
    executor = MagicMock()
    executor.get_product_details.return_value = {"price": "100.0"}
    return executor


@pytest.fixture
def paper(real, tmp_path):
    return PaperLedgerExecutor(real, tmp_path / "ledger.json", starting_cash=1000.0, fee_per_side=FEE)


def _fill(paper, result):
    return paper.check_order_filled(result["success_response"]["order_id"])


class TestLedger:
    def test_buy_debits_cash_with_fee_and_credits_holdings(self, paper):
        result = paper.place_limit_order("BTC-USDC", "BUY", 100.0, amount_quote_currency=500.0)
        assert _fill(paper, result) == {"price": 100.0, "fee": pytest.approx(0.5)}
        bal = paper.get_balances()
        assert bal["available"]["cash"]["USDC"] == pytest.approx(1000.0 - 500.0 - 0.5)
        assert bal["total"]["crypto"]["BTC"] == pytest.approx(5.0)

    def test_sell_credits_proceeds_minus_fee_capped_at_holdings(self, paper):
        paper.place_limit_order("BTC-USDC", "BUY", 100.0, amount_quote_currency=500.0)
        result = paper.place_aggressive_limit_order("BTC-USDC", "SELL", 110.0, amount_base_currency=99.0)
        assert _fill(paper, result)["fee"] == pytest.approx(5.0 * 110.0 * FEE)
        bal = paper.get_balances()
        assert "BTC" not in bal["total"]["crypto"]
        assert bal["available"]["cash"]["USDC"] == pytest.approx(499.5 + 550.0 - 0.55)

    def test_buy_is_scaled_to_available_cash(self, paper):
        paper.place_limit_order("BTC-USDC", "BUY", 100.0, amount_quote_currency=5000.0)
        assert paper.get_balances()["available"]["cash"]["USDC"] == pytest.approx(0.0, abs=1e-6)

    def test_buy_without_cash_is_rejected(self, paper):
        paper.place_limit_order("BTC-USDC", "BUY", 100.0, amount_quote_currency=5000.0)
        assert paper.place_limit_order("BTC-USDC", "BUY", 100.0, amount_quote_currency=10.0)["success"] is False

    def test_market_order_fills_at_current_price(self, paper, real):
        real.get_product_details.return_value = {"price": "120.0"}
        result = paper.place_market_order("BTC-USDC", "BUY", amount_quote_currency=120.0)
        assert _fill(paper, result)["price"] == 120.0

    def test_ledger_survives_restart(self, paper, real, tmp_path):
        paper.place_limit_order("BTC-USDC", "BUY", 100.0, amount_quote_currency=500.0)
        reloaded = PaperLedgerExecutor(real, tmp_path / "ledger.json", starting_cash=999999.0, fee_per_side=FEE)
        assert reloaded.get_balances()["total"]["crypto"]["BTC"] == pytest.approx(5.0)

    def test_never_touches_real_order_endpoints(self, paper, real):
        paper.place_limit_order("BTC-USDC", "BUY", 100.0, amount_quote_currency=100.0)
        paper.place_market_order("BTC-USDC", "SELL", amount_base_currency=0.5)
        paper.place_aggressive_limit_order("BTC-USDC", "SELL", 100.0, amount_base_currency=0.5)
        paper.cancel_open_orders("BTC-USDC")
        for name in ("place_limit_order", "place_market_order", "place_aggressive_limit_order",
                     "cancel_open_orders", "get_balances", "request"):
            getattr(real, name).assert_not_called()

    def test_fee_rate_exposed_for_pnl(self, paper):
        assert paper.round_trip_fee_pct == Decimal(str(2 * FEE))

    def test_market_data_delegates_to_real(self, paper, real):
        paper.get_market_data("BTC-USDC", 60, granularity="1d")
        real.get_market_data.assert_called_once_with("BTC-USDC", 60, granularity="1d")


class TestWiring:
    def test_factory_wraps_in_paper_mode_with_starting_cash(self, monkeypatch, tmp_path):
        import trading_bot
        monkeypatch.setenv("PAPER_STARTING_CASH", "5000")
        with patch.object(trading_bot, "TRADING_MODE", "paper"), patch.object(trading_bot, "STATE_FILE", tmp_path / "s.json"), \
             patch.object(trading_bot, "CoinbaseExecutor") as cb:
            executor = trading_bot.make_coinbase_executor()
        assert isinstance(executor, PaperLedgerExecutor)
        assert executor.get_balances()["available"]["cash"]["USDC"] == 5000.0
        cb.return_value.get_balances.assert_not_called()

    def test_factory_returns_real_executor_in_live_mode(self, monkeypatch):
        import trading_bot
        monkeypatch.setenv("PAPER_STARTING_CASH", "5000")
        with patch.object(trading_bot, "TRADING_MODE", "live"), patch.object(trading_bot, "CoinbaseExecutor") as cb:
            assert trading_bot.make_coinbase_executor() is cb.return_value

    def test_notify_prefix_marks_messages(self, monkeypatch):
        from core import telegram
        monkeypatch.setenv("NOTIFY_PREFIX", "PAPER")
        with patch.object(telegram, "_get_token", return_value="T"), patch.object(telegram.requests, "post") as post:
            telegram.send_telegram_message("Bought BTC")
        assert post.call_args.kwargs["json"]["text"].startswith("[PAPER] ")
