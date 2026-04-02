"""Centralized risk management for trading bot and backtesting."""
import time
import logging
from decimal import Decimal
from typing import Optional

from config.trading_config import TradingConfig

logger = logging.getLogger(__name__)


class RiskManager:
    """Centralized risk management for trading bot and backtesting.

    This class handles all risk-related decisions including:
    - Portfolio drawdown monitoring and circuit breakers
    - Position sizing with dynamic scaling
    - Fee calculations
    - Trading pause/resume logic
    - Peak value tracking

    Designed to work in both live trading and backtesting environments.
    """

    def __init__(
        self,
        config: TradingConfig,
        initial_capital: float,
        state_manager=None
    ):
        """Initialize risk manager.

        Args:
            config: TradingConfig instance with risk parameters
            initial_capital: Starting capital for reference
            state_manager: Optional StateManager for persistence (live trading).
                          If None, uses in-memory tracking (backtesting).
        """
        self.config = config
        self.initial_capital = initial_capital
        self.state_manager = state_manager

        # In-memory tracking for backtesting or when state_manager is None
        self._peak_values: dict[str, float] = {}  # executor_id -> peak value
        self._drawdown_timestamps: dict[str, float] = {}  # executor_id -> timestamp
        self._drawdown_paused: dict[str, bool] = {}  # executor_id -> is_paused

        logger.info(
            f"RiskManager initialized: initial_capital=${initial_capital:,.2f}, "
            f"max_drawdown={config.max_drawdown_pct}%, "
            f"cooldown={config.drawdown_cooldown_hours}h, "
            f"max_positions={config.max_concurrent_positions}"
        )

    def check_circuit_breakers(
        self,
        portfolio_value: float,
        executor_id: str = "default"
    ) -> tuple[bool, str]:
        """Check if any circuit breakers should halt trading.

        This implements the drawdown protection logic from trading_bot.py lines 365-386.

        Args:
            portfolio_value: Current portfolio value
            executor_id: Executor identifier for multi-executor tracking

        Returns:
            (allowed, reason) tuple:
                - allowed: True if trading is allowed, False if halted
                - reason: Human-readable explanation (empty if allowed)
        """
        # Get peak value for this executor
        peak = self._get_peak_value(executor_id)

        # Initialize peak on first run
        if peak == 0.0:
            self._set_peak_value(portfolio_value, executor_id)
            peak = portfolio_value

        # Update peak if current value is higher
        elif portfolio_value > peak:
            self._set_peak_value(portfolio_value, executor_id)
            peak = portfolio_value
            # Clear any paused state since we hit new high
            self._clear_drawdown_pause(executor_id)

        # Calculate drawdown percentage
        drawdown_pct = ((peak - portfolio_value) / peak * 100) if peak > 0 else 0

        # Check if drawdown exceeds limit
        max_dd = float(self.config.max_drawdown_pct)
        if drawdown_pct >= max_dd:
            # Check if cooldown period has elapsed
            dd_timestamp = self._drawdown_timestamps.get(executor_id)

            if dd_timestamp is None:
                # First time detecting drawdown - record timestamp
                self._drawdown_timestamps[executor_id] = time.time()
                self._drawdown_paused[executor_id] = True
                logger.warning(
                    f"[{executor_id}] Drawdown {drawdown_pct:.1f}% exceeds limit {max_dd}%. "
                    f"Pausing new buys."
                )
                return False, f"Drawdown {drawdown_pct:.1f}% exceeds {max_dd}% limit"

            else:
                # Check cooldown duration
                hours_paused = (time.time() - dd_timestamp) / 3600
                cooldown_hours = self.config.drawdown_cooldown_hours

                if hours_paused >= cooldown_hours:
                    # Cooldown elapsed - reset peak to current value
                    logger.warning(
                        f"[{executor_id}] Drawdown pause active for {hours_paused:.0f}h "
                        f"(limit {cooldown_hours}h). Resetting peak to ${portfolio_value:,.2f} "
                        f"to re-enable trading."
                    )
                    self._set_peak_value(portfolio_value, executor_id)
                    self._clear_drawdown_pause(executor_id)
                    return True, ""

                else:
                    # Still in cooldown period
                    logger.warning(
                        f"[{executor_id}] Drawdown {drawdown_pct:.1f}% exceeds limit {max_dd}%. "
                        f"Paused for {hours_paused:.1f}h (resets after {cooldown_hours}h)."
                    )
                    return False, f"Drawdown pause: {hours_paused:.1f}h/{cooldown_hours}h elapsed"

        else:
            # Not in drawdown - clear any previous pause state
            self._clear_drawdown_pause(executor_id)

        return True, ""

    def calculate_position_size(
        self,
        portfolio_value: float,
        price: float,
        existing_positions: int,
        max_positions: int,
        available_cash: float = None,
        executor_value: float = None,
        atr: float = None
    ) -> float:
        """Calculate safe position size with all limits applied.

        Includes volatility adjustment if ATR is provided:
        - Higher volatility (ATR) -> smaller position
        - Lower volatility -> larger position (capped by risk limits)

        Args:
            portfolio_value: Total portfolio value
            price: Asset price
            existing_positions: Number of current positions
            max_positions: Maximum allowed positions
            available_cash: Available cash
            executor_value: Optional per-executor value
            atr: Average True Range for volatility scaling

        Returns:
            Position size in USD
        """
        # Use executor_value if provided, otherwise portfolio_value
        reference_value = executor_value if executor_value is not None else portfolio_value

        # Use available_cash if provided, otherwise assume full portfolio
        cash = available_cash if available_cash is not None else reference_value

        # Base limit from risk parameters
        portfolio_risk = float(self.config.portfolio_risk_pct)
        risk_per_trade = float(self.config.risk_per_trade_pct)
        trade_limit = reference_value * portfolio_risk * risk_per_trade

        # Volatility adjustment (ATR scaling)
        # Normalizes sizing: aim for a loss of X% of trade if price moves 2.5 * ATR
        vol_multiplier = 1.0
        if atr and price > 0:
            # 2.5 * ATR is our typical stop distance. 
            # We want that distance to represent roughly 5% of the position value.
            # risk_distance = 2.5 * atr
            # target_risk_pct = 0.05
            # normalized_size = (target_risk_pct / (risk_distance / price)) * base_size
            
            risk_pct = (2.5 * atr) / price
            if risk_pct > 0:
                # Scale multiplier: if risk is 5%, mult=1.0. If risk is 10%, mult=0.5.
                vol_multiplier = 0.05 / risk_pct
                vol_multiplier = max(0.5, min(1.5, vol_multiplier))
                logger.info(f"Volatility scaling: ATR={atr:.2f} Risk={risk_pct*100:.1f}% -> Multiplier {vol_multiplier:.2f}x")

        # Equal-weight across remaining position slots
        slots_remaining = max(1, max_positions - existing_positions)
        buy_size = min(cash / slots_remaining, (trade_limit / slots_remaining) * vol_multiplier)

        # Dynamic per-asset position cap
        dynamic_max_position = reference_value / max(1, max_positions)
        buy_size = min(buy_size, dynamic_max_position)

        return buy_size

    def calculate_fees(self, trade_value: float, is_round_trip: bool = True) -> float:
        """Calculate trading fees.

        This implements the fee calculation from trading_bot.py lines 577-585.

        Args:
            trade_value: Value of the trade in USD
            is_round_trip: If True, calculate full round-trip fee (buy + sell)
                          If False, calculate single-leg fee

        Returns:
            Fee amount in USD
        """
        fee_pct = float(self.config.round_trip_fee_pct)

        if is_round_trip:
            return trade_value * fee_pct
        else:
            # Single leg is half of round-trip
            return trade_value * (fee_pct / 2.0)

    def can_open_position(
        self,
        portfolio_value: float,
        proposed_value: float,
        price: float,
        existing_positions: int,
        max_positions: int,
        executor_id: str = "default",
        current_asset_value: float = 0.0
    ) -> tuple[bool, str]:
        """Check if opening a new position is allowed.

        Combines multiple risk checks:
        - Circuit breakers (drawdown limits)
        - Position count limits
        - Minimum order size
        - Per-asset concentration limits

        Args:
            portfolio_value: Total portfolio value
            proposed_value: Proposed position size in USD
            price: Asset price
            existing_positions: Number of current positions
            max_positions: Maximum allowed positions
            executor_id: Executor identifier
            current_asset_value: Current value held in this asset (for position cap check)

        Returns:
            (allowed, reason) tuple:
                - allowed: True if position can be opened
                - reason: Human-readable explanation (empty if allowed)
        """
        # 1. Check circuit breakers
        allowed, reason = self.check_circuit_breakers(portfolio_value, executor_id)
        if not allowed:
            return False, reason

        # 2. Check position count limit (allow adding to existing positions)
        is_existing_position = current_asset_value > 0
        if existing_positions >= max_positions and not is_existing_position:
            return False, f"At maximum positions ({existing_positions}/{max_positions})"

        # 3. Check minimum order size
        min_order = float(self.config.min_order_usd)
        if proposed_value < min_order:
            return False, f"Position size ${proposed_value:.2f} below minimum ${min_order}"

        # 4. Check per-asset concentration limit (dynamic max position)
        dynamic_max_position = portfolio_value / max(1, max_positions)
        total_asset_value = current_asset_value + proposed_value

        if total_asset_value > dynamic_max_position:
            # Position would exceed dynamic max
            remaining_room = max(0, dynamic_max_position - current_asset_value)
            if remaining_room < min_order:
                return False, (
                    f"Asset at ${current_asset_value:,.0f} already at/exceeds "
                    f"dynamic max (${dynamic_max_position:,.0f})"
                )
            # Could open with reduced size
            return True, f"Reduced to ${remaining_room:.2f} to fit dynamic max"

        return True, ""

    def update_drawdown_tracking(
        self,
        portfolio_value: float,
        executor_id: str = "default"
    ) -> None:
        """Update high-water mark and drawdown state.

        This should be called periodically to update peak tracking.
        The actual circuit breaker logic is in check_circuit_breakers().

        Args:
            portfolio_value: Current portfolio value
            executor_id: Executor identifier
        """
        peak = self._get_peak_value(executor_id)

        # Initialize or update peak
        if peak == 0.0 or portfolio_value > peak:
            self._set_peak_value(portfolio_value, executor_id)
            # Clear pause state if we hit new high
            if portfolio_value > peak:
                self._clear_drawdown_pause(executor_id)

    def get_metrics(self, executor_id: str = "default") -> dict:
        """Return current risk metrics for monitoring.

        Args:
            executor_id: Executor identifier

        Returns:
            Dictionary with current risk metrics
        """
        peak = self._get_peak_value(executor_id)
        is_paused = self._drawdown_paused.get(executor_id, False)
        dd_timestamp = self._drawdown_timestamps.get(executor_id)

        metrics = {
            "executor_id": executor_id,
            "peak_value": peak,
            "is_paused": is_paused,
            "max_drawdown_pct": float(self.config.max_drawdown_pct),
            "cooldown_hours": self.config.drawdown_cooldown_hours,
            "max_positions": self.config.max_concurrent_positions,
        }

        if dd_timestamp is not None:
            hours_paused = (time.time() - dd_timestamp) / 3600
            metrics["hours_paused"] = hours_paused

        return metrics

    def get_current_drawdown(
        self,
        portfolio_value: float,
        executor_id: str = "default"
    ) -> float:
        """Calculate current drawdown percentage.

        Args:
            portfolio_value: Current portfolio value
            executor_id: Executor identifier

        Returns:
            Drawdown percentage (0-100)
        """
        peak = self._get_peak_value(executor_id)
        if peak <= 0:
            return 0.0

        return ((peak - portfolio_value) / peak * 100)

    def is_paused(self, executor_id: str = "default") -> bool:
        """Check if trading is currently paused due to drawdown.

        Args:
            executor_id: Executor identifier

        Returns:
            True if paused, False otherwise
        """
        return self._drawdown_paused.get(executor_id, False)

    def reset_drawdown(self, executor_id: str = "default") -> None:
        """Manually reset drawdown tracking (use with caution).

        This is primarily for backtesting or special recovery scenarios.

        Args:
            executor_id: Executor identifier
        """
        self._drawdown_timestamps.pop(executor_id, None)
        self._drawdown_paused[executor_id] = False
        logger.info(f"[{executor_id}] Drawdown tracking manually reset")

    # Private helper methods for peak value tracking

    def _get_peak_value(self, executor_id: str) -> float:
        """Get peak value from state_manager or in-memory cache."""
        if self.state_manager is not None:
            return self.state_manager.load_peak_value(executor_id)
        else:
            return self._peak_values.get(executor_id, 0.0)

    def _set_peak_value(self, value: float, executor_id: str) -> None:
        """Save peak value to state_manager or in-memory cache."""
        if self.state_manager is not None:
            self.state_manager.save_peak_value(value, executor_id)
        else:
            self._peak_values[executor_id] = value

    def _clear_drawdown_pause(self, executor_id: str) -> None:
        """Clear drawdown pause state."""
        self._drawdown_timestamps.pop(executor_id, None)
        self._drawdown_paused[executor_id] = False

        # Also clear from state_manager if available
        if self.state_manager is not None:
            # Need to manually clear from state dict since StateManager
            # doesn't have a dedicated method for this
            state = self.state_manager.load_state()
            dd_key = f"drawdown_since:{executor_id}"
            if dd_key in state:
                state.pop(dd_key)
                self.state_manager.save_state(state)

    def calculate_position_with_existing(
        self,
        portfolio_value: float,
        price: float,
        existing_positions: int,
        max_positions: int,
        current_asset_value: float,
        available_cash: float = None,
        executor_value: float = None,
        atr: float = None
    ) -> tuple[float, str]:
        """Calculate position size considering existing holdings in the asset.

        This combines calculate_position_size with per-asset concentration limits.
        Returns the actual size to buy and a message explaining any adjustments.

        Args:
            portfolio_value: Total portfolio value
            price: Asset price
            existing_positions: Number of current positions
            max_positions: Maximum allowed positions
            current_asset_value: Current USD value held in this specific asset
            available_cash: Available cash
            executor_value: Optional per-executor value
            atr: Average True Range for volatility scaling

        Returns:
            (buy_size, message) tuple:
                - buy_size: Position size in USD (0 if should skip)
                - message: Explanation of sizing decision
        """
        # Calculate base position size
        base_size = self.calculate_position_size(
            portfolio_value=portfolio_value,
            price=price,
            existing_positions=existing_positions,
            max_positions=max_positions,
            available_cash=available_cash,
            executor_value=executor_value,
            atr=atr
        )

        # Apply per-asset concentration limit
        reference_value = executor_value if executor_value is not None else portfolio_value
        dynamic_max_position = reference_value / max(1, max_positions)

        # Check if adding this position would exceed the per-asset limit
        if current_asset_value + base_size > dynamic_max_position:
            # Calculate remaining room
            remaining_room = max(0, dynamic_max_position - current_asset_value)

            min_order = float(self.config.min_order_usd)
            if remaining_room < min_order:
                return 0.0, (
                    f"Position at ${current_asset_value:,.0f} already at/exceeds "
                    f"dynamic max (${dynamic_max_position:,.0f})"
                )

            return remaining_room, (
                f"Capped to ${remaining_room:,.2f} to stay within "
                f"dynamic max ${dynamic_max_position:,.0f}"
            )

        return base_size, "Normal position sizing"
