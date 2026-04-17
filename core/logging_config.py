"""Logging configuration with daily rotation, gzip compression, and auto-cleanup.

Uses loguru for file logging with:
  - Daily rotation at midnight
  - Gzip compression of rotated files
  - Automatic deletion of logs older than 10 days
  - Intercept handler to bridge stdlib logging → loguru

Usage:
    from core.logging_config import setup_logging
    setup_logging("/path/to/trading.log")
"""
import logging
import sys
from pathlib import Path

from loguru import logger


class _InterceptHandler(logging.Handler):
    """Bridge stdlib logging calls to loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        # Find loguru level matching the stdlib record
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where the log originated
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(log_file: str | Path, level: str = "INFO") -> None:
    """Configure logging with file rotation and console output.

    Args:
        log_file: Path to the log file (e.g. ~/.openclaw/workspace/trading-bot/trading.log)
        level: Minimum log level (default: INFO)
    """
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Remove loguru's default stderr handler
    logger.remove()

    # Console handler (stdout, for systemd journal capture)
    logger.add(
        sys.stdout,
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss} - {level} - {message}",
    )

    # File handler with daily rotation, gzip, and 10-day retention
    logger.add(
        str(log_path),
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss} - {level} - {message}",
        rotation="00:00",      # New file at midnight
        compression="gz",     # Gzip old files
        retention="10 days",  # Delete after 10 days
    )

    # Trades-only file — buy/sell executions, PnL, and exit triggers
    _TRADE_MARKERS = ("Buy ", "✅ Sold", "📊 PnL:", "🚨")

    def _trade_filter(record):
        msg = record["message"]
        return any(m in msg for m in _TRADE_MARKERS)

    trades_path = log_path.with_name("trades.log")
    logger.add(
        str(trades_path),
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss} - {level} - {message}",
        filter=_trade_filter,
        rotation="00:00",
        compression="gz",
        retention="10 days",
    )

    # Intercept all stdlib logging and route through loguru
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
