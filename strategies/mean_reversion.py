"""Mean-reversion strategy: RSI oversold + Bollinger Band entry"""
import time
import logging

from core.technical_analysis import TechnicalAnalysis
from config.trading_config import TradingConfig

logger = logging.getLogger(__name__)


class MeanReversionStrategy:
    """Mean-reversion strategy: buy oversold assets, exit at SMA."""

    required_timeframes = {"1h": 55}

    def __init__(self, ta: TechnicalAnalysis, config: TradingConfig):
        self.ta = ta
        self.config = config
        self.name = "mean_reversion"

    def should_skip_regime(self, market_regime: str, full_regime: str) -> bool:
        # Skip BEAR and STRONG_BEAR — trends too strong for reversion
        return full_regime in ("BEAR", "STRONG_BEAR")

    def scan_entry(self, asset: str, product_id: str, market_data, market_regime: str, full_regime: str) -> dict | None:
        df = market_data["1h"]
        if self.should_skip_regime(market_regime, full_regime):
            return None

        rsi_threshold = float(self.config.mr_rsi_oversold)
        bb_period = self.config.mr_bollinger_period
        bb_std = self.config.mr_bollinger_std

        # RSI must be oversold
        rsi = self.ta.calculate_rsi(df)
        if rsi is None or rsi >= rsi_threshold:
            return None

        # Price must be below lower Bollinger Band
        bb = self.ta.calculate_bollinger_bands(df, period=bb_period, num_std=bb_std)
        if bb is None:
            return None
        _middle, _upper, lower = bb
        current_price = df['close'].iloc[-1]
        if current_price >= lower:
            return None

        # Volume filter
        min_volume = float(self.config.min_24h_volume_usd)
        if df is not None and len(df) >= 24:
            volume_24h = df['volume'].iloc[-24:].sum()
            close_price = df['close'].iloc[-1]
            usd_volume_24h = volume_24h * close_price
            if usd_volume_24h < min_volume:
                return None

        # Score = RSI (lower = better, so sort ascending later)
        return {"asset": asset, "product_id": product_id, "score": rsi}

    def rank_candidates(self, candidates: list[dict]) -> list[dict]:
        # Most oversold first (lowest RSI)
        return sorted(candidates, key=lambda x: x["score"])

    def check_exit(self, asset: str, product_id: str, market_data, price: float,
                   entry: float, hwm: float, tp_flags: dict,
                   state: dict, entry_key: str) -> tuple[bool, float, str, dict]:
        df = market_data["1h"]
        bb_period = self.config.mr_bollinger_period
        stop_pct = float(self.config.mr_trailing_stop_pct)
        max_candles = self.config.mr_time_exit_candles

        # Exit 1: Price >= 20-period SMA (mean reversion target)
        sma = self.ta.calculate_sma(df, period=bb_period)
        if sma is not None and price >= sma:
            return True, 1.0, f"Mean reversion target reached for {asset} (price ${price:,.2f} >= SMA ${sma:,.2f})", tp_flags

        # Exit 2: Fixed stop from HWM
        stop_price = hwm * (1 - stop_pct)
        if price < stop_price:
            return True, 1.0, f"Mean reversion stop loss for {asset} (price ${price:,.2f} < stop ${stop_price:,.2f})", tp_flags

        # Exit 3: Time-based exit (candle count)
        entry_time = state.get("entry_timestamps", {}).get(entry_key)
        if entry_time is not None:
            elapsed_candles = (time.time() - entry_time) / 3600
            if elapsed_candles >= max_candles:
                return True, 1.0, f"Mean reversion time exit for {asset} ({elapsed_candles:.0f} candles elapsed)", tp_flags

        return False, 0.0, "", tp_flags
