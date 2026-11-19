#!/usr/bin/env python3
"""Small live test swaps on Base through KyberSwap or CoW, recording quoted vs actual fills.

  onchain-test-swap fund --eth 0.02 --venue kyberswap [--confirm]
  onchain-test-swap roundtrip --venue cow --asset BTC --usd 25 --count 2 [--confirm]

Without --confirm nothing is signed: it prints the quote (and validates KyberSwap's calldata or
builds the CoW order) for the first leg. Fills go to the `fills` table of the cost-probe DB.
"""
import argparse
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

from executors.ethereum_executor import SLIPPAGE_BPS, TOKENS, EthereumExecutor
from onchain_cost_probe import DEFAULT_DB, book_mid, fetch_book, roundtrip_bps

USDC = TOKENS["USDC"]
ASSETS = {"BTC": (TOKENS["BTC"], 8), "ETH": (TOKENS["WETH"], 18)}
USDC_DECIMALS = 6
VENUES = ("kyberswap", "cow")
MAX_TEST_USD = 50
MAX_WRAP_ETH = 0.03


def mid_price(asset):
    return book_mid(fetch_book(f"{asset}-USD"))


def fill_row(venue, asset, side, amount_in, result, mid, eth_usd, seconds):
    """One leg's quoted vs received fill; cost_bps is against the Coinbase mid (positive = worse)."""
    token_units = 10 ** ASSETS[asset][1]
    usdc_units = 10 ** USDC_DECIMALS
    quoted, received = result["quoted_out"], result["received"]
    if side == "buy":
        price = (amount_in / usdc_units) / (received / token_units)
        cost_bps = (price / mid - 1) * 1e4
    else:
        price = (received / usdc_units) / (amount_in / token_units)
        cost_bps = (1 - price / mid) * 1e4
    return {"ts": int(time.time()), "venue": venue, "asset": asset, "side": side, "amount_in": amount_in,
            "quoted_out": quoted, "min_out": result.get("min_out"), "received_out": received,
            "slippage_bps": (1 - received / quoted) * 1e4, "cost_bps": cost_bps,
            "gas_usd": result.get("gas_wei", 0) / 1e18 * eth_usd,
            "ref": result.get("tx_hash") or result.get("order_uid"), "seconds": seconds}


def record_fills(db_path, rows):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        db.execute("create table if not exists fills (ts integer, venue text, asset text, side text, "
                   "amount_in integer, quoted_out integer, min_out integer, received_out integer, "
                   "slippage_bps real, cost_bps real, gas_usd real, ref text, seconds real)")
        db.executemany("insert into fills values (:ts, :venue, :asset, :side, :amount_in, :quoted_out, :min_out, "
                       ":received_out, :slippage_bps, :cost_bps, :gas_usd, :ref, :seconds)", rows)


def swap_leg(executor, venue, asset, side, amount_in, mid, eth_usd, db_path):
    token = ASSETS[asset][0]
    token_in, token_out = (USDC, token) if side == "buy" else (token, USDC)
    started = time.time()
    result = executor.execute_swap(token_in, token_out, amount_in, executor.account.address,
                                   reference_price=mid, router=venue)
    if not result.get("success") or not result.get("received"):
        print(f"{venue} {side} {asset} failed: {result.get('error')} (ref {result.get('tx_hash') or result.get('order_uid')})")
        return None
    row = fill_row(venue, asset, side, amount_in, result, mid, eth_usd, time.time() - started)
    record_fills(db_path, [row])
    print(f"{venue} {side} {asset}: quoted {row['quoted_out']} received {row['received_out']} "
          f"({row['slippage_bps']:+.1f} bps vs quote, {row['cost_bps']:+.1f} bps vs mid, gas ${row['gas_usd']:.4f}, "
          f"{row['seconds']:.0f}s) {row['ref']}")
    return row


