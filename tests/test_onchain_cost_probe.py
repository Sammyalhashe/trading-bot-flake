import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import onchain_cost_probe as probe  # noqa: E402
import onchain_cost_report as report  # noqa: E402

USDC, TOKEN = "usdc", "token"
MID = 50_000.0
TOKEN_DECIMALS = 8


def usdc_raw(usd):
    return int(usd * 10**6)


def token_raw(units):
    return int(units * 10**TOKEN_DECIMALS)


def pool(fee, gas=100_000, junk_options=()):
    """A fake quoter: constant price MID, `fee` taken on each leg; junk_options quote absurdly."""
    def quote(token_in, token_out, amount, option):
        if option == "missing":
            raise RuntimeError("execution reverted")
        rate = (1 - fee) * (0.01 if option in junk_options else 1.0)
        if token_in == USDC:
            return int(amount / 10**6 / MID * rate * 10**TOKEN_DECIMALS), gas
        return int(amount / 10**TOKEN_DECIMALS * MID * rate * 10**6), gas
    return quote


class TestMath:
    def test_roundtrip_bps_includes_gas(self):
        assert probe.roundtrip_bps(10_000, 9_980, gas_usd=0.0) == pytest.approx(20.0)
        assert probe.roundtrip_bps(10_000, 9_980, gas_usd=1.0) == pytest.approx(21.0)

    def test_buy_bps_vs_mid(self):
        assert probe.buy_bps(10_000, 0.199, MID) == pytest.approx((10_000 / 0.199 / MID - 1) * 1e4)

    def test_gas_usd(self):
        assert probe.gas_usd(200_000, gas_price_wei=10**7, eth_usd=2_000) == pytest.approx(0.004)


class TestBestRoundtrip:
    def test_picks_option_with_most_usdc_back(self):
        quotes = {500: pool(0.0005), 3000: pool(0.003)}
        best = probe.best_roundtrip(lambda ti, to, a, o: quotes[o](ti, to, a, o), USDC, TOKEN,
                                    usdc_raw(10_000), (3000, 500))
        assert best["option"] == 500
        assert best["usdc_back_raw"] == pytest.approx(usdc_raw(10_000 * 0.9995**2), rel=1e-6)
        assert best["gas"] == 200_000

    def test_missing_pools_are_skipped(self):
        best = probe.best_roundtrip(pool(0.0005), USDC, TOKEN, usdc_raw(1_000), ("missing", 500))
        assert best["option"] == 500

    def test_none_when_every_option_fails(self):
        assert probe.best_roundtrip(pool(0.0005), USDC, TOKEN, usdc_raw(1_000), ("missing",)) is None


class TestVenueSample:
    def test_sample_from_best_roundtrip(self):
        best = probe.best_roundtrip(pool(0.0005, gas=100_000), USDC, TOKEN, usdc_raw(10_000), (500,))
        row = probe.venue_sample("BTC", 10_000, "univ3", best, TOKEN_DECIMALS, MID, gas_usd_per_unit=1e-5)
        assert row["venue"] == "univ3" and row["option"] == "500"
        assert row["gas_usd"] == pytest.approx(2.0)
        assert row["roundtrip_bps"] == pytest.approx(probe.roundtrip_bps(10_000, 10_000 * 0.9995**2, 2.0), abs=0.01)
        assert row["buy_bps"] == pytest.approx(5.0, abs=0.1)

    def test_junk_price_rejected(self):
        best = probe.best_roundtrip(pool(0.0, junk_options=(1,)), USDC, TOKEN, usdc_raw(1_000), (1,))
        assert probe.venue_sample("BTC", 1_000, "slipstream", best, TOKEN_DECIMALS, MID, 0.0) is None

    def test_junk_option_loses_to_real_pool(self):
        best = probe.best_roundtrip(pool(0.0005, junk_options=(1,)), USDC, TOKEN, usdc_raw(1_000), (1, 500))
        assert best["option"] == 500


class TestCoinbase:
    BOOK = {"pricebook": {"bids": [{"price": "49990", "size": "0.1"}, {"price": "49900", "size": "10"}],
                          "asks": [{"price": "50010", "size": "0.1"}, {"price": "50100", "size": "10"}]}}

    def test_small_size_fills_at_top_of_book(self):
        row = probe.coinbase_sample("BTC", 1_000, self.BOOK, fee_per_side=0.0011)
        spread_bps = (50_010 - 49_990) / 50_000 * 1e4
        assert row["venue"] == "coinbase"
        assert row["roundtrip_bps"] == pytest.approx(spread_bps + 22.0, abs=0.05)
        assert row["buy_bps"] == pytest.approx(spread_bps / 2 + 11.0, abs=0.05)

    def test_large_size_walks_the_book(self):
        small = probe.coinbase_sample("BTC", 1_000, self.BOOK, 0.0011)
        large = probe.coinbase_sample("BTC", 100_000, self.BOOK, 0.0011)
        assert large["roundtrip_bps"] > small["roundtrip_bps"] + 10

    def test_too_thin_book_returns_none(self):
        assert probe.coinbase_sample("BTC", 10_000_000, self.BOOK, 0.0011) is None


