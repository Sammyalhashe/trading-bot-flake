"""Trading strategy configuration"""
from dataclasses import dataclass
from decimal import Decimal
import os


@dataclass
class TradingConfig:
    """Trading strategy configuration"""

    # Moving Averages
    ma_short_window: int
    ma_long_window: int

    # Risk Management
    portfolio_risk_pct: Decimal
    short_risk_pct: Decimal
    risk_per_trade_pct: Decimal
    max_position_usd: Decimal
    max_drawdown_pct: Decimal
    drawdown_cooldown_hours: int
    min_order_usd: Decimal

    # Regime Detection
    trend_asset: str
    enable_dual_regime: bool
    enable_btc_dominance: bool
    allow_btc_in_bear: bool

    # Technical Indicators
    rsi_overbought: Decimal
    trailing_stop_pct: Decimal
    min_24h_volume_usd: Decimal

    # Fee Configuration
    round_trip_fee_pct: Decimal

    # Take Profit Levels
    take_profit_1_pct: Decimal
    take_profit_1_sell_ratio: Decimal
    take_profit_2_pct: Decimal
    take_profit_2_sell_ratio: Decimal

    # Strategy Flags
    enable_short: bool

    # Asset Configuration
    asset_blacklist: list[str]
    momentum_window_hours: int
    top_momentum_count: int

    # Strategy Selection
    strategy: str

    # Mean-Reversion Parameters
    mr_rsi_oversold: Decimal
    mr_bollinger_period: int
    mr_bollinger_std: float
    mr_trailing_stop_pct: Decimal
    mr_time_exit_candles: int

    # Asset Mapping (for rebranded/bridged tokens)
    asset_mapping: dict[str, str]

    @classmethod
    def from_env(cls) -> 'TradingConfig':
        """Load configuration from environment variables"""
        # Validate TREND_ASSET
        trend_asset = os.getenv("TREND_ASSET", "BTC").upper()
        if trend_asset not in ["BTC", "ETH"]:
            trend_asset = "BTC"  # fallback to BTC if invalid

        return cls(
            # Moving Averages
            ma_short_window=int(os.getenv("SHORT_WINDOW", "20")),
            ma_long_window=int(os.getenv("LONG_WINDOW", "50")),

            # Risk Management
            portfolio_risk_pct=Decimal(os.getenv("PORTFOLIO_RISK_PERCENTAGE", "0.15")),
            short_risk_pct=Decimal(os.getenv("SHORT_RISK_PERCENTAGE", "0.05")),
            risk_per_trade_pct=Decimal(os.getenv("RISK_PER_TRADE_PCT", "0.95")),
            max_position_usd=Decimal(os.getenv("MAX_POSITION_USD", "5000")),
            max_drawdown_pct=Decimal(os.getenv("MAX_DRAWDOWN_PCT", "10")),
            drawdown_cooldown_hours=int(os.getenv("DRAWDOWN_COOLDOWN_HOURS", "24")),
            min_order_usd=Decimal(os.getenv("MIN_ORDER_USD", "10")),

            # Regime Detection
            trend_asset=trend_asset,
            enable_dual_regime=os.getenv("ENABLE_DUAL_REGIME", "true").lower() == "true",
            enable_btc_dominance=os.getenv("ENABLE_BTC_DOMINANCE", "false").lower() == "true",
            allow_btc_in_bear=os.getenv("ALLOW_BTC_IN_BEAR", "true").lower() == "true",

            # Technical Indicators
            rsi_overbought=Decimal(os.getenv("RSI_OVERBOUGHT", "70")),
            trailing_stop_pct=Decimal(os.getenv("TRAILING_STOP_PCT", "0.05")),
            min_24h_volume_usd=Decimal(os.getenv("MIN_24H_VOLUME_USD", "100000")),

            # Fee Configuration
            round_trip_fee_pct=Decimal(os.getenv("ROUND_TRIP_FEE_PCT", "0.006")),

            # Take Profit Levels
            take_profit_1_pct=Decimal(os.getenv("TAKE_PROFIT_1_PCT", "0.10")),
            take_profit_1_sell_ratio=Decimal(os.getenv("TAKE_PROFIT_1_SELL_RATIO", "0.33")),
            take_profit_2_pct=Decimal(os.getenv("TAKE_PROFIT_2_PCT", "0.20")),
            take_profit_2_sell_ratio=Decimal(os.getenv("TAKE_PROFIT_2_SELL_RATIO", "0.50")),

            # Strategy Flags
            enable_short=os.getenv("ENABLE_SHORT", "true").lower() == "true",

            # Strategy Selection
            strategy=os.getenv("STRATEGY", "trend_following"),

            # Mean-Reversion Parameters
            mr_rsi_oversold=Decimal(os.getenv("MR_RSI_OVERSOLD", "30")),
            mr_bollinger_period=int(os.getenv("MR_BOLLINGER_PERIOD", "20")),
            mr_bollinger_std=float(os.getenv("MR_BOLLINGER_STD", "2.0")),
            mr_trailing_stop_pct=Decimal(os.getenv("MR_TRAILING_STOP_PCT", "0.05")),
            mr_time_exit_candles=int(os.getenv("MR_TIME_EXIT_CANDLES", "24")),

            # Asset Configuration
            asset_blacklist=["DOGE", "SHLD", "SHIB"],
            momentum_window_hours=24,
            top_momentum_count=3,

            # Asset Mapping
            asset_mapping={
                "MATIC": "POL",      # MATIC rebranded to POL on Coinbase
                "ETH_NATIVE": "ETH", # For pricing native ETH
                "USDC.e": "USDC",    # For pricing bridged USDC
            },
        )

    def validate(self) -> None:
        """Validate configuration constraints"""
        errors = []

        # Strategy validation
        valid_strategies = ["trend_following", "mean_reversion"]
        if self.strategy not in valid_strategies:
            errors.append(f"Invalid strategy '{self.strategy}', must be one of {valid_strategies}")

        # Moving Average validation
        if self.ma_short_window >= self.ma_long_window:
            errors.append(f"Short MA ({self.ma_short_window}) must be < Long MA ({self.ma_long_window})")

        # Trend asset validation
        if self.trend_asset not in ["BTC", "ETH"]:
            errors.append(f"Invalid TREND_ASSET: {self.trend_asset}, must be BTC or ETH")

        # Risk percentage validation
        if not (0 < self.portfolio_risk_pct <= 1):
            errors.append(f"Portfolio risk ({self.portfolio_risk_pct}) must be between 0 and 1")

        if not (0 < self.short_risk_pct <= 1):
            errors.append(f"Short risk ({self.short_risk_pct}) must be between 0 and 1")

        if not (0 < self.risk_per_trade_pct <= 1):
            errors.append(f"Risk per trade ({self.risk_per_trade_pct}) must be between 0 and 1")

        # Position size validation
        if self.max_position_usd < self.min_order_usd:
            errors.append(f"Max position ({self.max_position_usd}) must be >= min order ({self.min_order_usd})")

        # RSI validation
        if not (0 < self.rsi_overbought <= 100):
            errors.append(f"RSI overbought ({self.rsi_overbought}) must be between 0 and 100")

        # Percentage validations
        if not (0 <= self.trailing_stop_pct <= 1):
            errors.append(f"Trailing stop ({self.trailing_stop_pct}) must be between 0 and 1")

        if not (0 < self.take_profit_1_pct <= 1):
            errors.append(f"Take profit 1 ({self.take_profit_1_pct}) must be between 0 and 1")

        if not (0 < self.take_profit_2_pct <= 1):
            errors.append(f"Take profit 2 ({self.take_profit_2_pct}) must be between 0 and 1")

        if not (0 < self.take_profit_1_sell_ratio <= 1):
            errors.append(f"Take profit 1 sell ratio ({self.take_profit_1_sell_ratio}) must be between 0 and 1")

        if not (0 < self.take_profit_2_sell_ratio <= 1):
            errors.append(f"Take profit 2 sell ratio ({self.take_profit_2_sell_ratio}) must be between 0 and 1")

        # Raise errors if any
        if errors:
            raise ValueError("Configuration validation errors:\n  " + "\n  ".join(errors))
