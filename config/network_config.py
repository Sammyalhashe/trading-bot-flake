"""Blockchain network configuration for Base"""
from dataclasses import dataclass
import os


@dataclass
class NetworkConfig:
    """Blockchain network configuration for Base"""
    chain_id: int
    rpc_urls: list[str]

    # Contract Addresses
    usdc_address: str
    weth_address: str
    swap_router_address: str
    quoter_address: str
    uniswap_factory_address: str

    # Token Registry
    tokens: dict[str, str]

    # Known Decimals (avoids RPC calls)
    known_decimals: dict[str, int]

    # Fee Tiers
    fee_tiers: list[int]

    # Slippage & Gas Settings
    slippage_bps: int
    min_gas_eth: float
    tx_deadline_seconds: int

    # Balance Scan Tokens (minimal set to avoid rate limits)
    balance_scan_tokens: list[str]

    @classmethod
    def base_mainnet(cls) -> 'NetworkConfig':
        """Base mainnet configuration"""
        return cls(
            chain_id=8453,
            rpc_urls=[
                "https://mainnet.base.org",
                "https://base.llamarpc.com",
            ],
            usdc_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            weth_address="0x4200000000000000000000000000000000000006",
            swap_router_address="0x2626664c2603336E57B271c5C0b26F421741e481",
            quoter_address="0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
            uniswap_factory_address="0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
            tokens={
                "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",   # Native USDC
                "USDC.e": "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca", # Bridged USDC
                "WETH": "0x4200000000000000000000000000000000000006",
                "BTC": "0x0555E30da8f98308EdB960aa94C0Db47230d2B9c",    # WBTC
                "DEGEN": "0x4ed4E281562193f5C8c11259D3e21839951e7d23",
                "AERO": "0x9401811A062933285c64D72A25e8e3cf24f3fFBE",
                "LINK": "0x88fb150bdc53a65fe94dea0c9ba0a6daf8c6e196",    # Chainlink
                "BRETT": "0x532f27101965dd16442e59d40670faf5ebb142e4",  # Brett meme coin
                "TOSHI": "0xAC1Bd2486aAf3B5C0fc3Fd868558b082a531B2B4",  # Toshi cat meme coin
                "MORPHO": "0xbaa5cc21fd487b8fcc2f632f3f4e8d37262a0842", # Morpho DeFi token
                "ZRO": "0x6985884c4392d348587b19cb9eaaf157f13271cd",    # LayerZero token
            },
            known_decimals={
                "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913": 6,    # USDC
                "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": 6,    # USDC.e
                "0x4200000000000000000000000000000000000006": 18,   # WETH
                "0x0555E30da8f98308EdB960aa94C0Db47230d2B9c": 8,    # WBTC
                "0x4ed4E281562193f5C8c11259D3e21839951e7d23": 18,   # DEGEN
                "0x9401811A062933285c64D72A25e8e3cf24f3fFBE": 18,   # AERO
                "0x88fb150bdc53a65fe94dea0c9ba0a6daf8c6e196": 18,   # LINK
                "0x532f27101965dd16442e59d40670faf5ebb142e4": 18,   # BRETT
                "0xAC1Bd2486aAf3B5C0fc3Fd868558b082a531B2B4": 18,   # TOSHI
                "0xbaa5cc21fd487b8fcc2f632f3f4e8d37262a0842": 18,   # MORPHO
                "0x6985884c4392d348587b19cb9eaaf157f13271cd": 18,   # ZRO
            },
            fee_tiers=[500, 3000, 10000],  # 0.05%, 0.30%, 1.00%
            slippage_bps=50,  # 0.5%
            min_gas_eth=float(os.getenv("MIN_GAS_ETH", "0.001")),
            tx_deadline_seconds=120,
            balance_scan_tokens=["USDC", "WETH"],
        )

    def get_pool_info(self) -> dict[str, dict]:
        """
        Get known pool information.

        Returns dict with product_id -> {address, fee} mapping
        """
        return {
            "ETH-USDC": {
                "address": "0x6c561B446416E1A00E8E93E221854d6eA4171372",
                "fee": 3000,  # 0.3%
            },
            "BTC-USDC": {
                "address": "0x49e30c322E2474B3767de9FC4448C1e9ceD6552f",
                "fee": 3000,  # 0.3%
            },
        }
