"""Shared Telegram messaging utility."""
import os
import logging
import requests

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN_FILE = "/run/secrets/telegram_bot_token"
_cached_token = None


def _get_token():
    """Lazy-load and cache the Telegram bot token."""
    global _cached_token
    if _cached_token is not None:
        return _cached_token
    if not os.path.exists(TELEGRAM_TOKEN_FILE):
        logger.warning(f"Telegram token file not found at {TELEGRAM_TOKEN_FILE}")
        return None
    with open(TELEGRAM_TOKEN_FILE, 'r') as f:
        _cached_token = f.read().strip()
    return _cached_token


def send_telegram_message(message, parse_mode="HTML"):
    """Send a Telegram message. Returns True on success, False on failure.

    Never raises — all exceptions are caught and logged.
    """
    token = _get_token()
    if not token:
        return False
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "8555669756")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": parse_mode}
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return False
