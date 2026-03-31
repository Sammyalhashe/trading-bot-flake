"""Supertrend strategy: multi-Supertrend consensus with volume confirmation"""
import logging

from core.technical_analysis import TechnicalAnalysis
from config.trading_config import TradingConfig

logger = logging.getLogger(__name__)

# Three Supertrend configs with different sensitivity levels.
# All three must agree for entry, reducing false signals.
# Fast (7,2) catches moves early, Medium (10,3) is standard,
# Slow (20,4) filters out noise. Consensus = high conviction.
SUPERTREND_PARAMS = [
    {"period": 7, "multiplier": 2.0},   # Fast — responsive
    {"period": 10, "multiplier": 3.0},   # Medium — standard
    {"period": 20, "multiplier": 4.0},   # Slow — filters noise
]


class SupertrendStrategy:
    """Supertrend consensus strategy with volume confirmation.

    Entry: all 3 Supertrend indicators agree on direction + volume spike.
    Exit: ATR trailing stop (primary), Supertrend flip (secondary), tiered TP.
    """

    def __init__(self, ta: TechnicalAnalysis, config: TradingConfig):
        self.ta = ta
        self.config = config
        self.name = "supertrend"

    def _get_supertrend_consensus(self, df) -> tuple[int, int]:
        """Calculate consensus across all Supertrend indicators.

        Returns:
            tuple: (bullish_count, bearish_count) out of len(SUPERTREND_PARAMS)
        """
        bullish = 0
        bearish = 0
        for params in SUPERTREND_PARAMS:
            st = self.ta.calculate_supertrend(
                df, period=params["period"], multiplier=params["multiplier"]
            )
            if st is None:
                continue
            if st["direction"] == 1:
                bullish += 1
            else:
                bearish += 1
        return bullish, bearish

    def _has_volume_spike(self, df, lookback: int = 20, threshold: float = 1.5) -> bool:
        """Check if the current candle has a volume spike relative to recent average."""
        if df is None or len(df) < lookback + 1:
            return True  # No volume data — don't block entry
        if 'volume' not in df.columns:
            return True
        avg_vol = df['volume'].iloc[-(lookback + 1):-1].mean()
        if avg_vol <= 0:
            return True
        current_vol = df['volume'].iloc[-1]
        return current_vol >= avg_vol * threshold

    def should_skip_regime(self, market_regime: str, full_regime: str) -> bool:
        return full_regime in ("BEAR", "STRONG_BEAR")

    def scan_entry(self, asset: str, product_id: str, df, market_regime: str, full_regime: str) -> dict | None:
        if self.should_skip_regime(market_regime, full_regime):
            return None

        # All 3 Supertrend indicators must agree: bullish
        bullish, bearish = self._get_supertrend_consensus(df)
        if bullish < len(SUPERTREND_PARAMS):
            return None

        # Volume confirmation: current candle > 1.5x 20-period average
        if not self._has_volume_spike(df):
            return None

        # RSI filter: skip overbought
        rsi = self.ta.calculate_rsi(df)
        if rsi is not None and rsi > float(self.config.rsi_overbought):
            return None

        # 24h volume floor
        min_volume = float(self.config.min_24h_volume_usd)
        if df is not None and len(df) >= 24:
            volume_24h = df['volume'].iloc[-24:].sum()
            close_price = df['close'].iloc[-1]
            usd_volume_24h = volume_24h * close_price
            if usd_volume_24h < min_volume:
                return None

        momentum = self.ta.get_momentum_ranking(df, self.config.momentum_window_hours)
        return {"asset": asset, "product_id": product_id, "score": momentum}

    def rank_candidates(self, candidates: list[dict]) -> list[dict]:
        return sorted(candidates, key=lambda x: x["score"], reverse=True)

    def check_exit(self, asset: str, product_id: str, df, price: float,
                   entry: float, hwm: float, tp_flags: dict,
                   state: dict, entry_key: str) -> tuple[bool, float, str, dict]:
        sell_trigger = False
        sell_ratio = 1.0
        reason = ""

        tp1_pct = float(self.config.take_profit_1_pct)
        tp1_ratio = float(self.config.take_profit_1_sell_ratio)
        tp2_pct = float(self.config.take_profit_2_pct)
        tp2_ratio = float(self.config.take_profit_2_sell_ratio)

        # Priority 1: Take-Profit Level 2
        if not tp_flags.get("tp2_hit", False) and price >= entry * (1 + tp2_pct):
            sell_trigger = True
            sell_ratio = tp2_ratio
            reason = f"Take-profit level 2 for {asset} (${price:,.2f} >= ${entry * (1 + tp2_pct):,.2f}, selling {tp2_ratio*100:.0f}%)"
            tp_flags["tp2_hit"] = True
            tp_flags["tp1_hit"] = True

        # Priority 2: Take-Profit Level 1
        elif not tp_flags.get("tp1_hit", False) and price >= entry * (1 + tp1_pct):
            sell_trigger = True
            sell_ratio = tp1_ratio
            reason = f"Take-profit level 1 for {asset} (${price:,.2f} >= ${entry * (1 + tp1_pct):,.2f}, selling {tp1_ratio*100:.0f}%)"
            tp_flags["tp1_hit"] = True

        # Priority 3: ATR trailing stop
        elif True:
            atr = self.ta.calculate_atr(df)
            if atr is not None and price > 0:
                atr_stop = 2.5 * atr / price
                atr_stop = max(0.03, min(0.15, atr_stop))
                effective_trailing_stop = atr_stop
            else:
                effective_trailing_stop = float(self.config.trailing_stop_pct)

            stop_price = hwm * (1 - effective_trailing_stop)

            # After TP1, raise stop floor to entry (breakeven protection)
            if tp_flags.get("tp1_hit", False) and entry > stop_price:
                stop_price = entry

            if price < stop_price:
                sell_trigger = True
                sell_ratio = 1.0
                be_note = " [breakeven]" if tp_flags.get("tp1_hit", False) and stop_price >= entry else ""
                reason = f"Trailing stop for {asset} (${price:,.2f} < stop ${stop_price:,.2f}, ATR stop={effective_trailing_stop*100:.1f}%{be_note})"

        # Priority 4: Supertrend reversal — 2 of 3 indicators flip bearish
        if not sell_trigger and not tp_flags.get("trend_exit_hit", False):
            bullish, bearish = self._get_supertrend_consensus(df)
            if bearish >= 2:
                sell_trigger = True
                sell_ratio = 0.5
                reason = f"Supertrend reversal for {asset} ({bearish}/3 bearish)"
                tp_flags["trend_exit_hit"] = True

        return sell_trigger, sell_ratio, reason, tp_flags
