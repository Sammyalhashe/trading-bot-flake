"""SQLite-based trade audit log with WAL mode for concurrent read access."""

import sqlite3
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any


class TradeLog:
    """Append-only trade log backed by SQLite."""

    SCHEMA_VERSION = 1

    def __init__(self, db_path: Path):
        self.db_path = str(db_path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        """Create a new connection per call for concurrency safety."""
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        """Create tables and indexes if they don't exist."""
        try:
            conn = self._connect()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < self.SCHEMA_VERSION:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        executor_id TEXT NOT NULL,
                        asset TEXT NOT NULL,
                        product_id TEXT NOT NULL,
                        side TEXT NOT NULL,
                        price REAL NOT NULL,
                        quantity REAL NOT NULL,
                        usd_value REAL,
                        entry_price REAL,
                        pnl REAL,
                        fee REAL,
                        reason TEXT,
                        market_regime TEXT,
                        rsi REAL,
                        momentum REAL,
                        hwm REAL
                    );
                    CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
                    CREATE INDEX IF NOT EXISTS idx_trades_asset ON trades(asset);
                    CREATE INDEX IF NOT EXISTS idx_trades_regime ON trades(market_regime);
                """)
                conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
                conn.commit()
            conn.close()
        except Exception as e:
            logging.error(f"TradeLog: failed to initialize schema: {e}")

    def record_buy(self, timestamp: str, executor_id: str, asset: str,
                   product_id: str, price: float, quantity: float,
                   usd_value: float, market_regime: str,
                   rsi: Optional[float] = None,
                   momentum: Optional[float] = None):
        """Insert a BUY trade record."""
        try:
            conn = self._connect()
            conn.execute(
                """INSERT INTO trades
                   (timestamp, executor_id, asset, product_id, side, price,
                    quantity, usd_value, market_regime, rsi, momentum)
                   VALUES (?, ?, ?, ?, 'BUY', ?, ?, ?, ?, ?, ?)""",
                (timestamp, executor_id, asset, product_id, price,
                 quantity, usd_value, market_regime, rsi, momentum))
            conn.commit()
            conn.close()
        except Exception as e:
            logging.error(f"TradeLog: failed to record buy: {e}")

    def record_sell(self, timestamp: str, executor_id: str, asset: str,
                    product_id: str, price: float, quantity: float,
                    entry_price: float, pnl: float, fee: float,
                    reason: str, market_regime: str,
                    hwm: Optional[float] = None):
        """Insert a SELL trade record."""
        try:
            conn = self._connect()
            conn.execute(
                """INSERT INTO trades
                   (timestamp, executor_id, asset, product_id, side, price,
                    quantity, usd_value, entry_price, pnl, fee, reason,
                    market_regime, hwm)
                   VALUES (?, ?, ?, ?, 'SELL', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (timestamp, executor_id, asset, product_id, price,
                 quantity, price * quantity, entry_price, pnl, fee,
                 reason, market_regime, hwm))
            conn.commit()
            conn.close()
        except Exception as e:
            logging.error(f"TradeLog: failed to record sell: {e}")

    def query(self, asset: Optional[str] = None, side: Optional[str] = None,
              regime: Optional[str] = None, since: Optional[str] = None,
              until: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Query trades with optional filters. Returns list of dicts."""
        try:
            conn = self._connect()
            clauses = []
            params = []
            if asset:
                clauses.append("asset = ?")
                params.append(asset.upper())
            if side:
                clauses.append("side = ?")
                params.append(side.upper())
            if regime:
                clauses.append("market_regime = ?")
                params.append(regime)
            if since:
                clauses.append("timestamp >= ?")
                params.append(since)
            if until:
                clauses.append("timestamp <= ?")
                params.append(until + "T23:59:59" if "T" not in until else until)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            sql = f"SELECT * FROM trades{where} ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            result = [dict(r) for r in rows]
            conn.close()
            return result
        except Exception as e:
            logging.error(f"TradeLog: query failed: {e}")
            return []

    def summary(self, asset: Optional[str] = None,
                since: Optional[str] = None) -> Dict[str, Any]:
        """Return aggregate stats for SELL trades (where PnL is known)."""
        try:
            conn = self._connect()
            clauses = ["side = 'SELL'", "pnl IS NOT NULL"]
            params = []
            if asset:
                clauses.append("asset = ?")
                params.append(asset.upper())
            if since:
                clauses.append("timestamp >= ?")
                params.append(since)
            where = " WHERE " + " AND ".join(clauses)
            rows = conn.execute(
                f"SELECT pnl FROM trades{where}", params).fetchall()
            conn.close()

            pnls = [r["pnl"] for r in rows]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            total = len(pnls)
            return {
                "total_trades": total,
                "wins": len(wins),
                "losses": len(losses),
                "total_pnl": sum(pnls) if pnls else 0.0,
                "win_rate": (len(wins) / total * 100) if total > 0 else 0.0,
                "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
                "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
            }
        except Exception as e:
            logging.error(f"TradeLog: summary failed: {e}")
            return {"total_trades": 0, "wins": 0, "losses": 0,
                    "total_pnl": 0.0, "win_rate": 0.0,
                    "avg_win": 0.0, "avg_loss": 0.0}

    @staticmethod
    def format_table(rows: List[Dict[str, Any]]) -> str:
        """Pretty-print rows as an aligned text table."""
        if not rows:
            return "(no trades)"
        # Select display columns
        display_cols = ["timestamp", "side", "asset", "price", "quantity",
                        "usd_value", "pnl", "reason", "market_regime"]
        cols = [c for c in display_cols if any(r.get(c) is not None for r in rows)]
        if not cols:
            cols = list(rows[0].keys())

        # Calculate column widths
        widths = {c: len(c) for c in cols}
        formatted_rows = []
        for r in rows:
            fmt = {}
            for c in cols:
                v = r.get(c)
                if v is None:
                    fmt[c] = ""
                elif isinstance(v, float):
                    if c in ("price", "usd_value", "pnl", "entry_price", "hwm"):
                        fmt[c] = f"${v:,.2f}" if c != "pnl" else f"${v:+,.2f}"
                    else:
                        fmt[c] = f"{v:.4f}"
                else:
                    fmt[c] = str(v)
                widths[c] = max(widths[c], len(fmt[c]))
            formatted_rows.append(fmt)

        # Build table
        header = "  ".join(c.ljust(widths[c]) for c in cols)
        sep = "  ".join("-" * widths[c] for c in cols)
        lines = [header, sep]
        for fmt in formatted_rows:
            lines.append("  ".join(fmt.get(c, "").ljust(widths[c]) for c in cols))
        return "\n".join(lines)
