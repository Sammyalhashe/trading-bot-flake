"""Trend-following strategy: MA crossover with ATR trailing stops"""
import logging

from core.technical_analysis import TechnicalAnalysis
from config.trading_config import TradingConfig

logger = logging.getLogger(__name__)


class TrendFollowingStrategy:
    """Original trend-following strategy extracted from run_executor_strategy."""

    def __init__(self, ta: TechnicalAnalysis, config: TradingConfig):
        self.ta = ta
        self.config = config
        self.name = "trend_following"

    def should_skip_regime(self, market_regime: str, full_regime: str) -> bool:
        return full_regime == "NEUTRAL"

    def scan_entry(self, asset: str, product_id: str, df, market_regime: str, full_regime: str) -> dict | None:
        ma_s, ma_l = self.ta.analyze_trend(df)

        # BTC exemption: allow BTC buys in BEAR if configured
        allow_buy = market_regime == "BULL" or (self.config.allow_btc_in_bear and asset == "BTC")
        # Skip all new entries in NEUTRAL regime
        if full_regime == "NEUTRAL":
            allow_buy = False

        if not (ma_s and ma_l and ma_s > ma_l * 1.002 and allow_buy):
            return None
        if not self.ta.is_crossover_confirmed(df, "bull"):
            return None

        # Log BTC bear-market exemption
        if market_regime == "BEAR" and asset == "BTC" and self.config.allow_btc_in_bear:
            logger.info(f"BTC bear-market exemption triggered (hedging strategy)")

        # Volume filter
        min_volume = float(self.config.min_24h_volume_usd)
        if df is not None and len(df) >= 24:
            volume_24h = df['volume'].iloc[-24:].sum()
            close_price = df['close'].iloc[-1]
            usd_volume_24h = volume_24h * close_price
            if usd_volume_24h < min_volume:
                return None

        # RSI filter: skip overbought
        rsi = self.ta.calculate_rsi(df)
        if rsi is not None and rsi > float(self.config.rsi_overbought):
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
        trailing_stop = float(self.config.trailing_stop_pct)

        # Priority 1: Take-Profit Level 2
        if not tp_flags.get("tp2_hit", False) and price >= entry * (1 + tp2_pct):
            sell_trigger = True
            sell_ratio = tp2_ratio
            reason = f"Take-profit level 2 triggered for {asset} (price ${price:,.2f} >= ${entry * (1 + tp2_pct):,.2f}, selling {tp2_ratio*100:.0f}%)"
            tp_flags["tp2_hit"] = True
            tp_flags["tp1_hit"] = True

        # Priority 2: Take-Profit Level 1
        elif not tp_flags.get("tp1_hit", False) and price >= entry * (1 + tp1_pct):
            sell_trigger = True
            sell_ratio = tp1_ratio
            reason = f"Take-profit level 1 triggered for {asset} (price ${price:,.2f} >= ${entry * (1 + tp1_pct):,.2f}, selling {tp1_ratio*100:.0f}%)"
            tp_flags["tp1_hit"] = True

        # Priority 3: ATR trailing stop
        elif True:
            atr = self.ta.calculate_atr(df)
            if atr is not None and price > 0:
                atr_stop = 2.5 * atr / price
                atr_stop = max(0.03, min(0.15, atr_stop))
                effective_trailing_stop = atr_stop
            else:
                effective_trailing_stop = trailing_stop

            stop_price = hwm * (1 - effective_trailing_stop)

            # After TP1, raise stop floor to entry (breakeven protection)
            if tp_flags.get("tp1_hit", False) and entry > stop_price:
                stop_price = entry

            if price < stop_price:
                sell_trigger = True
                sell_ratio = 1.0
                be_note = " [breakeven]" if tp_flags.get("tp1_hit", False) and stop_price >= entry else ""
                reason = f"Trailing stop-loss triggered for {asset} (price ${price:,.2f} < stop ${stop_price:,.2f}, HWM=${hwm:,.2f}, ATR stop={effective_trailing_stop*100:.1f}%{be_note})"

        # Priority 4: Trend-exit (MA cross) — fires once per entry
        if not sell_trigger and not tp_flags.get("trend_exit_hit", False):
            ma_s, ma_l = self.ta.analyze_trend(df)
            if ma_s and ma_l and ma_s < ma_l * 0.998:
                sell_trigger = True
                sell_ratio = 0.5
                reason = f"Trend-exit (50%) triggered for {asset}"
                tp_flags["trend_exit_hit"] = True

        return sell_trigger, sell_ratio, reason, tp_flags
