#!/usr/bin/env python3
"""Quote-only probe of round-trip swap cost on Base DEX pools vs Coinbase, for the same sizes.

Never signs or sends: on-chain reads are eth_calls to quoter contracts. Base's L1 data fee
(cents) is not modelled.
"""
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

import requests
from web3 import Web3

from executors.ethereum_executor import EXPECTED_CHAIN_ID, FALLBACK_RPCS, TOKENS, EthereumExecutor
from executors.zerox_client import ZeroExClient

USDC = TOKENS["USDC"]
ASSETS = {"BTC": TOKENS["BTC"], "ETH": TOKENS["WETH"]}
DECIMALS = {"BTC": 8, "ETH": 18}
USDC_DECIMALS = 6

# Verified on Base: factory() returns Uniswap v3's and Aerodrome Slipstream's pool factories.
UNIV3_QUOTER = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
SLIPSTREAM_QUOTER = "0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0"
UNIV3_FEE_TIERS = (100, 500, 3000)
SLIPSTREAM_TICK_SPACINGS = (1, 50, 100, 200)
AERO_V2_GAS_PER_SWAP = 150_000
MAX_PRICE_DEVIATION = 0.02

COINBASE_BOOK_URL = "https://api.coinbase.com/api/v3/brokerage/market/product_book"
DEFAULT_DB = Path(os.path.expanduser("~/.local/state/trading-bot-paper/onchain_costs.db"))


def roundtrip_bps(usdc_in, usdc_back, gas_usd=0.0):
    return (1 - (usdc_back - gas_usd) / usdc_in) * 1e4


def buy_bps(usdc_in, tokens_out, mid):
    return (usdc_in / tokens_out / mid - 1) * 1e4


def gas_usd(gas_units, gas_price_wei, eth_usd):
    return gas_units * gas_price_wei / 1e18 * eth_usd


def best_roundtrip(quote, usdc, token, usdc_in_raw, options):
    """Buy then immediately sell back through each pool option; keep the one returning most USDC.

    quote(token_in, token_out, amount_in, option) -> (amount_out, gas); raising means no pool.
    """
    best = None
    for option in options:
        try:
            tokens_raw, gas_buy = quote(usdc, token, usdc_in_raw, option)
            if tokens_raw <= 0:
                continue
            usdc_back_raw, gas_sell = quote(token, usdc, tokens_raw, option)
        except Exception as e:
            logging.debug(f"quote failed for option {option}: {e}")
            continue
        if best is None or usdc_back_raw > best["usdc_back_raw"]:
            best = {"option": option, "tokens_raw": tokens_raw, "usdc_back_raw": usdc_back_raw,
                    "gas": gas_buy + gas_sell}
    return best


def venue_sample(asset, size_usd, venue, best, token_decimals, mid, gas_usd_per_unit):
    tokens = best["tokens_raw"] / 10**token_decimals
    if abs(size_usd / tokens / mid - 1) > MAX_PRICE_DEVIATION:
        return None
    cost_gas = best["gas"] * gas_usd_per_unit
    usdc_back = best["usdc_back_raw"] / 10**USDC_DECIMALS
    return {"asset": asset, "size_usd": size_usd, "venue": venue, "option": str(best["option"]),
            "buy_bps": buy_bps(size_usd, tokens, mid),
            "roundtrip_bps": roundtrip_bps(size_usd, usdc_back, cost_gas), "gas_usd": cost_gas,
            "detail": json.dumps({k: best[k] for k in ("tokens_raw", "usdc_back_raw", "gas")})}


def _walk(levels, budget, budget_is_quote):
    """Fill against book levels; returns the other side's amount, or None if the book is too thin."""
    remaining, received = budget, 0.0
    for level in levels:
        price, size = float(level["price"]), float(level["size"])
        capacity = size * price if budget_is_quote else size
        take = min(remaining, capacity)
        received += take / price if budget_is_quote else take * price
        remaining -= take
        if remaining <= 1e-12:
            return received
    return None


def book_mid(book):
    book = book["pricebook"]
    return (float(book["bids"][0]["price"]) + float(book["asks"][0]["price"])) / 2


def coinbase_sample(asset, size_usd, book, fee_per_side):
    mid = book_mid(book)
    tokens = _walk(book["pricebook"]["asks"], size_usd * (1 - fee_per_side), budget_is_quote=True)
    if tokens is None:
        return None
    proceeds = _walk(book["pricebook"]["bids"], tokens, budget_is_quote=False)
    if proceeds is None:
        return None
    return {"asset": asset, "size_usd": size_usd, "venue": "coinbase", "option": "",
            "buy_bps": buy_bps(size_usd, tokens, mid),
            "roundtrip_bps": roundtrip_bps(size_usd, proceeds * (1 - fee_per_side)), "gas_usd": 0.0,
            "detail": json.dumps({"fee_per_side": fee_per_side})}


def fetch_book(product_id):
    response = requests.get(COINBASE_BOOK_URL, params={"product_id": product_id, "limit": 500}, timeout=15)
    response.raise_for_status()
    return response.json()


