import logging
import os
import sys
from unittest.mock import MagicMock, patch

import requests

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import telegram

TOKEN = "123456:SECRET-token"


def _http_error_response(description):
    response = MagicMock()
    response.status_code = 400
    response.json.return_value = {"ok": False, "error_code": 400, "description": description}
    response.raise_for_status.side_effect = requests.HTTPError(
        f"400 Client Error: Bad Request for url: https://api.telegram.org/bot{TOKEN}/sendMessage",
        response=response,
    )
    return response


def test_http_error_logs_description_without_token(caplog):
    response = _http_error_response("Bad Request: can't parse entities")
    with patch.object(telegram, "_get_token", return_value=TOKEN), \
         patch.object(telegram.requests, "post", return_value=response), \
         caplog.at_level(logging.ERROR):
        assert telegram.send_telegram_message("<b>x</b>") is False

    assert "SECRET" not in caplog.text
    assert "can't parse entities" in caplog.text


def test_connection_error_is_redacted(caplog):
    error = requests.ConnectionError(
        f"HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded with url: /bot{TOKEN}/sendMessage")
    with patch.object(telegram, "_get_token", return_value=TOKEN), \
         patch.object(telegram.requests, "post", side_effect=error), \
         caplog.at_level(logging.ERROR):
        assert telegram.send_telegram_message("hi") is False

    assert "SECRET" not in caplog.text


def test_sell_notification_escapes_html_in_reason():
    import trading_bot
    with patch.object(trading_bot, "send_telegram_message") as send:
        trading_bot._notify_sell("CoinbaseExecutor", "UNI", 7.18, 7.16, 1.0,
                                 "Custom exit: price < stop", -1.2)

    msg = send.call_args[0][0]
    assert "price &lt;" in msg
    assert "price <" not in msg
    assert msg.startswith("\U0001f534 <b>SELL UNI</b>")


def test_pnl_report_escapes_trade_reasons():
    import pnl_report
    trade_log = MagicMock()
    trade_log.total_fees.return_value = 0.0
    trade_log.query.return_value = [{
        "timestamp": "2026-10-29T16:04:35", "asset": "TIA", "pnl": -20.03,
        "reason": "Custom exit: price < stop",
    }]
    state_manager = MagicMock()
    state_manager.get_performance.return_value = {}
    state_manager.load_state.return_value = {}
    with patch.object(pnl_report, "ExecutorConfig"), \
         patch.object(pnl_report, "StateManager", return_value=state_manager), \
         patch.object(pnl_report, "TradeLog", return_value=trade_log), \
         patch.object(pnl_report, "get_all_balances",
                      return_value={"cash": {"USD": 0.0, "USDC": 100.0}, "crypto": {}}):
        report, _ = pnl_report.build_report()

    assert "price &lt;" in report
    assert "price <" not in report


def test_regime_alert_escapes_regime_names(tmp_path):
    import notify_telegram
    report_file = tmp_path / "report.txt"
    report_file.write_text("BTC MA21 < MA55 & falling")
    state = {"last_status": "BEAR", "regime_changes": 0, "bullish_changes": 0,
             "bearish_changes": 0, "transactions": []}
    with patch.object(notify_telegram, "REPORT_FILE", str(report_file)), \
         patch.object(notify_telegram, "load_market_state", return_value=state), \
         patch.object(notify_telegram, "save_market_state"), \
         patch.object(notify_telegram, "extract_market_status", return_value=("BULL<x>", "UP&", None)), \
         patch.object(notify_telegram, "send_telegram_message") as send:
        notify_telegram.main()

    msg = send.call_args[0][0]
    assert "BULL&lt;x&gt;" in msg and "UP&amp;" in msg
    assert "<x>" not in msg
    assert "<pre>" not in msg


def test_extract_market_status_ignores_banner_decoration():
    import notify_telegram
    plain = notify_telegram.extract_market_status("Market Regime: STRONG_BEAR")
    banner = notify_telegram.extract_market_status("━━━ Market Regime: STRONG_BEAR ━━━")
    assert plain[0] == banner[0] == "STRONG_BEAR"


def test_extract_market_status_dual_signal_format():
    import notify_telegram
    regime, btc, rotation = notify_telegram.extract_market_status(
        "Market Regime: STRONG_BULL (BTC: BULL | Rotation: ETH_LEADING)")
    assert (regime, btc, rotation) == ("STRONG_BULL", "BULL", "ETH_LEADING")


def test_exit_reason_label_maps_strategy_strings():
    label = telegram.exit_reason_label
    assert label("Trailing stop-loss triggered for TIA (price $0.53 < stop $0.53)") == "Trailing stop"
    assert label("Trailing stop-loss triggered for TIA (...) [breakeven]") == "Breakeven stop"
    assert label("Take-profit level 1 triggered for X (...)") == "Take-profit 1"
    assert label("Take-profit level 2 triggered for X (...)") == "Take-profit 2"
    assert label("MTF TP1 for X (...)") == "Take-profit 1"
    assert label("Trend-exit (50%) triggered for X (...)") == "Trend exit"
    fallback = label("Something <odd> and very long indeed, longer than the limit")
    assert "<odd>" not in fallback and len(fallback) <= 24 * 5


def test_exchange_label_shortens_executor_ids():
    assert telegram.exchange_label("CoinbaseExecutor") == "Coinbase"
    assert telegram.exchange_label("EthereumExecutor_0x4FA0") == "Base"


