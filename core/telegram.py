"""Shared Telegram messaging utility."""
import os
import html
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
        detail = str(e)
        if isinstance(e, requests.HTTPError) and e.response is not None:
            try:
                detail = f"HTTP {e.response.status_code}: {e.response.json().get('description', detail)}"
            except ValueError:
                pass
        # requests puts the full URL, bot token included, into exception messages.
        logger.error(f"Failed to send Telegram message: {detail.replace(token, '<redacted>')}")
        return False


def exit_reason_label(reason, max_len=24):
    """Map a long strategy exit reason to a short, HTML-escaped label."""
    text = reason or ""
    lower = text.lower()
    if "[breakeven]" in lower:
        return "Breakeven stop"
    if "trailing stop" in lower:
        return "Trailing stop"
    if "take-profit level 2" in lower or "tp2" in lower:
        return "Take-profit 2"
    if "take-profit level 1" in lower or "tp1" in lower:
        return "Take-profit 1"
    if "take-profit" in lower:
        return "Take-profit"
    if "trend-exit" in lower or "trend exit" in lower:
        return "Trend exit"
    if len(text) > max_len:
        text = text[:max_len - 1] + "\u2026"
    return html.escape(text or "-")


def exchange_label(executor_id):
    """Short venue name from an executor id like 'EthereumExecutor_0x4FA0'."""
    name = executor_id or ""
    if name.startswith("CoinbaseFutures"):
        return "Coinbase Futures"
    if name.startswith("Coinbase"):
        return "Coinbase"
    if name.startswith("Ethereum"):
        return "Base"
    return html.escape(name.split("Executor")[0] or name)


def format_price(price):
    """Price with precision suited to its magnitude."""
    if price >= 1:
        return f"${price:,.2f}"
    return f"${price:.4f}" if price >= 0.01 else f"${price:.6f}"


def format_usd(amount, signed=False, whole=False):
    """USD with a true minus sign, e.g. -$20.03 or +$1,052."""
    sign = ("+" if amount >= 0 else "\u2212") if signed else ("\u2212" if amount < 0 else "")
    return f"{sign}${abs(amount):,.0f}" if whole else f"{sign}${abs(amount):,.2f}"
