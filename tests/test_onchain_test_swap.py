import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import onchain_cost_report as report  # noqa: E402
import onchain_test_swap as cli  # noqa: E402

MID = 64_000.0
ETH_USD = 3_000.0


def result(quoted, received, gas_wei=10**12, ref="0xtx"):
    return {"success": True, "confirmed": True, "quoted_out": quoted, "min_out": quoted * 995 // 1000,
            "received": received, "gas_wei": gas_wei, "tx_hash": ref}


class TestFillRow:
    def test_buy_costs_vs_quote_and_mid(self):
        row = cli.fill_row("kyberswap", "BTC", "buy", 25 * 10**6, result(39_000, 38_961), MID, ETH_USD, 3.0)
        assert row["slippage_bps"] == pytest.approx(10.0)
        assert row["cost_bps"] == pytest.approx((25 / 0.00038961 / MID - 1) * 1e4)
        assert row["gas_usd"] == pytest.approx(0.003)
        assert row["ref"] == "0xtx" and row["seconds"] == 3.0

    def test_sell_costs_vs_mid(self):
        row = cli.fill_row("cow", "BTC", "sell", 39_000, result(24_900_000, 24_950_000, gas_wei=0, ref=None),
                           MID, ETH_USD, 60.0)
        assert row["slippage_bps"] == pytest.approx((1 - 24_950_000 / 24_900_000) * 1e4)
        assert row["cost_bps"] == pytest.approx((1 - 24.95 / 0.00039 / MID) * 1e4)
        assert row["gas_usd"] == 0.0

    def test_eth_sell_uses_18_decimals(self):
        row = cli.fill_row("kyberswap", "ETH", "sell", 2 * 10**16, result(59_900_000, 59_900_000), ETH_USD, ETH_USD, 2.0)
        assert row["cost_bps"] == pytest.approx((1 - 59.9 / 0.02 / ETH_USD) * 1e4)


def fake_executor(buy_received=39_000, sell_received=24_950_000, fail_on=None):
    ex = MagicMock()
    ex.account.address = "0xme"
    calls = []

    def swap(token_in, token_out, amount_in, recipient, reference_price=None, router=None):
        side = "buy" if token_in == cli.USDC else "sell"
        calls.append((side, amount_in, router, reference_price))
        if fail_on == side:
            return {"success": False, "error": "nope"}
        return result(39_100, buy_received) if side == "buy" else result(25_000_000, sell_received)
    ex.execute_swap.side_effect = swap
    ex.wrap_eth.return_value = {"success": True, "tx_hash": "0xwrap", "gas_wei": 10**12}
    return ex, calls


@pytest.fixture
def prices(monkeypatch):
    monkeypatch.setattr(cli, "mid_price", lambda asset: {"BTC": MID, "ETH": ETH_USD}[asset])


class TestRoundtrip:
    def test_sells_exactly_what_was_bought_and_records_legs(self, prices, tmp_path):
        ex, calls = fake_executor()
        db = tmp_path / "costs.db"
        summary = cli.roundtrip(ex, "kyberswap", "BTC", 25, 2, db)
        assert [c[0] for c in calls] == ["buy", "sell", "buy", "sell"]
        assert calls[1][1] == 39_000 and calls[0][1] == 25 * 10**6
        assert all(c[2] == "kyberswap" for c in calls) and calls[0][3] == MID
        assert len(summary) == 2 and summary[0]["usdc_back"] == pytest.approx(24.95)
        assert sqlite3.connect(db).execute("select count(*) from fills").fetchone()[0] == 4

    def test_stops_after_failed_leg(self, prices, tmp_path):
        ex, calls = fake_executor(fail_on="sell")
        assert cli.roundtrip(ex, "cow", "BTC", 25, 2, tmp_path / "c.db") == []
        assert [c[0] for c in calls] == ["buy", "sell"]

    def test_caps(self, prices, tmp_path):
        ex, calls = fake_executor()
        with pytest.raises(SystemExit):
            cli.roundtrip(ex, "kyberswap", "BTC", cli.MAX_TEST_USD + 1, 1, tmp_path / "c.db")
        with pytest.raises(SystemExit):
            cli.roundtrip(ex, "kyberswap", "DOGE", 10, 1, tmp_path / "c.db")
        assert calls == []


class TestFund:
    def test_wraps_then_sells_weth_for_usdc(self, prices, tmp_path):
        ex, calls = fake_executor()
        cli.fund(ex, "kyberswap", 0.02, tmp_path / "c.db")
        ex.wrap_eth.assert_called_once_with(2 * 10**16)
        assert calls == [("sell", 2 * 10**16, "kyberswap", ETH_USD)]

    def test_no_swap_when_wrap_fails(self, prices, tmp_path):
        ex, calls = fake_executor()
        ex.wrap_eth.return_value = {"success": False, "error": "x"}
        cli.fund(ex, "kyberswap", 0.02, tmp_path / "c.db")
        assert calls == []

    def test_cap(self, prices, tmp_path):
        ex, _ = fake_executor()
        with pytest.raises(SystemExit):
            cli.fund(ex, "kyberswap", cli.MAX_WRAP_ETH + 0.01, tmp_path / "c.db")
        ex.wrap_eth.assert_not_called()


class TestDryRun:
    def test_never_signs(self, prices):
        ex, calls = fake_executor()
        ex.kyber.routes.return_value = {"amountOut": "39100"}
        ex.kyber.build.return_value = {"amountOut": "39100", "data": "0x"}
        ex._cow_quote.return_value = (1, 39_100)
        for venue in ("kyberswap", "cow"):
            cli.dry_run(ex, venue, cli.USDC, cli.TOKENS["BTC"], 25 * 10**6, MID)
        assert calls == []
        ex.wrap_eth.assert_not_called()
        ex._approve_token.assert_not_called()
        ex._sign_cow_order.assert_not_called()
        ex._send_contract_tx.assert_not_called()


class TestReportFills:
    def test_fills_summary(self, prices, tmp_path):
        db = tmp_path / "costs.db"
        ex, _ = fake_executor()
        cli.roundtrip(ex, "kyberswap", "BTC", 25, 2, db)
        rows = report.summarize_fills(db, since_ts=0)
        assert {(r["venue"], r["side"]) for r in rows} == {("kyberswap", "buy"), ("kyberswap", "sell")}
        assert all(r["n"] == 2 for r in rows)

    def test_no_fills_table(self, tmp_path):
        assert report.summarize_fills(tmp_path / "missing.db", since_ts=0) == []
