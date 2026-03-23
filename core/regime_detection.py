"""Market regime detection using dual-signal approach"""
import pandas as pd
import logging
import requests
from decimal import Decimal

logger = logging.getLogger(__name__)


class RegimeDetector:
    """Dual-signal regime detection with BTC macro trend + ETH/BTC ratio"""

    def __init__(
        self,
        technical_analysis,
        ma_short_window: int = 20,
        ma_long_window: int = 50,
        enable_btc_dominance: bool = False
    ):
        """
        Initialize regime detector.

        Args:
            technical_analysis: TechnicalAnalysis instance for MA calculations
            ma_short_window: Short moving average window
            ma_long_window: Long moving average window
            enable_btc_dominance: Whether to fetch BTC dominance from CoinGecko
        """
        self.tech = technical_analysis
        self.ma_short_window = ma_short_window
        self.ma_long_window = ma_long_window
        self.enable_btc_dominance = enable_btc_dominance

    def analyze_btc_trend(self, data_provider, buffer_pct: float = 0.002) -> str:
        """Analyze BTC macro trend using MA crossover.

        Args:
            data_provider: Object with get_market_data() method
            buffer_pct: Buffer percentage to avoid whipsaw (default: 0.2%)

        Returns:
            str: "BULL", "BEAR", or "FLAT"
        """
        try:
            df = data_provider.get_market_data("BTC-USDC", self.ma_long_window)
            s_ma, l_ma = self.tech.analyze_trend(df)

            if s_ma is None or l_ma is None:
                logger.warning("BTC trend: Insufficient data")
                return "FLAT"

            # Apply buffer to prevent whipsaw
            if s_ma > l_ma * (1 + buffer_pct):
                trend = "BULL"
            elif s_ma < l_ma * (1 - buffer_pct):
                trend = "BEAR"
            else:
                trend = "FLAT"

            logger.info(f"BTC Trend: {trend} (SMA{self.ma_short_window}={s_ma:.2f}, SMA{self.ma_long_window}={l_ma:.2f})")
            return trend

        except Exception as e:
            logger.error(f"BTC trend analysis failed: {e}")
            return "FLAT"

    def compute_eth_btc_ratio(self, data_provider, buffer_pct: float = 0.003) -> str | None:
        """Compute ETH/BTC ratio trend to detect altcoin rotation.

        The ETH/BTC ratio indicates whether capital is rotating into altcoins (ETH leading)
        or consolidating into BTC (BTC leading). This is a critical signal for altcoin seasons.

        Args:
            data_provider: Object with get_market_data() method
            buffer_pct: Buffer percentage for ratio (default: 0.3%, wider than BTC trend)

        Returns:
            str: "ETH_LEADING", "BTC_LEADING", or "NEUTRAL_RATIO"
            None: if insufficient data
        """
        try:
            # Fetch both pairs
            eth_df = data_provider.get_market_data("ETH-USDC", self.ma_long_window)
            btc_df = data_provider.get_market_data("BTC-USDC", self.ma_long_window)

            if eth_df is None or btc_df is None:
                logger.warning("ETH/BTC ratio: Missing data for one or both pairs")
                return None

            if len(eth_df) < self.ma_long_window or len(btc_df) < self.ma_long_window:
                logger.warning(f"ETH/BTC ratio: Insufficient data (ETH:{len(eth_df)}, BTC:{len(btc_df)})")
                return None

            # Merge on timestamp to align candles
            merged = pd.merge(
                eth_df[['start', 'close']].rename(columns={'close': 'eth_close'}),
                btc_df[['start', 'close']].rename(columns={'close': 'btc_close'}),
                on='start',
                how='inner'
            )

            if len(merged) < self.ma_long_window:
                logger.warning(f"ETH/BTC ratio: Insufficient aligned data ({len(merged)} rows after merge)")
                return None

            # Calculate ratio
            merged['eth_btc_ratio'] = merged['eth_close'] / merged['btc_close']

            # Apply SMA analysis
            ratio_sma_short = merged['eth_btc_ratio'].rolling(window=self.ma_short_window).mean().iloc[-1]
            ratio_sma_long = merged['eth_btc_ratio'].rolling(window=self.ma_long_window).mean().iloc[-1]

            # Use buffer_pct (default 0.3%, wider than 0.2%) because ratio is noisier
            if ratio_sma_short > ratio_sma_long * (1 + buffer_pct):
                signal = "ETH_LEADING"  # ETH outperforming BTC → altcoin rotation
            elif ratio_sma_short < ratio_sma_long * (1 - buffer_pct):
                signal = "BTC_LEADING"  # BTC outperforming ETH → capital consolidating
            else:
                signal = "NEUTRAL_RATIO"  # No clear rotation

            ratio_current = merged['eth_btc_ratio'].iloc[-1]
            logger.info(f"ETH/BTC Ratio: {ratio_current:.5f} | Signal: {signal}")
            return signal

        except Exception as e:
            logger.error(f"ETH/BTC ratio computation failed: {e}")
            return None

    def get_btc_dominance(self) -> dict | None:
        """Fetch Bitcoin dominance from CoinGecko API.

        Bitcoin dominance (BTC.D) is the percentage of total crypto market cap that Bitcoin represents.
        High dominance (>55%) = capital consolidating in BTC (risk-off for alts)
        Low dominance (<45%) = capital flowing to alts (alt season)

        Returns:
            dict: {"btc_dominance": float, "regime": str} where regime is "BTC_DOMINANT", "ALT_SEASON", or "NEUTRAL"
            None: if API call fails or is unavailable
        """
        if not self.enable_btc_dominance:
            return None

        try:
            url = "https://api.coingecko.com/api/v3/global"
            response = requests.get(url, timeout=10)
            response.raise_for_status()

            data = response.json()["data"]
            btc_d = data["market_cap_percentage"]["btc"]
            total_mcap = data["total_market_cap"]["usd"]

            # Classify dominance regime
            if btc_d > 55:
                regime = "BTC_DOMINANT"  # Reduce alt exposure, favor BTC
            elif btc_d < 45:
                regime = "ALT_SEASON"    # Increase alt exposure
            else:
                regime = "NEUTRAL"

            logger.info(f"BTC Dominance: {btc_d:.2f}% (Total Market Cap: ${total_mcap/1e9:.1f}B) → {regime}")

            return {
                "btc_dominance": btc_d,
                "regime": regime,
                "total_market_cap_usd": total_mcap
            }

        except requests.exceptions.Timeout:
            logger.warning("BTC dominance fetch timed out (CoinGecko API)")
            return None
        except requests.exceptions.RequestException as e:
            logger.warning(f"BTC dominance fetch failed: {e}")
            return None
        except (KeyError, ValueError) as e:
            logger.error(f"BTC dominance parsing error: {e}")
            return None

    def resolve_regime(self, btc_macro: str, rotation_signal: str | None, btc_dominance: dict | None = None) -> str:
        """Combine BTC trend and ETH/BTC rotation into a composite 5-state regime.

        Args:
            btc_macro: "BULL", "BEAR", or "FLAT"
            rotation_signal: "ETH_LEADING", "BTC_LEADING", "NEUTRAL_RATIO", or None
            btc_dominance: Optional dict with {"btc_dominance": float, "regime": str} from CoinGecko

        Returns:
            str: One of "STRONG_BULL", "BULL", "NEUTRAL", "BEAR", "STRONG_BEAR"

        Regime meanings:
            STRONG_BULL: BTC uptrend + alts leading → aggressive longs on alts
            BULL: BTC uptrend + BTC leading → conservative longs, prefer BTC
            NEUTRAL: BTC flat or mixed signals → minimal new positions
            BEAR: BTC downtrend + BTC leading → shorts on alts, defensive
            STRONG_BEAR: BTC downtrend + alts dumping faster → high risk, cash heavy
        """
        # Handle missing rotation signal
        if rotation_signal is None:
            rotation_signal = "NEUTRAL_RATIO"

        # Primary axis: BTC macro trend
        if btc_macro == "BULL":
            if rotation_signal == "ETH_LEADING":
                regime = "STRONG_BULL"
            elif rotation_signal == "BTC_LEADING":
                regime = "BULL"
            else:  # NEUTRAL_RATIO
                regime = "BULL"

        elif btc_macro == "BEAR":
            if rotation_signal == "BTC_LEADING":
                regime = "BEAR"
            elif rotation_signal == "ETH_LEADING":
                regime = "STRONG_BEAR"
            else:  # NEUTRAL_RATIO
                regime = "BEAR"

        else:  # FLAT
            regime = "NEUTRAL"

        # Optional: BTC dominance can strengthen/weaken confidence (Phase 3 enhancement)
        # For now, we just log it without modifying the regime
        if btc_dominance:
            logger.info(f"BTC Dominance: {btc_dominance.get('btc_dominance', 'N/A')}% ({btc_dominance.get('regime', 'N/A')})")

        return regime

    def regime_to_legacy(self, regime: str) -> str:
        """Map 5-state regime to legacy binary BULL/BEAR for backward compatibility.

        This allows existing strategy code to work unchanged during migration.

        Args:
            regime: One of "STRONG_BULL", "BULL", "NEUTRAL", "BEAR", "STRONG_BEAR"

        Returns:
            str: "BULL" or "BEAR"
        """
        if regime in ("STRONG_BULL", "BULL"):
            return "BULL"
        elif regime in ("STRONG_BEAR", "BEAR"):
            return "BEAR"
        else:  # NEUTRAL
            return "BULL"  # Conservative: default to allowing longs, no shorts

    def get_current_regime(self, data_provider, enable_dual_regime: bool = True) -> tuple[str, str]:
        """
        Main entry point - full regime detection.

        Args:
            data_provider: Object with get_market_data() method
            enable_dual_regime: Use dual-signal detection (default: True)

        Returns:
            tuple: (full_regime, legacy_regime) where:
                - full_regime is one of "STRONG_BULL", "BULL", "NEUTRAL", "BEAR", "STRONG_BEAR"
                - legacy_regime is "BULL" or "BEAR"
        """
        # Get BTC macro trend
        btc_macro = self.analyze_btc_trend(data_provider)

        if enable_dual_regime:
            # Get ETH/BTC rotation signal
            rotation_signal = self.compute_eth_btc_ratio(data_provider)

            # Get BTC dominance (if enabled)
            btc_dominance = self.get_btc_dominance()

            # Resolve composite regime
            full_regime = self.resolve_regime(btc_macro, rotation_signal, btc_dominance)
        else:
            # Legacy single-asset mode
            if btc_macro == "BULL":
                full_regime = "BULL"
            elif btc_macro == "BEAR":
                full_regime = "BEAR"
            else:
                full_regime = "NEUTRAL"

        # Map to legacy format
        legacy_regime = self.regime_to_legacy(full_regime)

        logger.info(f"Market Regime: {full_regime} (Legacy: {legacy_regime})")
        return full_regime, legacy_regime
