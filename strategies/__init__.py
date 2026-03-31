"""Strategy factory for trading bot"""
from core.strategy import Strategy
from core.technical_analysis import TechnicalAnalysis
from config.trading_config import TradingConfig


def create_strategy(name: str, ta: TechnicalAnalysis, config: TradingConfig) -> Strategy:
    """Create a strategy instance by name.

    Args:
        name: Strategy name ("trend_following", "mean_reversion", or "supertrend")
        ta: TechnicalAnalysis instance
        config: TradingConfig instance

    Returns:
        Strategy implementation
    """
    if name == "trend_following":
        from strategies.trend_following import TrendFollowingStrategy
        return TrendFollowingStrategy(ta, config)
    elif name == "mean_reversion":
        from strategies.mean_reversion import MeanReversionStrategy
        return MeanReversionStrategy(ta, config)
    elif name == "supertrend":
        from strategies.supertrend import SupertrendStrategy
        return SupertrendStrategy(ta, config)
    else:
        raise ValueError(f"Unknown strategy: {name}")
