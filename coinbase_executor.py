import time
import secrets
import jwt
import requests
import pandas as pd
import uuid
import json
import logging
import urllib.parse
from cryptography.hazmat.primitives import serialization
from decimal import Decimal

class CoinbaseExecutor:
    """Handles all interaction with the Coinbase API."""
    def __init__(self, api_json_file, trading_mode="paper"):
        self.api_json_file = api_json_file
        self.trading_mode = trading_mode
        self.product_details_cache = {}

    def _get_credentials(self):
        with open(self.api_json_file, 'r') as f:
            data = json.load(f)
        return data.get('name'), data.get('privateKey')

    def _build_jwt(self, api_key_name, private_key_pem, service, uri):
        private_key = serialization.load_pem_private_key(private_key_pem.encode('utf-8'), password=None)
        jwt_payload = {
            "iss": "cdp",
            "nbf": int(time.time()),
            "exp": int(time.time()) + 120,
            "sub": api_key_name,
            "uri": f"{service} {uri}"
        }
        return jwt.encode(jwt_payload, private_key, algorithm="ES256", headers={"kid": api_key_name, "nonce": secrets.token_hex()})

    def request(self, method, path, body=None):
        try:
            api_key_name, private_key = self._get_credentials()
            host = "api.coinbase.com"
            path_for_jwt = urllib.parse.urlparse(path).path
            token = self._build_jwt(api_key_name, private_key, method, f"{host}{path_for_jwt}")
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            response = requests.request(method.upper(), f"https://{host}{path}", headers=headers, json=body, timeout=15)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.error(f"Coinbase request failed: {e}")
        return None

    def get_balances(self):
        all_accounts = []
        path = "/api/v3/brokerage/accounts"
        while True:
            data = self.request("GET", path)
            if not data: break
            all_accounts.extend(data['accounts'])
            if not data.get('has_next'): break
            path = f"/api/v3/brokerage/accounts?cursor={data['cursor']}"
        
        balances = {"cash": {"USD": 0.0, "USDC": 0.0}, "crypto": {}}
        for acc in all_accounts:
            cur, val = acc['currency'], float(acc['available_balance']['value'])
            if cur in balances['cash']:
                balances['cash'][cur] = val
            elif val > 0:
                balances['crypto'][cur] = val
        return balances

    def get_market_data(self, product_id, window):
        path = f"/api/v3/brokerage/products/{product_id}/candles?limit={window + 10}&granularity=ONE_HOUR"
        data = self.request("GET", path)
        if data and 'candles' in data:
            df = pd.DataFrame(data['candles'], columns=['start', 'low', 'high', 'open', 'close', 'volume'])
            df['start'] = pd.to_datetime(pd.to_numeric(df['start']), unit='s')
            df[df.columns[1:]] = df[df.columns[1:]].apply(pd.to_numeric)
            return df.sort_values(by='start')
        return None

    def get_product_details(self, product_id):
        if product_id in self.product_details_cache:
            return self.product_details_cache[product_id]
        data = self.request("GET", f"/api/v3/brokerage/products/{product_id}")
        if data:
            self.product_details_cache[product_id] = data
        return data

    def cancel_open_orders(self, product_id):
        logging.info(f"Cancelling open orders for {product_id}...")
        path = f"/api/v3/brokerage/orders/historical/batch?order_status=OPEN&product_id={product_id}"
        orders = self.request("GET", path)
        if orders and "orders" in orders:
            order_ids = [o["order_id"] for o in orders["orders"]]
            if order_ids:
                logging.info(f"Cancelling {len(order_ids)} orders: {order_ids}")
                self.request("POST", "/api/v3/brokerage/orders/batch_cancel", {"order_ids": order_ids})
                time.sleep(1)

    def get_best_bid_ask(self, product_id):
        book = self.request("GET", f"/api/v3/brokerage/product_book?product_id={product_id}&limit=1")
        if book and "pricebook" in book and book["pricebook"]["bids"] and book["pricebook"]["asks"]:
            best_bid = float(book["pricebook"]["bids"][0]["price"])
            best_ask = float(book["pricebook"]["asks"][0]["price"])
            return best_bid, best_ask
        return None, None

    def _round_to_increment(self, amount, increment):
        inc = Decimal(str(increment))
        amt = Decimal(str(amount))
        return (amt // inc) * inc

    def place_limit_order(self, product_id, side, price, amount_quote_currency=None, amount_base_currency=None):
        if self.trading_mode == "live":
            self.cancel_open_orders(product_id)
        
        details = self.get_product_details(product_id)
        if not details: return None
        
        best_bid, best_ask = self.get_best_bid_ask(product_id)
        tick = float(details['quote_increment'])
        
        if side == 'BUY':
            price = min(price, best_ask - tick if best_ask else price)
        else:
            price = max(price, best_bid + tick if best_bid else price)

        order_id = str(uuid.uuid4())
        if side == 'BUY' and amount_quote_currency:
            base_size = float(amount_quote_currency) / float(price)
        else:
            base_size = amount_base_currency

        rounded_base = self._round_to_increment(base_size, details['base_increment'])
        rounded_price = self._round_to_increment(price, details['quote_increment'])

        payload = {
            "client_order_id": order_id, 
            "product_id": product_id, 
            "side": side, 
            "order_configuration": {
                "limit_limit_gtc": {
                    "base_size": str(rounded_base),
                    "limit_price": str(rounded_price),
                    "post_only": True
                }
            }
        }
        
        logging.info(f"Placing LIMIT {side} (Post-Only) for {product_id} at {rounded_price}")
        if self.trading_mode == "live":
            return self.request("POST", "/api/v3/brokerage/orders", payload)
        return {"success": True}

    def check_order_filled(self, order_id, max_attempts=5, poll_interval=2):
        """Poll order status until filled or timeout. Returns filled price or None."""
        for attempt in range(max_attempts):
            data = self.request("GET", f"/api/v3/brokerage/orders/historical/{order_id}")
            if not data or "order" not in data:
                logging.warning(f"Could not fetch order {order_id} (attempt {attempt + 1}/{max_attempts})")
                time.sleep(poll_interval)
                continue
            order = data["order"]
            status = order.get("status", "")
            if status == "FILLED":
                avg_price = float(order.get("average_filled_price", 0))
                filled_size = float(order.get("filled_size", 0))
                logging.info(f"Order {order_id} FILLED: {filled_size} @ ${avg_price:,.2f}")
                return avg_price
            elif status in ("CANCELLED", "EXPIRED", "FAILED"):
                logging.warning(f"Order {order_id} terminal status: {status}")
                return None
            else:
                logging.debug(f"Order {order_id} status: {status} (attempt {attempt + 1}/{max_attempts})")
                time.sleep(poll_interval)
        logging.warning(f"Order {order_id} not filled after {max_attempts} attempts")
        return None

    def place_market_order(self, product_id, side, amount_quote_currency=None, amount_base_currency=None):
        if self.trading_mode == "live":
            self.cancel_open_orders(product_id)
            
        details = self.get_product_details(product_id)
        if not details: return None
        order_id = str(uuid.uuid4())
        
        if side == 'BUY' and amount_quote_currency:
            base_size = float(amount_quote_currency) / float(details['price'])
        else:
            base_size = amount_base_currency

        rounded_base = self._round_to_increment(base_size, details['base_increment'])
        payload = {
            "client_order_id": order_id, 
            "product_id": product_id, 
            "side": side, 
            "order_configuration": {
                "market_market_ioc": {
                    "quote_size": str(amount_quote_currency) if amount_quote_currency else "",
                    "base_size": str(rounded_base)
                }
            }
        }
        
        logging.info(f"Placing MARKET {side} (IOC) for {product_id}")
        if self.trading_mode == "live":
            return self.request("POST", "/api/v3/brokerage/orders", payload)
        return {"success": True}