class TestZeroEx:
    def test_quote_records_fee_and_gas(self):
        client = MagicMock()
        client.price.return_value = {"liquidityAvailable": True, "buyAmount": "123", "gas": "150000",
                                     "fees": {"zeroExFee": {"amount": "5", "token": "usdc"}}}
        quoter = probe.ZeroExQuoter(client)
        assert quoter(USDC, TOKEN, 1000, None) == (123, 150_000)
        params = client.price.call_args.args[0]
        assert params["chainId"] == 8453 and params["sellAmount"] == "1000"
        assert quoter.notes == [{"zeroExFee": {"amount": "5", "token": "usdc"}}]

    def test_no_liquidity_raises(self):
        client = MagicMock()
        client.price.return_value = {"liquidityAvailable": False}
        with pytest.raises(RuntimeError):
            probe.ZeroExQuoter(client)(USDC, TOKEN, 1000, None)

    def test_skipped_without_key(self, monkeypatch):
        monkeypatch.delenv("ZEROX_API_KEY", raising=False)
        assert probe.make_zerox_quoter() is None


class TestStorageAndReport:
    def _rows(self, venue, values):
        return [{"ts": 1_000 + i, "asset": "BTC", "size_usd": 10_000, "venue": venue, "option": "500",
                 "buy_bps": v / 2, "roundtrip_bps": v, "gas_usd": 0.01, "detail": "{}"}
                for i, v in enumerate(values)]

    def test_rows_persist(self, tmp_path):
        db = tmp_path / "costs.db"
        probe.record(db, self._rows("univ3", [15.0]))
        probe.record(db, self._rows("coinbase", [22.0]))
        assert sqlite3.connect(db).execute("select count(*) from samples").fetchone()[0] == 2

    def test_summary_median_and_p90_sorted_cheapest_first(self, tmp_path):
        db = tmp_path / "costs.db"
        probe.record(db, self._rows("univ3", [10, 12, 14, 16, 18, 20, 22, 24, 26, 100]))
        probe.record(db, self._rows("coinbase", [22.0] * 10))
        summary = report.summarize(db, since_ts=0)
        assert [s["venue"] for s in summary] == ["univ3", "coinbase"]
        assert summary[0]["n"] == 10
        assert summary[0]["median_bps"] == pytest.approx(19.0)
        assert summary[0]["p90_bps"] == pytest.approx(26.0)

    def test_summary_empty_before_first_probe(self, tmp_path):
        assert report.summarize(tmp_path / "missing.db", since_ts=0) == []
        assert not (tmp_path / "missing.db").exists()

    def test_summary_respects_window(self, tmp_path):
        db = tmp_path / "costs.db"
        probe.record(db, self._rows("univ3", [10, 20]))
        assert report.summarize(db, since_ts=1_001)[0]["n"] == 1


class TestReadOnly:
    def test_aero_v2_executor_has_no_account(self):
        executor = probe.make_aero_v2_executor("https://example.invalid")
        assert executor.account is None and executor.private_key is None


def http_session(payload, status=200):
    response = MagicMock(status_code=status)
    response.json.return_value = payload
    response.raise_for_status.side_effect = None if status == 200 else RuntimeError(f"HTTP {status}")
    session = MagicMock()
    session.get.return_value = session.post.return_value = response
    return session


BTC_TOKEN, USDC_TOKEN = probe.ASSETS["BTC"], probe.USDC


class TestAggregators:
    def test_kyberswap(self):
        session = http_session({"code": 0, "data": {"routeSummary": {"amountOut": "1583", "gas": "250000"}}})
        assert probe.KyberSwapQuoter(session)(USDC_TOKEN, BTC_TOKEN, 1000, None) == (1583, 250_000)
        assert session.get.call_args.kwargs["params"]["amountIn"] == "1000"

    def test_kyberswap_error_raises(self):
        with pytest.raises(RuntimeError):
            probe.KyberSwapQuoter(http_session({"code": 4008, "message": "no route"}))(USDC_TOKEN, BTC_TOKEN, 1, None)

    def test_velora_uses_amount_after_partner_fee(self):
        session = http_session({"priceRoute": {"destAmount": "1600", "destAmountAfterFee": "1590",
                                               "gasCost": "300000", "partnerFee": 0.01}})
        quoter = probe.VeloraQuoter(session)
        assert quoter(USDC_TOKEN, BTC_TOKEN, 1000, None) == (1590, 300_000)
        params = session.get.call_args.kwargs["params"]
        assert (params["srcDecimals"], params["destDecimals"], params["network"]) == (6, 8, 8453)
        assert quoter.notes == [{"partnerFee": 0.01}]

    def test_openocean(self):
        session = http_session({"code": 200, "data": {"outAmount": "1577", "estimatedGas": "900000"}})
        assert probe.OpenOceanQuoter(session, gas_price_wei=10**7)(USDC_TOKEN, BTC_TOKEN, 1000, None) == (1577, 900_000)
        assert session.get.call_args.kwargs["params"]["gasPriceDecimals"] == str(10**7)

    def test_openocean_error_raises(self):
        with pytest.raises(RuntimeError):
            probe.OpenOceanQuoter(http_session({"code": 500, "error": "x"}), 1)(USDC_TOKEN, BTC_TOKEN, 1, None)

    def test_cow_buy_amount_already_net_of_network_fee(self):
        session = http_session({"quote": {"buyAmount": "1570", "sellAmount": "990", "feeAmount": "10"},
                                "protocolFeeBps": "2"})
        quoter = probe.CowQuoter(session)
        assert quoter(USDC_TOKEN, BTC_TOKEN, 1000, None) == (1570, 0)
        body = session.post.call_args.kwargs["json"]
        assert body["kind"] == "sell" and body["sellAmountBeforeFee"] == "1000"
        assert quoter.notes == [{"feeAmount": "10", "protocolFeeBps": "2"}]

    def test_http_error_raises(self):
        with pytest.raises(RuntimeError):
            probe.KyberSwapQuoter(http_session({}, status=503))(USDC_TOKEN, BTC_TOKEN, 1, None)
