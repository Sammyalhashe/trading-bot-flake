"""Coinbase Advanced Trade WebSocket client for real-time price ticks."""
import asyncio
import json
import logging
import time

import websockets

WS_URL = "wss://advanced-trade-ws.coinbase.com"
JWT_REFRESH_INTERVAL = 90  # seconds (JWT expires at 120s)
MAX_BACKOFF = 60  # seconds


class CoinbaseWSClient:
    """Connects to Coinbase WS, dispatches ticks, runs periodic scans."""

    def __init__(self, jwt_builder, product_ids, on_tick, on_scan_cycle,
                 scan_interval, shutdown_event):
        self._jwt_builder = jwt_builder
        self._product_ids = list(product_ids)
        self._on_tick = on_tick
        self._on_scan_cycle = on_scan_cycle
        self._scan_interval = scan_interval
        self._shutdown = shutdown_event
        self._lock = asyncio.Lock()
        self._ws = None
        self._consecutive_failures = 0

    def update_subscriptions(self, product_ids):
        self._product_ids = list(product_ids)

    async def run(self):
        """Reconnect loop with exponential backoff."""
        backoff = 1
        while not self._shutdown.is_set():
            try:
                await self._connect_and_listen()
                backoff = 1  # reset on clean disconnect
            except Exception as e:
                self._consecutive_failures += 1
                if self._consecutive_failures >= 10:
                    logging.critical(
                        f"WebSocket: {self._consecutive_failures} consecutive "
                        f"failures. Last error: {e}"
                    )
                else:
                    logging.error(f"WebSocket disconnected: {e}. "
                                  f"Reconnecting in {backoff}s...")
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=backoff)
                    return  # shutdown requested during backoff
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, MAX_BACKOFF)

    async def _connect_and_listen(self):
        jwt_token = self._jwt_builder()
        async with websockets.connect(WS_URL, ping_interval=20,
                                      ping_timeout=10) as ws:
            self._ws = ws
            await self._subscribe(ws, jwt_token)
            logging.info(f"WebSocket connected, subscribed to "
                         f"{len(self._product_ids)} products")

            refresh_task = asyncio.create_task(self._jwt_refresh_loop(ws))
            scan_task = asyncio.create_task(self._scan_loop())

            try:
                async for raw in ws:
                    if self._shutdown.is_set():
                        break
                    self._consecutive_failures = 0
                    await self._handle_message(raw)
            finally:
                refresh_task.cancel()
                scan_task.cancel()
                await asyncio.gather(refresh_task, scan_task,
                                     return_exceptions=True)

    async def _subscribe(self, ws, jwt_token):
        msg = {
            "type": "subscribe",
            "product_ids": self._product_ids,
            "channel": "ticker",
            "jwt": jwt_token,
        }
        await ws.send(json.dumps(msg))
        # Also subscribe to heartbeats for connection health
        hb = {
            "type": "subscribe",
            "product_ids": self._product_ids,
            "channel": "heartbeats",
            "jwt": jwt_token,
        }
        await ws.send(json.dumps(hb))

    async def _handle_message(self, raw):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        channel = data.get("channel")
        if channel != "ticker":
            return

        events = data.get("events", [])
        for event in events:
            for ticker in event.get("tickers", []):
                product_id = ticker.get("product_id")
                price_str = ticker.get("price")
                if product_id and price_str:
                    try:
                        price = float(price_str)
                    except (ValueError, TypeError):
                        continue
                    async with self._lock:
                        try:
                            self._on_tick(product_id, price)
                        except Exception as e:
                            logging.error(
                                f"on_tick error for {product_id}: {e}")

    async def _jwt_refresh_loop(self, ws):
        """Re-subscribe with fresh JWT every 90 seconds."""
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(),
                                       timeout=JWT_REFRESH_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            try:
                jwt_token = self._jwt_builder()
                await self._subscribe(ws, jwt_token)
                logging.debug("WebSocket JWT refreshed")
            except Exception as e:
                logging.error(f"JWT refresh failed: {e}")

    async def _scan_loop(self):
        """Run full entry/exit scan every scan_interval seconds."""
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(),
                                       timeout=self._scan_interval)
                return
            except asyncio.TimeoutError:
                pass
            async with self._lock:
                try:
                    await asyncio.to_thread(self._on_scan_cycle)
                except Exception as e:
                    logging.error(f"Scan cycle error: {e}")
