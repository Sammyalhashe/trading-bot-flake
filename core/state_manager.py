"""State management and persistence for trading bot"""
import json
import fcntl
import os
import time
import datetime
from pathlib import Path
from decimal import Decimal
import logging

logger = logging.getLogger(__name__)


class StateManager:
    """Manage trading state persistence and file locking"""

    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.lock_file = state_file.with_suffix('.lock')

    def _acquire_lock(self):
        """Acquire file lock for state access. Returns lock file handle."""
        os.makedirs(os.path.dirname(self.lock_file), exist_ok=True)
        lock_fd = open(self.lock_file, 'w')
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return lock_fd

    def _release_lock(self, lock_fd):
        """Release file lock for state access."""
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        except Exception:
            pass

    def load_state(self) -> dict:
        """Load state from disk with file locking"""
        default_state = {
            "entry_prices": {},
            "high_water_marks": {},
            "take_profit_flags": {},
            "short_close_failures": {},
            "entry_timestamps": {}
        }
        lock_fd = self._acquire_lock()
        try:
            if self.state_file.exists():
                try:
                    with open(self.state_file, 'r') as f:
                        state = json.load(f)
                    state.setdefault("entry_prices", {})
                    state.setdefault("high_water_marks", {})
                    state.setdefault("take_profit_flags", {})
                    state.setdefault("short_close_failures", {})
                    state.setdefault("entry_timestamps", {})
                    return state
                except Exception as e:
                    logger.error(f"Failed to load state: {e}")
                    return default_state
            return default_state
        finally:
            self._release_lock(lock_fd)

    def save_state(self, state: dict) -> None:
        """Save state to disk atomically with file locking"""
        lock_fd = self._acquire_lock()
        try:
            tmp = self.state_file.with_suffix('.tmp')
            with open(tmp, 'w') as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, self.state_file)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
        finally:
            self._release_lock(lock_fd)

    def update_entry_price(self, executor_id: str, product_id: str, price: float | Decimal) -> None:
        """Track position entry with initial high water mark"""
        state = self.load_state()
        key = f"{executor_id}:{product_id}"
        state.setdefault("entry_prices", {})[key] = float(price)
        # Initialize high water mark to entry price
        state.setdefault("high_water_marks", {})[key] = float(price)
        # Reset take-profit flags for new entry
        state.setdefault("take_profit_flags", {})[key] = {
            "tp1_hit": False,
            "tp2_hit": False,
            "trend_exit_hit": False
        }
        # Store entry timestamp for time-based exits
        state.setdefault("entry_timestamps", {})[key] = time.time()
        self.save_state(state)

    def clear_entry_price(self, executor_id: str, product_id: str) -> None:
        """Clear position entry and associated tracking"""
        state = self.load_state()
        key = f"{executor_id}:{product_id}"
        if key in state.get("entry_prices", {}):
            del state["entry_prices"][key]
        # Also clear high water mark
        if key in state.get("high_water_marks", {}):
            del state["high_water_marks"][key]
        # Also clear take-profit flags
        if key in state.get("take_profit_flags", {}):
            del state["take_profit_flags"][key]
        # Also clear entry timestamp
        if key in state.get("entry_timestamps", {}):
            del state["entry_timestamps"][key]
        self.save_state(state)

    def load_peak_value(self, executor_id: str = "default") -> float:
        """Load peak portfolio value for drawdown tracking"""
        state = self.load_state()
        peaks = state.get("peak_portfolio_values", {})
        return peaks.get(executor_id, 0.0)

    def save_peak_value(self, value: float, executor_id: str = "default") -> None:
        """Save peak portfolio value for drawdown tracking"""
        state = self.load_state()
        peaks = state.setdefault("peak_portfolio_values", {})
        peaks[executor_id] = value
        self.save_state(state)

    def record_trade(self, is_win: bool, pnl: float) -> None:
        """Record completed trade for performance tracking"""
        state = self.load_state()
        perf = state.setdefault("performance", {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "total_pnl": 0.0,
            "run_count": 0
        })
        perf["total_trades"] += 1
        if is_win:
            perf["winning_trades"] += 1
        else:
            perf["losing_trades"] += 1
        perf["total_pnl"] += pnl
        perf["last_run_time"] = datetime.datetime.now().isoformat()
        self.save_state(state)

    def increment_run_count(self) -> None:
        """Increment bot run counter"""
        state = self.load_state()
        perf = state.setdefault("performance", {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "total_pnl": 0.0,
            "run_count": 0
        })
        perf["run_count"] += 1
        perf["last_run_time"] = datetime.datetime.now().isoformat()
        self.save_state(state)

    def get_performance(self) -> dict:
        """Get performance metrics"""
        state = self.load_state()
        return state.get("performance", {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "total_pnl": 0.0,
            "run_count": 0
        })

    def log_performance_summary(self) -> None:
        """Log performance summary to logger"""
        perf = self.get_performance()
        total = perf.get("total_trades", 0)
        wins = perf.get("winning_trades", 0)
        win_rate = (wins / total * 100) if total > 0 else 0
        logger.info(
            f"[Performance] Trades: {total} | Wins: {wins} ({win_rate:.0f}%) | "
            f"Total PnL: ${perf.get('total_pnl', 0):+.2f}"
        )
