"""CoW Protocol order book API on Base: quotes, order submission and status."""
import requests

BASE_URL = "https://api.cow.fi/base/api/v1"
TIMEOUT_SECONDS = 15


class CowError(RuntimeError):
    pass


class CowClient:
    def __init__(self, session=None):
        self._session = session or requests.Session()

    def _request(self, method, path, **kwargs):
        try:
            response = self._session.request(method, f"{BASE_URL}{path}", timeout=TIMEOUT_SECONDS, **kwargs)
        except requests.RequestException as e:
            raise CowError(f"{path} request failed: {e}") from None
        try:
            body = response.json()
        except ValueError:
            body = None
        if not response.ok:
            detail = body.get("description") or body.get("errorType") if isinstance(body, dict) else response.text[:200]
            raise CowError(f"{path} HTTP {response.status_code}: {detail}")
        return body

    def quote(self, body):
        return self._request("POST", "/quote", json=body)

    def post_order(self, body):
        """Returns the order uid."""
        return self._request("POST", "/orders", json=body)

    def order(self, uid):
        return self._request("GET", f"/orders/{uid}")

    def trades(self, uid):
        return self._request("GET", "/trades", params={"orderUid": uid})
