"""Strategy registry and factory. STRATEGY_NAMES is the single list of valid STRATEGY values."""
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config.trading_config import TradingConfig
    from core.strategy import Strategy
    from core.technical_analysis import TechnicalAnalysis

# name -> (module, class); modules are imported lazily so config can import this file.
STRATEGIES = {
    "trend_following": ("strategies.trend_following", "TrendFollowingStrategy"),
    "mean_reversion": ("strategies.mean_reversion", "MeanReversionStrategy"),
    "supertrend": ("strategies.supertrend", "SupertrendStrategy"),
    "mtf_trend": ("strategies.mtf_trend", "MTFTrendStrategy"),
    "daily_trend": ("strategies.daily_trend", "DailyTrendStrategy"),
}

# "auto" picks one of the above per market regime (see select_strategy_for_regime in trading_bot.py).
STRATEGY_NAMES = (*STRATEGIES, "auto")


def create_strategy(name: str, ta: "TechnicalAnalysis", config: "TradingConfig") -> "Strategy":
    """Create a registered strategy instance by name."""
    if name not in STRATEGIES:
        raise ValueError(f"Unknown strategy: {name}")
    module, cls = STRATEGIES[name]
    return getattr(import_module(module), cls)(ta, config)
