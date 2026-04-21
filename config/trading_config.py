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
    risk_per_trade_pct: Decimal
    max_drawdown_pct: Decimal
    drawdown_cooldown_hours: int
    min_order_usd: Decimal

    # Regime Detection
    trend_asset: str
    enable_dual_regime: bool
    enable_btc_dominance: bool
    allow_btc_in_bear: bool
    bear_position_scale: float

    # Derivatives Signals (OKX funding rates, OI, long/short ratio)
    enable_derivatives_signals: bool
    derivatives_funding_high: float
    derivatives_funding_extreme: float
    derivatives_oi_divergence_pct: float

    # Technical Indicators
    rsi_overbought: Decimal
    rsi_overbought_bull: Decimal
    rsi_overbought_strong_bull: Decimal
    rsi_overbought_neutral: Decimal
    rsi_overbought_bear: Decimal
    trailing_stop_pct: Decimal
    min_24h_volume_usd: Decimal
    volume_spike_rsi_bonus: float
    volume_spike_threshold: float

    # Fee Configuration
    round_trip_fee_pct: Decimal

    # Take Profit Levels
    take_profit_1_pct: Decimal
    take_profit_1_sell_ratio: Decimal
    take_profit_2_pct: Decimal
    take_profit_2_sell_ratio: Decimal

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

    # Concentration Guard
    max_concurrent_positions: int

    # CFM Futures (Coinbase Financial Markets — US nano perp-style contracts)
    enable_futures: bool
    futures_leverage: float
    futures_round_trip_fee_pct: Decimal

    # WebSocket Mode
    ws_scan_interval: int

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
            # Moving Averages — 21/55 (Fibonacci) proved most effective in benchmarks.
            # Faster regime detection catches rallies and dumps sooner without 
            # excessive whipsaw. (Updated from 20/50 after YTD_2025/H2_2024 testing).
            ma_short_window=int(os.getenv("SHORT_WINDOW", "21")),
            ma_long_window=int(os.getenv("LONG_WINDOW", "55")),

            # Risk Management
            portfolio_risk_pct=Decimal(os.getenv("PORTFOLIO_RISK_PERCENTAGE", "0.90")),
            risk_per_trade_pct=Decimal(os.getenv("RISK_PER_TRADE_PCT", "0.95")),
            max_drawdown_pct=Decimal(os.getenv("MAX_DRAWDOWN_PCT", "15")),
            drawdown_cooldown_hours=int(os.getenv("DRAWDOWN_COOLDOWN_HOURS", "48")),
            min_order_usd=Decimal(os.getenv("MIN_ORDER_USD", "10")),

            # Regime Detection
            trend_asset=trend_asset,
            enable_dual_regime=os.getenv("ENABLE_DUAL_REGIME", "true").lower() == "true",
            enable_btc_dominance=os.getenv("ENABLE_BTC_DOMINANCE", "false").lower() == "true",
            allow_btc_in_bear=os.getenv("ALLOW_BTC_IN_BEAR", "true").lower() == "true",
            # Bear position scaling: 0.0 = no trades in BEAR (old behavior),
            # 0.25 = 25% position size, 1.0 = full size. Backtested best at 0.25.
            bear_position_scale=float(os.getenv("BEAR_POSITION_SCALE", "0.25")),

            # Derivatives Signals — OKX perpetual futures data (funding rates, OI, L/S ratio).
            # Opt-in: modifies position sizing and filters entries based on leveraged positioning.
            enable_derivatives_signals=os.getenv("ENABLE_DERIVATIVES_SIGNALS", "false").lower() == "true",
            derivatives_funding_high=float(os.getenv("DERIVATIVES_FUNDING_HIGH", "0.0005")),
            derivatives_funding_extreme=float(os.getenv("DERIVATIVES_FUNDING_EXTREME", "0.0010")),
            derivatives_oi_divergence_pct=float(os.getenv("DERIVATIVES_OI_DIVERGENCE_PCT", "-5.0")),

            # Technical Indicators
            rsi_overbought=Decimal(os.getenv("RSI_OVERBOUGHT", "75")),
            rsi_overbought_bull=Decimal(os.getenv("RSI_OVERBOUGHT_BULL", "82")),
            rsi_overbought_strong_bull=Decimal(os.getenv("RSI_OVERBOUGHT_STRONG_BULL", "88")),
            rsi_overbought_neutral=Decimal(os.getenv("RSI_OVERBOUGHT_NEUTRAL", "75")),
            rsi_overbought_bear=Decimal(os.getenv("RSI_OVERBOUGHT_BEAR", "70")),
            trailing_stop_pct=Decimal(os.getenv("TRAILING_STOP_PCT", "0.07")),
            min_24h_volume_usd=Decimal(os.getenv("MIN_24H_VOLUME_USD", "500000")),
            volume_spike_rsi_bonus=float(os.getenv("VOLUME_SPIKE_RSI_BONUS", "5")),
            volume_spike_threshold=float(os.getenv("VOLUME_SPIKE_THRESHOLD", "2.0")),

            # Fee Configuration — Coinbase Advanced Level 3 (w/ Coinbase One):
            # Maker 0.075% / Taker 0.150% per side.
            # Default assumes maker fees (post_only limit orders): 0.075% × 2 = 0.15%
            round_trip_fee_pct=Decimal(os.getenv("ROUND_TRIP_FEE_PCT", "0.0015")),

            # Take Profit Levels — wide targets, small sells, let trailing stop
            # do the heavy lifting. TP1 locks in some profit, TP2 is for big moves.
            take_profit_1_pct=Decimal(os.getenv("TAKE_PROFIT_1_PCT", "0.15")),
            take_profit_1_sell_ratio=Decimal(os.getenv("TAKE_PROFIT_1_SELL_RATIO", "0.25")),
            take_profit_2_pct=Decimal(os.getenv("TAKE_PROFIT_2_PCT", "0.40")),
            take_profit_2_sell_ratio=Decimal(os.getenv("TAKE_PROFIT_2_SELL_RATIO", "0.35")),

            # Strategy Selection
            # Default to 'trend_following' — backtested combined (MA20/100 + bear0.25):
            # +7.28% avg return, 1.76 Sharpe, -6.78% maxDD across 5 periods.
            # 'auto' underperforms due to mean_reversion losses in volatile markets
            # (-26.70% YTD_2025 vs -6.11% for trend_following).
            strategy=os.getenv("STRATEGY", "trend_following"),

            # Mean-Reversion Parameters (only used if STRATEGY=mean_reversion)
            mr_rsi_oversold=Decimal(os.getenv("MR_RSI_OVERSOLD", "30")),
            mr_bollinger_period=int(os.getenv("MR_BOLLINGER_PERIOD", "20")),
            mr_bollinger_std=float(os.getenv("MR_BOLLINGER_STD", "2.0")),
            mr_trailing_stop_pct=Decimal(os.getenv("MR_TRAILING_STOP_PCT", "0.08")),
            mr_time_exit_candles=int(os.getenv("MR_TIME_EXIT_CANDLES", "10")),

            # CFM Futures — Coinbase nano perp-style contracts (0.00% maker / 0.03% taker).
            # Opt-in: runs as a separate executor alongside spot. BTC + ETH only.
            enable_futures=os.getenv("ENABLE_FUTURES", "false").lower() == "true",
            futures_leverage=float(os.getenv("FUTURES_LEVERAGE", "1.0")),
            futures_round_trip_fee_pct=Decimal(os.getenv("FUTURES_ROUND_TRIP_FEE_PCT", "0.0006")),

            # Concentration Guard
            max_concurrent_positions=int(os.getenv("MAX_CONCURRENT_POSITIONS", "3")),

            # WebSocket Mode — 120s between full scans; ticks handle exits
            ws_scan_interval=int(os.getenv("WS_SCAN_INTERVAL", "120")),

            # Asset Configuration
            asset_blacklist=["DOGE", "SHLD", "SHIB"],
            momentum_window_hours=24,
            top_momentum_count=int(os.getenv("TOP_MOMENTUM_COUNT", "3")),

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
        valid_strategies = ["trend_following", "mean_reversion", "supertrend", "mtf_trend", "auto"]
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

        if not (0 < self.risk_per_trade_pct <= 1):
            errors.append(f"Risk per trade ({self.risk_per_trade_pct}) must be between 0 and 1")

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

        # Futures validation
        if self.enable_futures:
            if self.futures_leverage != 1.0:
                errors.append(f"FUTURES_LEVERAGE must be 1.0 (got {self.futures_leverage}). Leverage >1x not yet supported")

        # Raise errors if any
        if errors:
            raise ValueError("Configuration validation errors:\n  " + "\n  ".join(errors))
