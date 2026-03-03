import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except Exception:  # pragma: no cover
    hashes = serialization = padding = None

BTC_1H_SERIES_TICKER = "KXBTC1H"


@dataclass
class KalshiEnvironment:
    name: str
    base_url: str


ENVIRONMENTS = {
    "paper": KalshiEnvironment("paper", "https://demo-api.kalshi.co"),
    "production": KalshiEnvironment("production", "https://api.elections.kalshi.com"),
}


class KalshiClient:
    def __init__(self, environment: str, api_key_id: str, private_key_path: str):
        self.environment = ENVIRONMENTS[environment]
        self.api_key_id = api_key_id
        self.private_key_path = private_key_path
        self.http = httpx.Client(base_url=self.environment.base_url, timeout=10) if httpx else None

    def _sign(self, message: bytes) -> str:
        if not serialization or not padding or not hashes:
            raise RuntimeError("cryptography dependency missing")
        with open(self.private_key_path, "rb") as fp:
            key = serialization.load_pem_private_key(fp.read(), password=None)
        sig = key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def _auth_headers(self, method: str, path: str, body: str = "") -> dict:
        ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        payload = f"{ts}{method.upper()}{path}{body}".encode()
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._sign(payload),
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        text = json.dumps(body) if body else ""
        headers = self._auth_headers(method, path, text)
        if self.http is None:
            raise RuntimeError("httpx dependency missing")
        resp = self.http.request(method, path, headers=headers, content=text if text else None)
        resp.raise_for_status()
        return resp.json()

    def connect(self) -> dict:
        return self.get_account_summary()

    def close(self) -> None:
        if self.http is not None:
            self.http.close()

    def get_account_summary(self) -> dict:
        return self._request("GET", "/trade-api/v2/portfolio/balance")

    def get_open_orders(self) -> dict:
        return self._request("GET", "/trade-api/v2/portfolio/orders?status=open")

    def get_order_status(self, order_id: str) -> dict:
        return self._request("GET", f"/trade-api/v2/portfolio/orders/{order_id}")

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/trade-api/v2/portfolio/orders/{order_id}")

    def place_limit_order(self, ticker: str, side: str, count: int, limit_price: int, client_order_id: str) -> dict:
        return self._request(
            "POST",
            "/trade-api/v2/portfolio/orders",
            {
                "ticker": ticker,
                "side": side,
                "type": "limit",
                "count": count,
                "yes_price": limit_price if side == "yes" else None,
                "no_price": limit_price if side == "no" else None,
                "client_order_id": client_order_id,
            },
        )

    def get_orderbook_snapshot(self, ticker: str) -> dict:
        return self._request("GET", f"/trade-api/v2/markets/{ticker}/orderbook")

    def list_series_markets(self) -> dict:
        return self._request("GET", f"/trade-api/v2/markets?series_ticker={BTC_1H_SERIES_TICKER}&status=open")

    def list_open_markets(self) -> dict:
        return self._request("GET", "/trade-api/v2/markets?status=open")

    def get_open_hourly_series_markets(self) -> list[dict]:
        markets = self.list_series_markets().get("markets", [])
        return sorted(markets, key=lambda m: (m.get("close_time", ""), m.get("ticker", "")))

    @staticmethod
    def extract_best_prices(orderbook: dict) -> dict:
        def _price_from_level(level):
            if isinstance(level, dict):
                for key in ("price", "yes_price", "no_price", "value"):
                    if key in level and level[key] is not None:
                        try:
                            return float(level[key])
                        except (TypeError, ValueError):
                            return None
            if isinstance(level, (list, tuple)) and level:
                try:
                    return float(level[0])
                except (TypeError, ValueError):
                    return None
            return None

        def _extract_list(root: dict, direct_key: str, nested_key: str, side_name: str):
            direct = root.get(direct_key)
            if isinstance(direct, list):
                return direct
            side = root.get(side_name)
            if isinstance(side, dict) and isinstance(side.get(nested_key), list):
                return side.get(nested_key)
            return []

        def _best(levels: list, mode: str):
            prices = [p for p in (_price_from_level(level) for level in levels) if p is not None]
            if not prices:
                return None
            return max(prices) if mode == "bid" else min(prices)

        root = orderbook.get("orderbook", orderbook) if isinstance(orderbook, dict) else {}
        yes_bids = _extract_list(root, "yes_bids", "bids", "yes")
        yes_asks = _extract_list(root, "yes_asks", "asks", "yes")
        no_bids = _extract_list(root, "no_bids", "bids", "no")
        no_asks = _extract_list(root, "no_asks", "asks", "no")

        if not yes_bids and isinstance(root.get("yes"), list):
            yes_bids = root.get("yes", [])
        if not no_bids and isinstance(root.get("no"), list):
            no_bids = root.get("no", [])

        return {
            "yes_bid": _best(yes_bids, "bid"),
            "yes_ask": _best(yes_asks, "ask"),
            "no_bid": _best(no_bids, "bid"),
            "no_ask": _best(no_asks, "ask"),
        }

    def get_hourly_series_quote_rows(self) -> list[dict]:
        rows = []
        for market in self.get_open_hourly_series_markets():
            ticker = market.get("ticker", "")
            try:
                orderbook = self.get_orderbook_snapshot(ticker)
                best = self.extract_best_prices(orderbook)
            except Exception:
                best = {"yes_bid": None, "yes_ask": None, "no_bid": None, "no_ask": None}
            rows.append(
                {
                    "ticker": ticker,
                    "title": market.get("title", ""),
                    "close_time": market.get("close_time", ""),
                    "strike": parse_btc_threshold(market),
                    **best,
                    "last_update": datetime.now(timezone.utc).isoformat(),
                }
            )
        return rows

    def resolve_btc_target_market(self) -> Optional[dict]:
        data = self.list_series_markets().get("markets", [])
        if data:
            return sorted(data, key=lambda m: m.get("close_time", ""))[0]
        fallback = self.list_open_markets().get("markets", [])
        hourly = [m for m in fallback if "BTC" in m.get("title", "") and self._close_minute(m) == 0]
        return sorted(hourly, key=lambda m: m.get("close_time", ""))[0] if hourly else None

    def _close_minute(self, market: dict) -> int:
        close = market.get("close_time")
        if not close:
            return -1
        return datetime.fromisoformat(close.replace("Z", "+00:00")).minute


def parse_btc_threshold(market: dict) -> Optional[float]:
    text = " ".join([market.get("title", ""), market.get("subtitle", ""), market.get("ticker", "")])
    match = re.search(r"(?:above|over|below|under|>=|<=|>|<)\s*\$?([0-9]{2,}(?:,[0-9]{3})*(?:\.[0-9]+)?)", text, re.I)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))
