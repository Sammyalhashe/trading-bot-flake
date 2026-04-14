"""Fetch and analyze derivatives market data from OKX for position sizing and entry filtering."""

import logging
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

# OKX public API endpoints (free, no auth, US-accessible)
OKX_FUNDING_HISTORY = "https://www.okx.com/api/v5/public/funding-rate-history"
OKX_OI_VOLUME = "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume"
OKX_LONG_SHORT_RATIO = "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio"

TIMEOUT = 10  # seconds


@dataclass
class FundingSignal:
    avg_rate: float       # 24h average funding rate
    latest_rate: float    # most recent settlement
    signal: str           # EXTREME_NEGATIVE, NEGATIVE, NORMAL, ELEVATED, EXTREME


@dataclass
class OISignal:
    change_pct: float     # 24h % change in open interest
    latest_oi: float      # most recent OI value (USD)
    signal: str           # FALLING, STABLE, RISING


@dataclass
class LSRatioSignal:
    long_ratio: float     # fraction of accounts long (0-1)
    signal: str           # EXTREME_LONG, NEUTRAL, EXTREME_SHORT


@dataclass
class DerivativesSignals:
    funding: FundingSignal | None
    oi: OISignal | None
    ls_ratio: LSRatioSignal | None
    position_modifier: float    # multiplier for position sizing [0.25, 1.25]
    entry_allowed: bool         # False when OI bearish divergence detected
    caution_flags: list[str]    # human-readable warnings


