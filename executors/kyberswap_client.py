"""KyberSwap aggregator API on Base: route quote and calldata build. Callers validate the calldata."""
import requests

BASE_URL = "https://aggregator-api.kyberswap.com/base/api/v1"
CLIENT_ID = "trading-bot"
TIMEOUT_SECONDS = 15


class KyberSwapError(RuntimeError):
    pass


class KyberSwapClient:
    def __init__(self, session=None):
        self._session = session or requests.Session()
        self._headers = {"x-client-id": CLIENT_ID}

    def _data(self, response):
        try:
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as e:
            raise KyberSwapError(f"HTTP error: {e}") from None
        if body.get("code") != 0 or not body.get("data"):
            raise KyberSwapError(f"code {body.get('code')}: {body.get('message')}")
        return body["data"]

    def routes(self, token_in, token_out, amount_in):
        """Best route summary for an exact-input swap."""
        try:
            response = self._session.get(
                f"{BASE_URL}/routes", headers=self._headers, timeout=TIMEOUT_SECONDS,
                params={"tokenIn": token_in, "tokenOut": token_out, "amountIn": str(amount_in)})
        except requests.RequestException as e:
            raise KyberSwapError(f"routes request failed: {e}") from None
        return self._data(response)["routeSummary"]

    def build(self, route_summary, sender, recipient, slippage_bps):
        """Router calldata for route_summary: {routerAddress, data, amountOut, gas, transactionValue}."""
        try:
            response = self._session.post(
                f"{BASE_URL}/route/build", headers=self._headers, timeout=TIMEOUT_SECONDS,
                json={"routeSummary": route_summary, "sender": sender, "recipient": recipient,
                      "slippageTolerance": slippage_bps})
        except requests.RequestException as e:
            raise KyberSwapError(f"build request failed: {e}") from None
        return self._data(response)
