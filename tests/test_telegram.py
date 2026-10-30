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
                                 "Trailing stop-loss triggered for UNI (price $7.18 < stop $7.20)", -1.2)

    msg = send.call_args[0][0]
    assert "&lt; stop" in msg
    assert "< stop" not in msg
    assert msg.startswith("<b>SELL UNI</b>")


def test_pnl_report_escapes_trade_reasons():
    import pnl_report
    trade_log = MagicMock()
    trade_log.query.return_value = [{
        "timestamp": "2026-10-29T16:04:35", "asset": "TIA", "pnl": -20.03,
        "reason": "Trailing stop-loss triggered for TIA (price $0.53 < stop $0.53)",
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

    assert "&lt; stop" in report
    assert "< stop" not in report


def test_regime_alert_escapes_report_content(tmp_path):
    import notify_telegram
    report_file = tmp_path / "report.txt"
    report_file.write_text("BTC MA21 < MA55 & falling")
    state = {"last_status": "BEAR", "regime_changes": 0, "bullish_changes": 0,
             "bearish_changes": 0, "transactions": []}
    with patch.object(notify_telegram, "REPORT_FILE", str(report_file)), \
         patch.object(notify_telegram, "load_market_state", return_value=state), \
         patch.object(notify_telegram, "save_market_state"), \
         patch.object(notify_telegram, "extract_market_status", return_value=("BULL", None, None)), \
         patch.object(notify_telegram, "send_telegram_message") as send:
        notify_telegram.main()

    msg = send.call_args[0][0]
    assert msg.startswith("<pre>") and msg.endswith("</pre>")
    assert "MA21 &lt; MA55 &amp; falling" in msg