def _quoter_abi(option_name, option_type):
    return [{"name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
             "inputs": [{"name": "params", "type": "tuple", "components": [
                 {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
                 {"name": "amountIn", "type": "uint256"}, {"name": option_name, "type": option_type},
                 {"name": "sqrtPriceLimitX96", "type": "uint160"}]}],
             "outputs": [{"name": "amountOut", "type": "uint256"}, {"name": "sqrtPriceX96After", "type": "uint160"},
                         {"name": "initializedTicksCrossed", "type": "uint32"},
                         {"name": "gasEstimate", "type": "uint256"}]}]


def make_quoter(w3, address, option_name, option_type):
    contract = w3.eth.contract(address=Web3.to_checksum_address(address), abi=_quoter_abi(option_name, option_type))

    def quote(token_in, token_out, amount_in, option):
        result = contract.functions.quoteExactInputSingle(
            (Web3.to_checksum_address(token_in), Web3.to_checksum_address(token_out), amount_in, option, 0)).call()
        return result[0], result[3]
    return quote


def make_aero_v2_executor(rpc_url):
    return EthereumExecutor(rpc_url, None, "paper", trusted_rpc_url=rpc_url)


def make_aero_v2_quoter(executor):
    def quote(token_in, token_out, amount_in, _option):
        best = executor.get_quote(token_in, token_out, amount_in)
        if best is None:
            raise RuntimeError("no Aerodrome v2 route")
        return best[1], AERO_V2_GAS_PER_SWAP
    return quote


class ZeroExQuoter:
    """0x indicative /price quotes (all-in, incl. 0x's own fee, recorded in `fees`)."""

    def __init__(self, client):
        self.client = client
        self.fees = []

    def __call__(self, token_in, token_out, amount_in, _option):
        response = self.client.price({"chainId": EXPECTED_CHAIN_ID, "sellToken": token_in,
                                      "buyToken": token_out, "sellAmount": str(amount_in)})
        if response.get("liquidityAvailable") is not True:
            raise RuntimeError("0x: no liquidity")
        self.fees.append((response.get("fees") or {}).get("zeroExFee"))
        return int(response["buyAmount"]), int(response.get("gas") or 0)


def make_zerox_quoter():
    key = os.environ.get("ZEROX_API_KEY")
    return ZeroExQuoter(ZeroExClient(key)) if key else None


def record(db_path, rows):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        db.execute("create table if not exists samples (ts integer, asset text, size_usd real, venue text, "
                   "option text, buy_bps real, roundtrip_bps real, gas_usd real, detail text)")
        db.executemany("insert into samples values (:ts, :asset, :size_usd, :venue, :option, :buy_bps, "
                       ":roundtrip_bps, :gas_usd, :detail)", rows)


def run_probe():
    sizes = [float(s) for s in os.environ.get("PROBE_SIZES_USD", "1000,10000").split(",")]
    fee_per_side = float(os.environ.get("COINBASE_FEE_PER_SIDE", "0.0011"))
    rpc_url = os.environ.get("BASE_TRUSTED_RPC_URL") or os.environ.get("BASE_RPC_URL") or FALLBACK_RPCS[0]
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))

    books = {asset: fetch_book(f"{asset}-USD") for asset in ASSETS}
    gas_usd_per_unit = gas_usd(1, w3.eth.gas_price, book_mid(books["ETH"]))
    venues = {
        "univ3": (make_quoter(w3, UNIV3_QUOTER, "fee", "uint24"), UNIV3_FEE_TIERS),
        "slipstream": (make_quoter(w3, SLIPSTREAM_QUOTER, "tickSpacing", "int24"), SLIPSTREAM_TICK_SPACINGS),
        "aero_v2": (make_aero_v2_quoter(make_aero_v2_executor(rpc_url)), (None,)),
    }
    zerox = make_zerox_quoter()
    if zerox:
        venues["zerox"] = (zerox, (None,))

    now, rows = int(time.time()), []
    for asset, token in ASSETS.items():
        mid = book_mid(books[asset])
        for size in sizes:
            samples = [coinbase_sample(asset, size, books[asset], fee_per_side)]
            for venue, (quote, options) in venues.items():
                if zerox:
                    zerox.fees = []
                best = best_roundtrip(quote, USDC, token, int(size * 10**USDC_DECIMALS), options)
                sample = best and venue_sample(asset, size, venue, best, DECIMALS[asset], mid, gas_usd_per_unit)
                if sample and venue == "zerox":
                    sample["detail"] = json.dumps({**json.loads(sample["detail"]), "zeroExFee": zerox.fees})
                if not sample:
                    logging.warning(f"{venue}: no usable quote for {asset} ${size:,.0f}")
                samples.append(sample)
            rows += [{**s, "ts": now} for s in samples if s]

    db_path = Path(os.environ.get("PROBE_DB") or DEFAULT_DB)
    record(db_path, rows)
    for r in sorted(rows, key=lambda r: (r["asset"], r["size_usd"], r["roundtrip_bps"])):
        print(f"{r['asset']:4} ${r['size_usd']:>8,.0f} {r['venue']:10} {r['option']:>5} "
              f"round trip {r['roundtrip_bps']:6.1f} bps | buy vs mid {r['buy_bps']:+6.1f} bps | gas ${r['gas_usd']:.4f}")
    return rows


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    run_probe()
