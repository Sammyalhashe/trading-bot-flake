"""Strategy protocol for pluggable trading strategies"""
from typing import Protocol, runtime_checkable
import pandas as pd


@runtime_checkable
class Strategy(Protocol):
    name: str

    def should_skip_regime(self, market_regime: str, full_regime: str) -> bool:
        """Return True to skip all new entries in this regime."""
        ...

    def scan_entry(self, asset: str, product_id: str, df: pd.DataFrame,
                   market_regime: str, full_regime: str) -> dict | None:
        """Scan for a long entry signal.

        Returns {"asset", "product_id", "score"} or None.
        """
        ...

    def rank_candidates(self, candidates: list[dict]) -> list[dict]:
        """Rank and sort entry candidates (best first)."""
        ...

    def check_exit(self, asset: str, product_id: str, df: pd.DataFrame,
                   price: float, entry: float, hwm: float,
                   tp_flags: dict, state: dict, entry_key: str) -> tuple[bool, float, str, dict]:
        """Check for exit signal.

        Returns (sell_trigger, sell_ratio, reason, updated_tp_flags).
        """
        ...
