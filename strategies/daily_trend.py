"""Daily trend filter on a small universe (default: BTC above its 50-day average).

Decisions use closed daily candles only, so the live in-progress candle never moves a position.
The strategy is its own regime filter: it ignores the hourly market regime entirely.
"""
import logging
import time

import pandas as pd

logger = logging.getLogger(__name__)

DAY_SECONDS = 86400


def parse_filter(spec: str):
    """'sma:50' -> ('sma', 50); 'ema:21:55' -> ('ema', 21, 55)."""
    kind, *windows = spec.lower().split(":")
    if kind == "sma" and len(windows) == 1:
        return "sma", int(windows[0])
    if kind == "ema" and len(windows) == 2:
        return "ema", int(windows[0]), int(windows[1])
    raise ValueError(f"DAILY_TREND_FILTER must be sma:N or ema:FAST:SLOW, got {spec!r}")


class DailyTrendStrategy:
    name = "daily_trend"
    uses_hourly_regime = False

    def __init__(self, ta, config):
        self.config = config
        self.assets = {a.strip().upper() for a in config.daily_trend_assets.split(",") if a.strip()}
        self.filter = parse_filter(config.daily_trend_filter)
        self.buffer = float(config.daily_trend_buffer_pct)
        self.confirm = max(1, int(config.daily_trend_confirm_days))
        self.stop_pct = float(config.daily_trend_stop_pct)
        self.required_timeframes = {"1d": max(self.filter[1:]) + self.confirm + 10}

    def should_skip_regime(self, market_regime: str, full_regime: str) -> bool:
        return False

    def _closed(self, df: pd.DataFrame) -> pd.DataFrame:
        column = "start" if "start" in df.columns else "timestamp"
        day_start = pd.to_datetime(df[column]).astype("int64") // 10**9
        return df[day_start + DAY_SECONDS <= time.time()]

    def _trend(self, df):
        """(close vs reference) for the last `confirm` closed days, or None without enough history."""
        if df is None:
            return None
        closes = self._closed(df)["close"].astype(float).reset_index(drop=True)
        if self.filter[0] == "sma":
            x, ref = closes, closes.rolling(self.filter[1]).mean()
        else:
            x = closes.ewm(span=self.filter[1], adjust=False).mean()
            ref = closes.ewm(span=self.filter[2], adjust=False).mean()
        x, ref = x.iloc[-self.confirm:], ref.iloc[-self.confirm:]
        if len(ref) < self.confirm or ref.isna().any():
            return None
        return x, ref

    def _label(self):
        return f"SMA{self.filter[1]}" if self.filter[0] == "sma" else f"EMA{self.filter[1]}/{self.filter[2]}"

    def scan_entry(self, asset, product_id, market_data, market_regime, full_regime):
        if asset.upper() not in self.assets:
            return None
        trend = self._trend(market_data.get("1d"))
        if trend is None:
            return None
        x, ref = trend
        if not (x > ref * (1 + self.buffer)).all():
            return None
        strength = float(x.iloc[-1] / ref.iloc[-1] - 1) * 100
        logger.info(f"Daily trend entry: {asset} {self._label()} {strength:+.1f}% above reference")
        return {"asset": asset, "product_id": product_id, "score": strength, "rsi": None, "momentum": strength}

    def rank_candidates(self, candidates):
        return sorted(candidates, key=lambda c: c["score"], reverse=True)

    def check_exit(self, asset, product_id, market_data, price, entry, hwm, tp_flags, state, entry_key,
                   round_trip_fee_pct=None):
        if asset.upper() not in self.assets:
            return False, 0.0, "", tp_flags
        if self.stop_pct > 0 and price < hwm * (1 - self.stop_pct):
            return (True, 1.0, f"Trailing stop-loss triggered for {asset} (catastrophe stop: price ${price:,.2f} "
                    f"< {self.stop_pct:.0%} below HWM ${hwm:,.2f})", tp_flags)
        trend = self._trend(market_data.get("1d"))
        if trend is None:
            return False, 0.0, "", tp_flags
        x, ref = trend
        if (x < ref * (1 - self.buffer)).all():
            return (True, 1.0, f"Trend-exit (daily {self._label()}) for {asset}: "
                    f"{float(x.iloc[-1]):,.2f} < reference {float(ref.iloc[-1]):,.2f}", tp_flags)
        return False, 0.0, "", tp_flags
