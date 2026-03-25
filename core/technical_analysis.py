"""Technical indicator calculations"""
import pandas as pd
from decimal import Decimal
import logging

logger = logging.getLogger(__name__)


class TechnicalAnalysis:
    """Technical indicator calculations for trading strategy"""

    def __init__(self, ma_short_window: int = 20, ma_long_window: int = 50):
        """
        Initialize technical analysis calculator.

        Args:
            ma_short_window: Short moving average window
            ma_long_window: Long moving average window
        """
        self.ma_short_window = ma_short_window
        self.ma_long_window = ma_long_window

    def analyze_trend(self, df: pd.DataFrame) -> tuple[float | None, float | None]:
        """Compute short and long moving averages for trend detection.

        The bot uses MA crossover to determine market direction:
          - short_MA > long_MA * 1.002 → uptrend (BUY signal, 0.2% buffer avoids noise)
          - short_MA < long_MA * 0.998 → downtrend (SELL/SHORT signal)
        The 0.2% buffer prevents whipsawing on flat markets.

        Args:
            df: DataFrame with 'close' column

        Returns:
            tuple: (short_ma, long_ma) or (None, None) if insufficient data
        """
        if df is None or len(df) < self.ma_long_window:
            return None, None
        s_ma = df['close'].ewm(span=self.ma_short_window, adjust=False).mean().iloc[-1]
        l_ma = df['close'].ewm(span=self.ma_long_window, adjust=False).mean().iloc[-1]
        return s_ma, l_ma

    def is_crossover_confirmed(self, df: pd.DataFrame, direction: str = "bull", buffer: float = 1.002) -> bool:
        """Check if MA crossover held for both current and previous bar.

        Reduces false signals by requiring the crossover to persist for two
        consecutive bars before generating an entry signal.

        Args:
            df: DataFrame with 'close' column
            direction: "bull" (short > long) or "bear" (short < long)
            buffer: Multiplier buffer (default 0.2% = 1.002)

        Returns:
            bool: True if crossover confirmed on both bars
        """
        if df is None or len(df) < self.ma_long_window + 1:
            return False
        s_ema = df['close'].ewm(span=self.ma_short_window, adjust=False).mean()
        l_ema = df['close'].ewm(span=self.ma_long_window, adjust=False).mean()
        if direction == "bull":
            current = s_ema.iloc[-1] > l_ema.iloc[-1] * buffer
            previous = s_ema.iloc[-2] > l_ema.iloc[-2] * buffer
        else:
            inv_buffer = 2 - buffer  # 1.002 -> 0.998
            current = s_ema.iloc[-1] < l_ema.iloc[-1] * inv_buffer
            previous = s_ema.iloc[-2] < l_ema.iloc[-2] * inv_buffer
        return current and previous

    def calculate_rsi(self, df: pd.DataFrame, period: int = 14) -> float | None:
        """Calculate RSI (Relative Strength Index) from candle close prices.

        RSI measures momentum on a 0–100 scale:
          - RSI > 70 → overbought (price rose too fast, likely to pull back)
          - RSI < 30 → oversold  (price dropped too fast, likely to bounce)

        Formula:
          RS  = avg_gain / avg_loss   (over `period` bars)
          RSI = 100 - 100/(1 + RS)

        When gains dominate, RS is large → RSI approaches 100.
        When losses dominate, RS is small → RSI approaches 0.

        Args:
            df: DataFrame with 'close' column
            period: RSI period (default: 14)

        Returns:
            float: RSI value, or None if insufficient data
        """
        if df is None or len(df) < period + 1:
            return None
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.rolling(window=period).mean()
        avg_loss = loss.rolling(window=period).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi.iloc[-1]

    def calculate_atr(self, df: pd.DataFrame, period: int = 14) -> float | None:
        """Calculate ATR (Average True Range) from candle OHLC data.

        ATR measures volatility — the average size of recent price swings.
        Used to set trailing stops that adapt to current market conditions:
          - High ATR → wider stop (volatile market, avoid getting stopped out by noise)
          - Low ATR  → tighter stop (calm market, protect gains more aggressively)

        True Range for each bar is the largest of:
          1. high - low                (intra-bar range)
          2. |high - previous close|   (gap up)
          3. |low  - previous close|   (gap down)

        ATR = simple moving average of True Range over `period` bars.

        Args:
            df: DataFrame with 'high', 'low', 'close' columns
            period: ATR period (default: 14)

        Returns:
            float: ATR value, or None if insufficient data
        """
        if df is None or len(df) < period + 1:
            return None
        high = df['high']
        low = df['low']
        prev_close = df['close'].shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean().iloc[-1]
        return atr

    def get_momentum_ranking(self, df: pd.DataFrame, window_hours: int = 24) -> float:
        """Calculate momentum as percentage change over window.

        Args:
            df: DataFrame with 'close' column
            window_hours: Lookback window in hours (default: 24)

        Returns:
            float: Momentum percentage, or 0.0 if insufficient data
        """
        if df is None or len(df) < window_hours + 1:
            return 0.0
        curr = df['close'].iloc[-1]
        hist = df['close'].iloc[-(window_hours + 1)]
        return ((curr - hist) / hist) * 100 if hist != 0 else 0.0

    def calculate_bollinger_bands(self, df: pd.DataFrame, period: int = 20, num_std: float = 2.0) -> tuple[float, float, float] | None:
        """Calculate Bollinger Bands (middle, upper, lower).

        Bollinger Bands measure volatility around a moving average:
          - Middle = SMA(period)
          - Upper  = Middle + num_std * std(period)
          - Lower  = Middle - num_std * std(period)

        Price below the lower band suggests oversold conditions.

        Args:
            df: DataFrame with 'close' column
            period: SMA period (default: 20)
            num_std: Number of standard deviations (default: 2.0)

        Returns:
            tuple: (middle, upper, lower) or None if insufficient data
        """
        if df is None or len(df) < period:
            return None
        close = df['close'].iloc[-period:]
        middle = close.mean()
        std = close.std()
        upper = middle + num_std * std
        lower = middle - num_std * std
        return middle, upper, lower

    def calculate_sma(self, df: pd.DataFrame, period: int = 20) -> float | None:
        """Calculate Simple Moving Average.

        Args:
            df: DataFrame with 'close' column
            period: SMA period (default: 20)

        Returns:
            float: SMA value, or None if insufficient data
        """
        if df is None or len(df) < period:
            return None
        return df['close'].iloc[-period:].mean()