def roundtrip(executor, venue, asset, usd, count, db_path):
    if asset not in ASSETS or not 0 < usd <= MAX_TEST_USD or venue not in VENUES:
        sys.exit(f"refusing: asset in {list(ASSETS)}, venue in {VENUES}, 0 < usd <= {MAX_TEST_USD}")
    summaries = []
    for i in range(count):
        mid, eth_usd = mid_price(asset), mid_price("ETH")
        usdc_in = int(usd * 10**USDC_DECIMALS)
        bought = swap_leg(executor, venue, asset, "buy", usdc_in, mid, eth_usd, db_path)
        if bought is None:
            break
        sold = swap_leg(executor, venue, asset, "sell", bought["received_out"], mid_price(asset), eth_usd, db_path)
        if sold is None:
            print(f"Holding {bought['received_out']} raw {asset} from the unmatched buy; sell it manually")
            break
        usdc_back = sold["received_out"] / 10**USDC_DECIMALS
        bps = roundtrip_bps(usd, usdc_back, bought["gas_usd"] + sold["gas_usd"])
        summaries.append({"venue": venue, "asset": asset, "usdc_in": usd, "usdc_back": usdc_back, "roundtrip_bps": bps})
        print(f"Round trip {i + 1}/{count}: ${usd:.2f} -> ${usdc_back:.6f}, {bps:.1f} bps incl. gas")
    return summaries


def fund(executor, venue, eth, db_path):
    if not 0 < eth <= MAX_WRAP_ETH or venue not in VENUES:
        sys.exit(f"refusing: venue in {VENUES}, 0 < eth <= {MAX_WRAP_ETH}")
    amount = int(round(eth * 10**18))
    wrapped = executor.wrap_eth(amount)
    print(f"wrap {eth} ETH: {wrapped}")
    if not wrapped.get("success"):
        return None
    eth_usd = mid_price("ETH")
    return swap_leg(executor, venue, "ETH", "sell", amount, eth_usd, eth_usd, db_path)


def dry_run(executor, venue, token_in, token_out, amount_in, mid):
    """Quote the leg and run the pre-signing checks without approving, signing or sending."""
    me = executor.account.address
    if venue == "kyberswap":
        route = executor.kyber.routes(token_in, token_out, amount_in)
        built = executor.kyber.build(route, me, me, SLIPPAGE_BPS)
        quoted = int(built["amountOut"])
        our_min = executor._amount_out_minimum(quoted)
        executor._check_kyber_build(built, token_in, token_out, amount_in, our_min)
        print(f"kyberswap: quoted {quoted}, min {our_min}; calldata checks passed (router {built.get('routerAddress')})")
    else:
        quote_id, quoted = executor._cow_quote(token_in, token_out, amount_in)
        our_min = executor._amount_out_minimum(quoted)
        order = executor._cow_order(token_in, token_out, amount_in, our_min, int(time.time()) + 600)
        print(f"cow: quote {quote_id} buyAmount {quoted}; would sign {order}")
    refusal = executor._reference_price_refusal(token_in, token_out, amount_in, quoted, mid, venue)
    print(f"reference {mid:,.2f}: {f'REFUSED: {refusal}' if refusal else 'within bounds'}")


def make_executor():
    key = os.environ.get("ETH_PRIVATE_KEY")
    if not key and os.path.exists("/run/secrets/eth_private_key"):
        key = Path("/run/secrets/eth_private_key").read_text().strip()
    if not key:
        sys.exit("ETH_PRIVATE_KEY not set")
    rpc_url = os.environ.get("BASE_RPC_URL") or os.environ.get("ETH_RPC_URL") or "https://mainnet.base.org"
    return EthereumExecutor(rpc_url, key, "live")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fund", help="wrap ETH and sell the WETH for USDC")
    f.add_argument("--eth", type=float, required=True)
    r = sub.add_parser("roundtrip", help="USDC -> asset -> USDC")
    r.add_argument("--asset", default="BTC", choices=sorted(ASSETS))
    r.add_argument("--usd", type=float, default=25)
    r.add_argument("--count", type=int, default=1)
    for p in (f, r):
        p.add_argument("--venue", required=True, choices=VENUES)
        p.add_argument("--confirm", action="store_true", help="actually trade")
        p.add_argument("--db", default=os.environ.get("PROBE_DB") or str(DEFAULT_DB))
    args = parser.parse_args()

    executor = make_executor()
    if not args.confirm:
        if args.command == "fund":
            dry_run(executor, args.venue, TOKENS["WETH"], USDC, int(round(args.eth * 10**18)), mid_price("ETH"))
        else:
            dry_run(executor, args.venue, USDC, ASSETS[args.asset][0], int(args.usd * 10**USDC_DECIMALS),
                    mid_price(args.asset))
        print("Dry run: nothing signed. Add --confirm to trade.")
        return
    if args.command == "fund":
        fund(executor, args.venue, args.eth, args.db)
    else:
        roundtrip(executor, args.venue, args.asset, args.usd, args.count, args.db)


if __name__ == "__main__":
    main()
