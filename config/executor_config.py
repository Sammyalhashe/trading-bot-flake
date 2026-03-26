"""Executor-specific configuration"""
from dataclasses import dataclass
from pathlib import Path
import os


@dataclass
class ExecutorConfig:
    """Executor-specific configuration"""
    trading_mode: str  # "paper" or "live"

    # File Paths
    api_json_file: Path
    state_file: Path
    log_file: Path

    # Coinbase Configuration
    coinbase_enabled: bool

    # Ethereum/Base Configuration
    ethereum_enabled: bool
    eth_rpc_url: str | None
    eth_private_key: str | None

    @classmethod
    def from_env(cls) -> 'ExecutorConfig':
        """Load executor config from environment"""
        home = os.path.expanduser("~")

        return cls(
            trading_mode=os.getenv("TRADING_MODE", "paper").lower(),
            api_json_file=Path(os.getenv("COINBASE_API_JSON", os.path.join(home, "cdb_api_key.json"))),
            state_file=Path(os.getenv("TRADING_STATE_FILE", os.path.join(
                os.getenv("XDG_STATE_HOME", os.path.join(home, ".local", "state")),
                "trading-bot", "trading_state.json"
            ))),
            log_file=Path(os.getenv("TRADING_LOG_FILE", os.path.join(home, ".openclaw", "workspace", "trading-bot", "trading.log"))),
            coinbase_enabled=True,  # Always enabled by default
            ethereum_enabled=os.getenv("ENABLE_ETHEREUM", "false").lower() == "true",
            eth_rpc_url=os.getenv("ETH_RPC_URL"),
            eth_private_key=os.getenv("ETH_PRIVATE_KEY"),
        )

    def validate(self) -> None:
        """Validate configuration"""
        errors = []

        # Validate trading mode
        if self.trading_mode not in ["paper", "live"]:
            errors.append(f"Invalid trading mode: {self.trading_mode}, must be 'paper' or 'live'")

        # Validate Coinbase config (only in live mode)
        if self.coinbase_enabled and self.trading_mode == "live":
            if not self.api_json_file.exists():
                errors.append(f"Coinbase API JSON file not found: {self.api_json_file}")

        # Validate Ethereum config (only in live mode)
        if self.ethereum_enabled and self.trading_mode == "live":
            if not self.eth_rpc_url:
                errors.append("ETH_RPC_URL required when ENABLE_ETHEREUM=true in live mode")
            if not self.eth_private_key:
                errors.append("ETH_PRIVATE_KEY required when ENABLE_ETHEREUM=true in live mode")

        # Raise errors if any
        if errors:
            raise ValueError("Executor configuration validation errors:\n  " + "\n  ".join(errors))
