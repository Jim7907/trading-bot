"""Trading 212 REST API client."""

import time
import base64
import logging
import requests

log = logging.getLogger("t212")


class T212Client:
    def __init__(self, api_key: str, base_url: str, api_secret: str = ""):
        # Build Authorization header:
        # - If both key + secret supplied: Basic base64(key:secret)
        # - Otherwise: just the API key directly
        if api_secret:
            token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
            auth_header = f"Basic {token}"
        else:
            auth_header = api_key

        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": auth_header,
            "Content-Type":  "application/json",
        })
        self.base = base_url
        self._last_order = 0.0

    def _get(self, path, params=None):
        r = self.session.get(f"{self.base}{path}", params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def _post(self, path, body):
        r = self.session.post(f"{self.base}{path}", json=body, timeout=15)
        r.raise_for_status()
        return r.json()

    def _delete(self, path):
        r = self.session.delete(f"{self.base}{path}", timeout=15)
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()

    def _throttle(self):
        elapsed = time.monotonic() - self._last_order
        if elapsed < 1.1:
            time.sleep(1.1 - elapsed)
        self._last_order = time.monotonic()

    # Account
    def get_account_info(self):  return self._get("/equity/account/info")
    def get_cash(self):          return self._get("/equity/account/cash")

    # Portfolio
    def get_portfolio(self):     return self._get("/equity/portfolio")
    def get_position(self, ticker):
        try:
            return self._get(f"/equity/portfolio/{ticker}")
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                return None
            raise

    # Orders
    def get_orders(self):        return self._get("/equity/orders")
    def get_order(self, oid):    return self._get(f"/equity/orders/{oid}")
    def cancel_order(self, oid):
        self._throttle()
        self._delete(f"/equity/orders/{oid}")

    def place_limit(self, ticker, qty, price, validity="DAY"):
        self._throttle()
        body = {"ticker": ticker, "quantity": qty,
                "limitPrice": round(price, 4), "timeValidity": validity}
        r = self._post("/equity/orders/limit", body)
        log.info(f"LIMIT {ticker} qty={qty} @ {price} -> id={r.get('id')}")
        return r

    def place_stop(self, ticker, qty, stop_price, validity="GOOD_TILL_CANCEL"):
        self._throttle()
        body = {"ticker": ticker, "quantity": qty,
                "stopPrice": round(stop_price, 4), "timeValidity": validity}
        r = self._post("/equity/orders/stop", body)
        log.info(f"STOP {ticker} qty={qty} stop={stop_price} -> id={r.get('id')}")
        return r

    def place_market(self, ticker, qty):
        self._throttle()
        r = self._post("/equity/orders/market", {"ticker": ticker, "quantity": qty})
        log.info(f"MARKET {ticker} qty={qty} -> id={r.get('id')}")
        return r

    def cancel_all_for(self, ticker):
        for o in self.get_orders():
            if o.get("ticker") == ticker and o.get("status") in ("PENDING", "NEW"):
                try:
                    self.cancel_order(o["id"])
                except Exception as e:
                    log.warning(f"cancel {o['id']}: {e}")
