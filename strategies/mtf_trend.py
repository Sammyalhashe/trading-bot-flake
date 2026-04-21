"""Multi-timeframe trend strategy: 4h bias + 15m entry timing.

Uses higher timeframe (4h, resampled from 1h) for directional bias and
lower timeframe (15m) for precise entry/exit timing. This provides:
- Smoother regime detection (fewer whipsaws than 1h MAs)
- Tighter entries (catching pullback recoveries on 15m)
- Tighter stops (15m ATR instead of 1h ATR)
"""
import logging

from core.technical_analysis import TechnicalAnalysis
from config.trading_config import TradingConfig

logger = logging.getLogger(__name__)


def resample_to_4h(df_1h):
    """Resample 1h OHLCV data to 4h candles.

    Uses standard OHLCV aggregation: first open, max high, min low,
    last close, sum volume. Anchors at midnight UTC.
    """
    if df_1h is None or len(df_1h) < 8:
        return None
    df = df_1h.set_index("start")
    resampled = df.resample("4h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()
    return resampled.reset_index()


class MTFTrendStrategy:
    """Multi-timeframe trend strategy.

    Entry: 4h MAs bullish (directional bias) + 15m pullback recovery
    Exit: 15m ATR trailing stop (tight) + 4h trend reversal
    """

    required_timeframes = {"1h": 100, "15m": 60}

    def __init__(self, ta: TechnicalAnalysis, config: TradingConfig):
        self.ta = ta
        self.config = config
        self.name = "mtf_trend"
        # 4h TA uses same MA windows as config but applied to 4h candles
        self.ta_4h = TechnicalAnalysis(
            ma_short_window=min(ta.ma_short_window, 20),
            ma_long_window=min(ta.ma_long_window, 50),
        )

    def should_skip_regime(self, market_regime: str, full_regime: str) -> bool:
        return full_regime == "NEUTRAL"

    def _get_4h_bias(self, df_1h):
        """Get directional bias from 4h resampled data.

        Returns: "BULL", "BEAR", or None if insufficient data.
        """
        df_4h = resample_to_4h(df_1h)
        if df_4h is None:
            return None, None, None
        ma_s, ma_l = self.ta_4h.analyze_trend(df_4h)
        if ma_s is None or ma_l is None:
            return None, None, None
        if ma_s > ma_l * 1.002:
            return "BULL", ma_s, ma_l
        elif ma_s < ma_l * 0.998:
            return "BEAR", ma_s, ma_l
        return "FLAT", ma_s, ma_l

    def scan_entry(self, asset, product_id, market_data, market_regime, full_regime):
        df_1h = market_data.get("1h")
        df_15m = market_data.get("15m")
        if df_1h is None or df_15m is None:
            return None

        # BEAR regime: skip (same as trend_following default)
        if market_regime == "BEAR" and self.config.bear_position_scale <= 0:
            logger.info(f"[{asset}] MTF skip: BEAR regime, no scale")
            return None

        # 1. 4h directional bias must be bullish
        bias, ma_s, ma_l = self._get_4h_bias(df_1h)
        if bias != "BULL":
            logger.info(f"[{asset}] MTF skip: 4h bias={bias} (need BULL)")
            return None

        # 2. 15m RSI in sweet spot (40-70): not overbought, not deeply oversold
        rsi_15m = self.ta.calculate_rsi(df_15m)
        if rsi_15m is None:
            logger.info(f"[{asset}] MTF skip: 15m RSI unavailable")
            return None
        if rsi_15m > 70:
            logger.info(f"[{asset}] MTF skip: 15m RSI overbought ({rsi_15m:.1f})")
            return None
        if rsi_15m < 40:
            logger.info(f"[{asset}] MTF skip: 15m RSI too low ({rsi_15m:.1f}), not recovering yet")
            return None

        # 3. 15m price above its 20-SMA (pullback has recovered)
        sma_15m = self.ta.calculate_sma(df_15m, period=20)
        price_15m = df_15m["close"].iloc[-1]
        if sma_15m is not None and price_15m < sma_15m:
            logger.info(f"[{asset}] MTF skip: 15m price ${price_15m:,.2f} below SMA ${sma_15m:,.2f}")
            return None

        # 4. 24h volume filter (using 1h data)
        min_volume = float(self.config.min_24h_volume_usd)
        if len(df_1h) >= 24:
            volume_24h = df_1h["volume"].iloc[-24:].sum()
            close_price = df_1h["close"].iloc[-1]
            usd_volume_24h = volume_24h * close_price
            if usd_volume_24h < min_volume:
                logger.info(f"[{asset}] MTF skip: 24h volume ${usd_volume_24h:,.0f} < ${min_volume:,.0f}")
                return None

        momentum = self.ta.get_momentum_ranking(df_1h, self.config.momentum_window_hours)
        logger.info(f"[{asset}] MTF entry signal: 4h BULL (MA{self.ta_4h.ma_short_window}=${ma_s:,.0f} > MA{self.ta_4h.ma_long_window}=${ma_l:,.0f}) | 15m RSI={rsi_15m:.0f} | price above SMA")
        return {"asset": asset, "product_id": product_id, "score": momentum,
                "rsi": rsi_15m, "momentum": momentum}

    def rank_candidates(self, candidates):
        return sorted(candidates, key=lambda x: x["score"], reverse=True)

    def check_exit(self, asset, product_id, market_data, price, entry, hwm,
                   tp_flags, state, entry_key):
        df_1h = market_data.get("1h")
        df_15m = market_data.get("15m")
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
            reason = f"MTF TP2 for {asset} (${price:,.2f} >= ${entry * (1 + tp2_pct):,.2f}, selling {tp2_ratio*100:.0f}%)"
            tp_flags["tp2_hit"] = True
            tp_flags["tp1_hit"] = True

        # Priority 2: Take-Profit Level 1
        elif not tp_flags.get("tp1_hit", False) and price >= entry * (1 + tp1_pct):
            sell_trigger = True
            sell_ratio = tp1_ratio
            reason = f"MTF TP1 for {asset} (${price:,.2f} >= ${entry * (1 + tp1_pct):,.2f}, selling {tp1_ratio*100:.0f}%)"
            tp_flags["tp1_hit"] = True

        # Priority 3: 15m ATR trailing stop (tighter than 1h)
        elif df_15m is not None:
            atr = self.ta.calculate_atr(df_15m)
            if atr is not None and price > 0:
                # 2.0x multiplier (vs 2.5x on 1h), tighter clamp range
                atr_stop = 2.0 * atr / price
                atr_stop = max(0.02, min(0.10, atr_stop))
            else:
                atr_stop = float(self.config.trailing_stop_pct)

            stop_price = hwm * (1 - atr_stop)

            # After TP1, raise stop floor to entry (breakeven protection)
            if tp_flags.get("tp1_hit", False) and entry > stop_price:
                stop_price = entry

            if price < stop_price:
                sell_trigger = True
                sell_ratio = 1.0
                be_note = " [breakeven]" if tp_flags.get("tp1_hit", False) and stop_price >= entry else ""
                reason = f"MTF trailing stop for {asset} (${price:,.2f} < stop ${stop_price:,.2f}, HWM=${hwm:,.2f}, 15m ATR stop={atr_stop*100:.1f}%{be_note})"

        # Priority 4: 4h trend reversal — partial exit
        if not sell_trigger and not tp_flags.get("trend_exit_hit", False) and df_1h is not None:
            pnl_pct = (price - entry) / entry
            fee_floor = float(self.config.round_trip_fee_pct) * 1.5
            bias, _, _ = self._get_4h_bias(df_1h)
            if bias == "BEAR":
                if pnl_pct > fee_floor:
                    sell_trigger = True
                    sell_ratio = 0.5
                    reason = f"MTF 4h reversal for {asset} (PnL {pnl_pct*100:+.2f}% > fee floor {fee_floor*100:.2f}%)"
                    tp_flags["trend_exit_hit"] = True
                else:
                    logger.info(f"[{asset}] MTF 4h reversal deferred: PnL {pnl_pct*100:+.2f}% below fee floor {fee_floor*100:.2f}%")

        return sell_trigger, sell_ratio, reason, tp_flags