def test_buy_notification_headline_first_and_escaped():
    import trading_bot
    with patch.object(trading_bot, "send_telegram_message") as send:
        trading_bot._notify_buy("CoinbaseExecutor", "UNI", 7.16, 250.0, "BULL<",
                                {"rsi": 55.4, "momentum": 3.21})

    msg = send.call_args[0][0]
    assert msg.splitlines()[0] == "\U0001f7e2 <b>BUY UNI</b> \u00b7 $250 @ $7.16"
    assert "Coinbase" in msg and "Executor" not in msg
    assert "BULL&lt;" in msg


def test_sell_notification_headline_and_label():
    import trading_bot
    with patch.object(trading_bot, "send_telegram_message") as send:
        trading_bot._notify_sell("EthereumExecutor_0x4FA0", "TIA", 0.53, 0.55, 100.0,
                                 "Trailing stop-loss triggered for TIA (price $0.53 < stop $0.53)", -20.03)

    msg = send.call_args[0][0]
    assert msg.splitlines()[0] == "\U0001f534 <b>SELL TIA</b> \u00b7 \u2212$20.03 (\u22123.6%)"
    assert "Trailing stop" in msg and "Base" in msg
    assert "0x4FA0" not in msg


def test_pnl_report_includes_fees_section_and_snapshot():
    import pnl_report
    trade_log = MagicMock()
    trade_log.total_fees.side_effect = lambda since=None: 30.0 if since else 100.0
    trade_log.query.return_value = []
    state_manager = MagicMock()
    state_manager.get_performance.return_value = {"total_pnl": 300.0}
    state_manager.load_state.return_value = {}
    with patch.object(pnl_report, "ExecutorConfig"), \
         patch.object(pnl_report, "StateManager", return_value=state_manager), \
         patch.object(pnl_report, "TradeLog", return_value=trade_log), \
         patch.object(pnl_report, "get_all_balances",
                      return_value={"cash": {"USD": 0.0, "USDC": 100.0}, "crypto": {}}):
        report, snapshot = pnl_report.build_report()

    assert report.splitlines()[0].startswith("\U0001f4ca <b>Daily PnL</b> \u00b7 +$300.00")
    assert "Total $100.00" in report
    assert "Last 30d $30.00" in report
    assert "25% of gross profit" in report
    assert snapshot["fees"]["total_usd"] == 100.0
    assert snapshot["fees"]["last_30d_usd"] == 30.0
    assert snapshot["realized"]["total_pnl_usd"] == 300.0


def test_pnl_report_flags_fees_exceeding_gross_profit():
    import pnl_report
    trade_log = MagicMock()
    trade_log.total_fees.return_value = 50.0
    trade_log.query.return_value = []
    state_manager = MagicMock()
    state_manager.get_performance.return_value = {"total_pnl": -80.0}
    state_manager.load_state.return_value = {}
    with patch.object(pnl_report, "ExecutorConfig"), \
         patch.object(pnl_report, "StateManager", return_value=state_manager), \
         patch.object(pnl_report, "TradeLog", return_value=trade_log), \
         patch.object(pnl_report, "get_all_balances",
                      return_value={"cash": {"USD": 0.0, "USDC": 0.0}, "crypto": {}}):
        report, _ = pnl_report.build_report()

    assert "fees > gross profit" in report


def test_trade_log_total_fees(tmp_path):
    from core.trade_log import TradeLog
    log = TradeLog(tmp_path / "t.db")
    log.record_buy("2026-01-01T00:00:00", "Ex", "UNI", "UNI-USDC", 7.0, 1.0, 7.0, "BULL")
    log.record_sell("2026-01-02T00:00:00", "Ex", "UNI", "UNI-USDC", 8.0, 1.0, 7.0, 1.0, 0.5, "r", "BULL")
    log.record_sell("2026-03-01T00:00:00", "Ex", "TIA", "TIA-USDC", 1.0, 1.0, 2.0, -1.0, 0.25, "r", "BULL")
    assert log.total_fees() == 0.75
    assert log.total_fees(since="2026-02-01T00:00:00") == 0.25
    assert TradeLog(tmp_path / "empty.db").total_fees() == 0.0


def test_regime_alert_headline_direction_and_recent_changes(tmp_path):
    import notify_telegram
    report_file = tmp_path / "report.txt"
    report_file.write_text("BTC Macro: $67,000 | MA21: $1 | MA55: $2 | Spread: -1.20% → BEAR\n"
                           "ETH/BTC Rotation: Ratio=0.05123 (ETH $3,400 / BTC $67,000)")
    old = [{"time": f"2026-10-0{i}T10:00:00", "detail": "d", "change": f"A{i} → B{i}"} for i in range(1, 6)]
    state = {"last_status": "BEAR", "regime_changes": 0, "bullish_changes": 0,
             "bearish_changes": 0, "transactions": old}
    with patch.object(notify_telegram, "REPORT_FILE", str(report_file)), \
         patch.object(notify_telegram, "load_market_state", return_value=state), \
         patch.object(notify_telegram, "save_market_state"), \
         patch.object(notify_telegram, "extract_market_status",
                      return_value=("STRONG_BEAR", "BEAR", "ETH_LEADING")), \
         patch.object(notify_telegram, "send_telegram_message") as send:
        notify_telegram.main()

    msg = send.call_args[0][0]
    assert msg.splitlines()[0] == "\U0001f4c9 <b>Regime: BEAR \u2192 STRONG_BEAR</b>"
    assert "BTC $67,000" in msg and "ETH/BTC 0.0512" in msg
    assert "Total regime changes" not in msg
    assert msg.count(" \u00b7 ") >= 3 and "A1" not in msg
    assert state["regime_changes"] == 1
