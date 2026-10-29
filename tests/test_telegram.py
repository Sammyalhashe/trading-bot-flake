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
