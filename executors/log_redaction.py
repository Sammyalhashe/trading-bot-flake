"""Scrubs RPC URL parts, API keys and the private key from every root-logger record, because
URL tokens also reappear inside RPC exception text."""
import logging
from urllib.parse import parse_qsl, urlsplit

REDACTED = "[redacted]"
UNPARSED_HOST = "unparsed-url"
MIN_URL_PART_LENGTH = 8

_secrets = set()


def url_host(url):
    try:
        return urlsplit(url).hostname or UNPARSED_HOST
    except (ValueError, TypeError, AttributeError):
        return UNPARSED_HOST


def register_secret(value):
    if value:
        _secrets.add(str(value))


def register_url(url):
    try:
        parts = urlsplit(url)
        password, username = parts.password, parts.username
        path_segments = [s for s in parts.path.split("/") if len(s) >= MIN_URL_PART_LENGTH]
        query_values = [v for _, v in parse_qsl(parts.query) if len(v) >= MIN_URL_PART_LENGTH]
    except (ValueError, TypeError, AttributeError):
        path_segments, query_values, password, username = [], [], None, None
    register_secret(url)
    register_secret(password)
    if username and len(username) >= MIN_URL_PART_LENGTH:
        register_secret(username)
    for part in path_segments + query_values:
        register_secret(part)


def redact(text):
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


class _RedactingFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        scrubbed = redact(message)
        if scrubbed != message:
            record.msg, record.args = scrubbed, None
        return True


def install_filter():
    root = logging.getLogger()
    if not any(isinstance(f, _RedactingFilter) for f in root.filters):
        root.addFilter(_RedactingFilter())
