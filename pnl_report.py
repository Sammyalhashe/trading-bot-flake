#!/usr/bin/env python3
"""Since-inception PnL report: realized performance (StateManager), recent
trades (TradeLog), and unrealized PnL on open positions vs. live balances.

Sends a Telegram summary and writes a JSON snapshot other tools (e.g. the
hermes agent) can read for on-demand questions.
"""
import os
import html
import json
import time
import secrets
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

import jwt
import requests
from cryptography.hazmat.primitives import serialization

from config import ExecutorConfig
from core import StateManager, TradeLog, send_telegram_message

SNAPSHOT_FILE = Path(os.environ.get(
    "PNL_SNAPSHOT_FILE",
    os.path.join(os.path.expanduser("~"), "projects", "trading-pnl", "snapshot.json"),
))
API_JSON_FILE = os.environ.get(
    "COINBASE_API_JSON", os.path.join(os.path.expanduser("~"), "cdb_api_key.json")
)


def _get_credentials():
    with open(API_JSON_FILE, "r") as f:
        data = json.load(f)
    return data.get("name"), data.get("privateKey")


def _build_jwt(name, key, service, uri):
    pk = serialization.load_pem_private_key(key.encode("utf-8"), None)
    payload = {
        "iss": "cdp", "nbf": int(time.time()), "exp": int(time.time()) + 120,
        "sub": name, "uri": f"{service} {uri}",
    }
    return jwt.encode(payload, pk, "ES256", headers={"kid": name, "nonce": secrets.token_hex()})


def _coinbase_request(method, path):
    try:
        name, key = _get_credentials()
        host = "api.coinbase.com"
        token = _build_jwt(name, key, method, f"{host}{urllib.parse.urlparse(path).path}")
        return requests.get(
            f"https://{host}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=15
        ).json()
    except Exception:
        return None


def get_current_price(product_id):
    try:
        return float(_coinbase_request("GET", f"/api/v3/brokerage/products/{product_id}")["price"])
    except Exception:
        return None


def get_all_balances():
    all_accounts = []
    path = "/api/v3/brokerage/accounts"
    while True:
        data = _coinbase_request("GET", path)
        if not data:
            break
        all_accounts.extend(data["accounts"])
        if not data.get("has_next"):
            break
        path = f"/api/v3/brokerage/accounts?cursor={data['cursor']}"

    balances = {"cash": {"USD": 0.0, "USDC": 0.0}, "crypto": {}}
    for acc in all_accounts:
        cur, val = acc["currency"], float(acc["available_balance"]["value"])
        if cur in balances["cash"]:
            balances["cash"][cur] = val
        elif val > 0:
            balances["crypto"][cur] = val
    return balances


def build_report() -> tuple[str, dict]:
    exec_config = ExecutorConfig.from_env()
    state_manager = StateManager(exec_config.state_file)
    trade_log = TradeLog(exec_config.state_file.with_suffix(".trades.db"))

    perf = state_manager.get_performance()
    state = state_manager.load_state()
    entry_prices = state.get("entry_prices", {})

    balances = get_all_balances()
    cash = balances["cash"]
    held = balances["crypto"]

    lines = ["<b>Trading Bot PnL Report</b>"]
    lines.append(f"As of: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")

    # --- Since-inception realized performance ---
    total_trades = perf.get("total_trades", 0)
    win_rate = (perf.get("winning_trades", 0) / total_trades * 100) if total_trades else 0.0
    pf = perf.get("profit_factor", 0.0)
    pf_str = f"{pf:.2f}" if pf != float("inf") else "inf"
    lines.append("")
    lines.append("<b>Realized (since inception)</b>")
    lines.append(f"Trades: {total_trades} | Win rate: {win_rate:.0f}%")
    lines.append(f"Total PnL: ${perf.get('total_pnl', 0):+,.2f} | Profit Factor: {pf_str}")
    lines.append(
        f"Avg Win: ${perf.get('avg_win', 0):+,.2f} / Avg Loss: ${perf.get('avg_loss', 0):,.2f}"
    )

    # --- Open positions: unrealized PnL vs live prices ---
    open_positions = []
    unrealized_total = 0.0
    for key, entry_price in entry_prices.items():
        # key format: "executor_id:PRODUCT-ID", e.g. "CoinbaseExecutor:SOL-USDC"
        product_id = key.split(":", 1)[-1]
        asset = product_id.split("-")[0]
        qty = held.get(asset)
        current_price = get_current_price(product_id)
        if qty is None or current_price is None:
            continue
        unrealized = (current_price - entry_price) * qty
        unrealized_pct = (current_price / entry_price - 1) * 100 if entry_price else 0.0
        unrealized_total += unrealized
        open_positions.append({
            "asset": asset,
            "qty": qty,
            "entry": entry_price,
            "current": current_price,
            "unrealized_usd": unrealized,
            "unrealized_pct": unrealized_pct,
        })

    lines.append("")
    if open_positions:
        lines.append(f"<b>Open Positions</b> (unrealized: ${unrealized_total:+,.2f})")
        for p in open_positions:
            lines.append(
                f"  {p['asset']}: {p['qty']:.4f} @ ${p['entry']:,.2f} → ${p['current']:,.2f} "
                f"({'+' if p['unrealized_usd'] >= 0 else ''}${p['unrealized_usd']:,.2f}, "
                f"{p['unrealized_pct']:+.2f}%)"
            )
    else:
        lines.append("<b>Open Positions</b>: none")

    # --- Current portfolio value ---
    total_value = cash["USD"] + cash["USDC"]
    for asset, qty in held.items():
        price = get_current_price(f"{asset}-USDC")
        if price:
            total_value += qty * price
    lines.append("")
    lines.append(f"<b>Current Portfolio Value</b>: ${total_value:,.2f}")
    lines.append(f"  Cash: ${cash['USD'] + cash['USDC']:,.2f}")

    # --- Recent closed trades ---
    recent = trade_log.query(side="SELL", limit=8)
    if recent:
        lines.append("")
        lines.append("<b>Recent Closed Trades</b>")
        for t in recent:
            pnl = t.get("pnl") or 0.0
            lines.append(
                f"  {t['timestamp']}: {t['asset']} PnL {'+' if pnl >= 0 else ''}${pnl:,.2f} "
                f"({html.escape(t.get('reason') or '-')})"
            )

    report_text = "\n".join(lines)

    snapshot = {
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "realized": {
            "total_trades": total_trades,
            "win_rate_pct": round(win_rate, 1),
            "total_pnl_usd": perf.get("total_pnl", 0),
            "profit_factor": pf if pf != float("inf") else None,
        },
        "unrealized_usd": unrealized_total,
        "open_positions": open_positions,
        "current_portfolio_value_usd": total_value,
    }
    return report_text, snapshot


def main():
    report_text, snapshot = build_report()
    print(html.unescape(report_text.replace("<b>", "").replace("</b>", "")))

    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(snapshot, f, indent=2)
    os.replace(tmp, SNAPSHOT_FILE)

    send_telegram_message(report_text)


if __name__ == "__main__":
    main()
