"""Paper trading with a simulated ledger.

Wraps a real CoinbaseExecutor for prices, candles and product details only; orders fill
immediately against a JSON ledger (cash + holdings) at the requested or current price,
with a fixed fee per side. Balances come back in the real executor's {available, total} shape.
"""
import fcntl
import json
import logging
import os
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_REMEMBERED_FILLS = 200


class PaperLedgerExecutor:
    def __init__(self, real_executor, ledger_path: Path, starting_cash: float, fee_per_side: float = 0.0011):
        self.real = real_executor
        self.ledger_path = Path(ledger_path)
        self.starting_cash = float(starting_cash)
        self.fee_per_side = float(fee_per_side)
        self.round_trip_fee_pct = Decimal(str(2 * self.fee_per_side))

    # --- read-only market data, delegated -------------------------------------------------
    def get_market_data(self, product_id, window, granularity="1h"):
        return self.real.get_market_data(product_id, window, granularity=granularity)

    def get_product_details(self, product_id):
        return self.real.get_product_details(product_id)

    def get_best_bid_ask(self, product_id):
        return self.real.get_best_bid_ask(product_id)

    def get_supported_assets(self):
        return self.real.get_supported_assets()

    def build_ws_jwt(self):
        return self.real.build_ws_jwt()

    # --- ledger ----------------------------------------------------------------------------
    @contextmanager
    def _ledger(self):
        """Locked read-modify-write; the scan and WS paths each hold their own instance."""
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ledger_path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.ledger_path.exists():
                ledger = json.loads(self.ledger_path.read_text())
            else:
                ledger = {"cash": self.starting_cash, "holdings": {}, "fills": {}}
                logger.info(f"[Paper] New ledger with ${self.starting_cash:,.2f} at {self.ledger_path}")
            yield ledger
            tmp = self.ledger_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(ledger, indent=2))
            os.replace(tmp, self.ledger_path)

    def get_balances(self):
        with self._ledger() as ledger:
            cash = {"USD": 0.0, "USDC": ledger["cash"]}
            crypto = dict(ledger["holdings"])
        return {"available": {"cash": dict(cash), "crypto": dict(crypto)},
                "total": {"cash": dict(cash), "crypto": dict(crypto)}}

    def _fill(self, product_id, side, price, quote=None, base=None):
        asset = product_id.split("-")[0]
        order_id = f"paper-{uuid.uuid4()}"
        with self._ledger() as ledger:
            if side == "BUY":
                spend = float(quote) if quote is not None else float(base) * price
                spend = min(spend, ledger["cash"] / (1 + self.fee_per_side))
                if spend < 1.0:
                    return {"success": False, "error": "insufficient paper cash"}
                fee = spend * self.fee_per_side
                ledger["cash"] -= spend + fee
                ledger["holdings"][asset] = ledger["holdings"].get(asset, 0.0) + spend / price
            else:
                held = ledger["holdings"].get(asset, 0.0)
                qty = min(float(base) if base is not None else float(quote) / price, held)
                if qty <= 0:
                    return {"success": False, "error": f"no paper {asset} to sell"}
                fee = qty * price * self.fee_per_side
                ledger["cash"] += qty * price - fee
                if held - qty > 1e-12:
                    ledger["holdings"][asset] = held - qty
                else:
                    ledger["holdings"].pop(asset, None)
            ledger["fills"][order_id] = {"price": price, "fee": fee}
            for stale in list(ledger["fills"])[:-MAX_REMEMBERED_FILLS]:
                del ledger["fills"][stale]
        logger.info(f"[Paper] {side} {product_id} @ ${price:,.2f} (fee ${fee:.2f})")
        return {"success": True, "success_response": {"order_id": order_id}}

    def _current_price(self, product_id):
        return float(self.real.get_product_details(product_id)["price"])

    def place_limit_order(self, product_id, side, price, amount_quote_currency=None, amount_base_currency=None):
        return self._fill(product_id, side, float(price), amount_quote_currency, amount_base_currency)

    def place_aggressive_limit_order(self, product_id, side, price, amount_base_currency):
        return self._fill(product_id, side, float(price), base=amount_base_currency)

    def place_market_order(self, product_id, side, amount_quote_currency=None, amount_base_currency=None):
        return self._fill(product_id, side, self._current_price(product_id), amount_quote_currency, amount_base_currency)

    def check_order_filled(self, order_id, **_):
        with self._ledger() as ledger:
            return ledger["fills"].get(order_id)

    def cancel_open_orders(self, product_id):
        return None  # paper orders fill immediately
