"""Strategy protocol for pluggable trading strategies"""
from typing import Protocol, runtime_checkable
import pandas as pd


@runtime_checkable
class Strategy(Protocol):
    name: str
    required_timeframes: dict[str, int]  # e.g. {"1h": 55} or {"1h": 100, "15m": 60}

    def should_skip_regime(self, market_regime: str, full_regime: str) -> bool:
        """Return True to skip all new entries in this regime."""
        ...

    def scan_entry(self, asset: str, product_id: str,
                   market_data: dict[str, pd.DataFrame],
                   market_regime: str, full_regime: str) -> dict | None:
        """Scan for a long entry signal.

        Args:
            market_data: Dict of DataFrames keyed by timeframe (e.g. "1h", "15m").

        Returns {"asset", "product_id", "score"} or None.
        """
        ...

    def rank_candidates(self, candidates: list[dict]) -> list[dict]:
        """Rank and sort entry candidates (best first)."""
        ...

    def check_exit(self, asset: str, product_id: str,
                   market_data: dict[str, pd.DataFrame],
                   price: float, entry: float, hwm: float,
                   tp_flags: dict, state: dict, entry_key: str) -> tuple[bool, float, str, dict]:
        """Check for exit signal.

        Args:
            market_data: Dict of DataFrames keyed by timeframe (e.g. "1h", "15m").

        Returns (sell_trigger, sell_ratio, reason, updated_tp_flags).
        """
        ...
