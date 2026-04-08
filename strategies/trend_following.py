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

    def _log_skip(self, asset, reason, **indicators):
        parts = [f"[{asset}] Skip: {reason}"]
        if indicators:
            detail = " | ".join(f"{k}={v}" for k, v in indicators.items())
            parts.append(f"[{detail}]")
        logger.info(" ".join(parts))

    def should_skip_regime(self, market_regime: str, full_regime: str) -> bool:
        return full_regime == "NEUTRAL"

    def _get_rsi_limit(self, full_regime: str) -> float:
        """Return regime-specific RSI overbought threshold."""
        regime_map = {
            "STRONG_BULL": float(self.config.rsi_overbought_strong_bull),
            "BULL": float(self.config.rsi_overbought_bull),
            "NEUTRAL": float(self.config.rsi_overbought_neutral),
            "BEAR": float(self.config.rsi_overbought_bear),
        }
        return regime_map.get(full_regime, float(self.config.rsi_overbought))

    def scan_entry(self, asset: str, product_id: str, df, market_regime: str, full_regime: str) -> dict | None:
        ma_s, ma_l = self.ta.analyze_trend(df)

        # BEAR regime: use momentum+RSI entry (MA crossover can't fire in BEAR)
        if market_regime == "BEAR":
            if self.config.bear_position_scale > 0:
                return self._bear_momentum_entry(asset, product_id, df)
            elif self.config.allow_btc_in_bear and asset == "BTC":
                # Legacy BTC exemption (requires MA crossover — rarely triggers)
                if ma_s and ma_l and ma_s > ma_l * 1.002 and self.ta.is_crossover_confirmed(df, "bull"):
                    logger.info(f"BTC bear-market exemption triggered (hedging strategy)")
                    return self._standard_entry_checks(asset, product_id, df, full_regime)
            self._log_skip(asset, "BEAR regime, no scale or BTC exemption")
            return None

        # NEUTRAL: skip entries
        if full_regime == "NEUTRAL":
            self._log_skip(asset, "NEUTRAL regime")
            return None

        # BULL: standard MA crossover entry
        if not (ma_s and ma_l and ma_s > ma_l * 1.002):
            threshold = f"${ma_l * 1.002:,.0f}" if ma_l else "N/A"
            self._log_skip(asset, "MA crossover not triggered",
                           ma_s=f"${ma_s:,.0f}" if ma_s else "N/A",
                           ma_l=f"${ma_l:,.0f}" if ma_l else "N/A",
                           threshold=threshold)
            return None
        if not self.ta.is_crossover_confirmed(df, "bull"):
            self._log_skip(asset, "MA crossover not confirmed (need 3 bars)")
            return None

        return self._standard_entry_checks(asset, product_id, df, full_regime)

    def _standard_entry_checks(self, asset: str, product_id: str, df, full_regime: str = "BULL") -> dict | None:
        """Volume, RSI, and momentum checks shared by all entry paths."""
        min_volume = float(self.config.min_24h_volume_usd)
        if df is not None and len(df) >= 24:
            volume_24h = df['volume'].iloc[-24:].sum()
            close_price = df['close'].iloc[-1]
            usd_volume_24h = volume_24h * close_price
            if usd_volume_24h < min_volume:
                self._log_skip(asset, "24h volume too low",
                               volume=f"${usd_volume_24h:,.0f}",
                               min=f"${min_volume:,.0f}")
                return None

        rsi = self.ta.calculate_rsi(df)
        rsi_limit = self._get_rsi_limit(full_regime)
        if rsi is not None and rsi > rsi_limit:
            self._log_skip(asset, "RSI overbought",
                           rsi=f"{rsi:.1f}",
                           limit=f"{rsi_limit:.0f}",
                           regime=full_regime)
            return None

        momentum = self.ta.get_momentum_ranking(df, self.config.momentum_window_hours)
        return {"asset": asset, "product_id": product_id, "score": momentum,
                "rsi": rsi, "momentum": momentum}

    def _bear_momentum_entry(self, asset: str, product_id: str, df) -> dict | None:



        """Entry signal for BEAR regime: momentum + RSI instead of MA crossover.

        Uses short-term momentum (>2% in 24h) and RSI not overbought as entry
        criteria. This allows catching rallies during bear markets since the
        standard MA crossover can't fire when regime MAs are bearish.
        """
        momentum = self.ta.get_momentum_ranking(df, self.config.momentum_window_hours)
        if momentum < 2.0:  # Require >2% 24h momentum to enter in BEAR
            self._log_skip(asset, "BEAR momentum too low",
                           momentum=f"{momentum:+.1f}%", required=">2.0%")
            return None

        rsi = self.ta.calculate_rsi(df)
        if rsi is None:
            self._log_skip(asset, "RSI unavailable (BEAR momentum)")
            return None
        rsi_limit = self._get_rsi_limit("BEAR")
        if rsi > rsi_limit:
            self._log_skip(asset, "RSI overbought (BEAR momentum)",
                           rsi=f"{rsi:.1f}",
                           limit=f"{rsi_limit:.0f}")
            return None
        if rsi < 35:  # Skip deeply oversold — likely a dump, not a rally
            self._log_skip(asset, "RSI deeply oversold (BEAR momentum)",
                           rsi=f"{rsi:.1f}", floor="35")
            return None

        # Volume filter
        min_volume = float(self.config.min_24h_volume_usd)
        if df is not None and len(df) >= 24:
            volume_24h = df['volume'].iloc[-24:].sum()
            close_price = df['close'].iloc[-1]
            usd_volume_24h = volume_24h * close_price
            if usd_volume_24h < min_volume:
                self._log_skip(asset, "24h volume too low (BEAR momentum)",
                               volume=f"${usd_volume_24h:,.0f}",
                               min=f"${min_volume:,.0f}")
                return None

        logger.info(f"Bear momentum entry: {asset} momentum={momentum:+.1f}% RSI={rsi:.0f}")
        return {"asset": asset, "product_id": product_id, "score": momentum,
                "rsi": rsi, "momentum": momentum}

    def rank_candidates(self, candidates: list[dict]) -> list[dict]:
        return sorted(candidates, key=lambda x: x["score"], reverse=True)

    def check_exit(self, asset: str, product_id: str, df, price: float,
                   entry: float, hwm: float, tp_flags: dict,
                   state: dict, entry_key: str) -> tuple[bool, float, str, dict]:
        sell_trigger = False
        current_price = df["close"].iloc[-1]
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
        # Only exit if gain exceeds 1.5× round-trip fees; otherwise let trailing stop handle it
        if not sell_trigger and not tp_flags.get("trend_exit_hit", False):
            pnl_pct = (price - entry) / entry
            fee_floor = float(self.config.round_trip_fee_pct) * 1.5
            ma_s, ma_l = self.ta.analyze_trend(df)
            if ma_s and ma_l and (ma_s < ma_l * 0.998 or current_price < ma_l * 0.99):
                if pnl_pct > fee_floor:
                    sell_trigger = True
                    sell_ratio = 0.5
                    reason = f"Trend-exit (50%) triggered for {asset} (PnL {pnl_pct*100:+.2f}% > fee floor {fee_floor*100:.2f}%)"
                    tp_flags["trend_exit_hit"] = True
                else:
                    logger.info(f"[{asset}] Trend-exit deferred: PnL {pnl_pct*100:+.2f}% below fee floor {fee_floor*100:.2f}%, deferring to trailing stop")

        return sell_trigger, sell_ratio, reason, tp_flags