class DerivativesDataProvider:
    """Fetches OKX derivatives data and computes trading signals."""

    # Cache TTLs
    FUNDING_TTL = 4 * 3600     # 4 hours (settles every 8h)
    OI_TTL = 30 * 60           # 30 minutes
    LS_TTL = 15 * 60           # 15 minutes

    def __init__(self, config):
        self.config = config
        self._cache: dict[str, tuple[float, object]] = {}

    def _get_cached(self, key: str, ttl: int):
        entry = self._cache.get(key)
        if entry and (time.time() - entry[0]) < ttl:
            return entry[1]
        return None

    def _set_cache(self, key: str, data):
        self._cache[key] = (time.time(), data)

    def get_funding_rate(self, inst_id: str = "BTC-USD-SWAP") -> FundingSignal | None:
        cache_key = f"funding:{inst_id}"
        cached = self._get_cached(cache_key, self.FUNDING_TTL)
        if cached is not None:
            return cached

        try:
            r = requests.get(
                OKX_FUNDING_HISTORY,
                params={"instId": inst_id, "limit": "3"},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("code") != "0" or not data.get("data"):
                logger.warning(f"OKX funding rate: unexpected response code={data.get('code')}")
                return None

            rates = [float(entry["fundingRate"]) for entry in data["data"]]
            avg_rate = sum(rates) / len(rates)
            latest_rate = rates[0]

            signal = self._classify_funding(avg_rate)
            result = FundingSignal(avg_rate=avg_rate, latest_rate=latest_rate, signal=signal)
            self._set_cache(cache_key, result)
            return result

        except Exception as e:
            logger.warning(f"OKX funding rate fetch failed: {e}")
            return None

    def get_open_interest(self, ccy: str = "BTC") -> OISignal | None:
        cache_key = f"oi:{ccy}"
        cached = self._get_cached(cache_key, self.OI_TTL)
        if cached is not None:
            return cached

        try:
            r = requests.get(
                OKX_OI_VOLUME,
                params={"ccy": ccy, "period": "1H"},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("code") != "0" or not data.get("data"):
                logger.warning(f"OKX OI: unexpected response code={data.get('code')}")
                return None

            # Response format: [[ts, oi, vol], ...] sorted newest first
            entries = data["data"]
            if len(entries) < 24:
                logger.warning(f"OKX OI: only {len(entries)} entries, need 24 for 24h change")
                # Use what we have
                latest_oi = float(entries[0][1])
                oldest_oi = float(entries[-1][1])
            else:
                latest_oi = float(entries[0][1])
                oldest_oi = float(entries[23][1])

            change_pct = ((latest_oi - oldest_oi) / oldest_oi * 100) if oldest_oi > 0 else 0.0

            if change_pct > 10.0:
                signal = "RISING"
            elif change_pct < self.config.derivatives_oi_divergence_pct:
                signal = "FALLING"
            else:
                signal = "STABLE"

            result = OISignal(change_pct=change_pct, latest_oi=latest_oi, signal=signal)
            self._set_cache(cache_key, result)
            return result

        except Exception as e:
            logger.warning(f"OKX OI fetch failed: {e}")
            return None

    def get_long_short_ratio(self, ccy: str = "BTC") -> LSRatioSignal | None:
        cache_key = f"ls:{ccy}"
        cached = self._get_cached(cache_key, self.LS_TTL)
        if cached is not None:
            return cached

        try:
            r = requests.get(
                OKX_LONG_SHORT_RATIO,
                params={"ccy": ccy, "period": "1H"},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("code") != "0" or not data.get("data"):
                logger.warning(f"OKX L/S ratio: unexpected response code={data.get('code')}")
                return None

            # Response format: [[ts, ratio], ...] where ratio = long_accounts/short_accounts
            # e.g. ratio=0.73 means 73% of accounts are long
            # Note: OKX returns this as a decimal (0-1 range already)
            long_ratio = float(data["data"][0][1])

            if long_ratio > 0.65:
                signal = "EXTREME_LONG"
            elif long_ratio < 0.35:
                signal = "EXTREME_SHORT"
            else:
                signal = "NEUTRAL"

            result = LSRatioSignal(long_ratio=long_ratio, signal=signal)
            self._set_cache(cache_key, result)
            return result

        except Exception as e:
            logger.warning(f"OKX L/S ratio fetch failed: {e}")
            return None

    def _classify_funding(self, rate: float) -> str:
        if rate < -self.config.derivatives_funding_high:
            return "EXTREME_NEGATIVE"
        elif rate < -0.0001:
            return "NEGATIVE"
        elif rate <= 0.0003:
            return "NORMAL"
        elif rate <= self.config.derivatives_funding_high:
            return "ELEVATED"
        else:
            return "EXTREME"

    def compute_position_modifier(
        self,
        funding: FundingSignal | None,
        ls_ratio: LSRatioSignal | None,
    ) -> float:
        """Compute position size multiplier from funding rate and L/S ratio."""
        modifier = 1.0

        if funding:
            if funding.signal == "EXTREME":
                modifier *= 0.50
            elif funding.signal == "ELEVATED":
                modifier *= 0.75
            elif funding.signal == "EXTREME_NEGATIVE":
                modifier *= 1.25
            elif funding.signal == "NEGATIVE":
                modifier *= 1.10

        if ls_ratio and ls_ratio.signal == "EXTREME_LONG":
            modifier *= 0.75

        return max(0.25, min(1.25, modifier))

    def detect_oi_divergence(self, oi: OISignal | None, price_change_pct: float) -> bool:
        """Detect bearish divergence: price rising but OI falling."""
        if oi is None:
            return False
        return price_change_pct > 1.0 and oi.signal == "FALLING"

    def get_derivatives_signals(self, price_change_pct: float | None = None) -> DerivativesSignals:
        """Fetch all signals and compute aggregated result."""
        funding = self.get_funding_rate()
        oi = self.get_open_interest()
        ls_ratio = self.get_long_short_ratio()

        position_modifier = self.compute_position_modifier(funding, ls_ratio)

        oi_divergence = False
        if price_change_pct is not None:
            oi_divergence = self.detect_oi_divergence(oi, price_change_pct)

        caution_flags = []
        if funding and funding.signal == "EXTREME":
            caution_flags.append(f"Extreme funding rate: {funding.avg_rate*100:.4f}% (overleveraged longs)")
        if funding and funding.signal == "EXTREME_NEGATIVE":
            caution_flags.append(f"Extreme negative funding: {funding.avg_rate*100:.4f}% (capitulation)")
        if ls_ratio and ls_ratio.signal == "EXTREME_LONG":
            caution_flags.append(f"Crowded long: {ls_ratio.long_ratio:.0%} of accounts long")
        if oi_divergence:
            caution_flags.append(f"OI bearish divergence: price +{price_change_pct:.1f}% but OI falling")

        return DerivativesSignals(
            funding=funding,
            oi=oi,
            ls_ratio=ls_ratio,
            position_modifier=position_modifier,
            entry_allowed=not oi_divergence,
            caution_flags=caution_flags,
        )
