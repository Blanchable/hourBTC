import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except Exception:  # pragma: no cover
    hashes = serialization = padding = None

BTC_1H_SERIES_TICKER = "KXBTC"


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
        self.last_series_diagnostics: dict = {}

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

    def _get_all_markets_for_series(self, series_ticker: str, status: str | None = None) -> list[dict]:
        cursor: str | None = None
        rows: list[dict] = []
        while True:
            query = {"series_ticker": series_ticker, "limit": 100}
            if status:
                query["status"] = status
            if cursor:
                query["cursor"] = cursor
            path = f"/trade-api/v2/markets?{urlencode(query)}"
            payload = self._request("GET", path)
            markets = payload.get("markets", [])
            if isinstance(markets, list):
                rows.extend(markets)
            cursor = payload.get("cursor")
            if not cursor:
                break
        return rows

    def list_series_markets(self) -> dict:
        return {"markets": self._get_all_markets_for_series(BTC_1H_SERIES_TICKER, status="open")}

    def list_open_markets(self) -> dict:
        return self._request("GET", "/trade-api/v2/markets?status=open")

    def validate_hourly_series(self) -> dict:
        try:
            self._request("GET", f"/trade-api/v2/series/{BTC_1H_SERIES_TICKER}")
            return {"ok": True, "ticker": BTC_1H_SERIES_TICKER}
        except Exception as exc:
            return {"ok": False, "ticker": BTC_1H_SERIES_TICKER, "error": str(exc)}

    def _is_tradable_series_market(self, market: dict) -> bool:
        status = str(market.get("status", "")).lower()
        if status and status not in {"open", "active", "initialized", "paused"}:
            return False
        close = market.get("close_time")
        if close:
            try:
                close_dt = datetime.fromisoformat(close.replace("Z", "+00:00"))
                if close_dt <= datetime.now(timezone.utc):
                    return False
            except Exception:
                pass
        return True

    def get_open_hourly_series_markets(self) -> list[dict]:
        open_rows = self._get_all_markets_for_series(BTC_1H_SERIES_TICKER, status="open")
        if open_rows:
            self.last_series_diagnostics = {
                "series_ticker": BTC_1H_SERIES_TICKER,
                "open_count": len(open_rows),
                "fallback_count": 0,
                "tradable_count": len(open_rows),
                "used_fallback": False,
            }
            return sorted(open_rows, key=lambda m: (m.get("close_time", ""), m.get("ticker", "")))

        fallback_rows = self._get_all_markets_for_series(BTC_1H_SERIES_TICKER, status=None)
        tradable = [m for m in fallback_rows if self._is_tradable_series_market(m)]
        self.last_series_diagnostics = {
            "series_ticker": BTC_1H_SERIES_TICKER,
            "open_count": 0,
            "fallback_count": len(fallback_rows),
            "tradable_count": len(tradable),
            "used_fallback": True,
        }
        return sorted(tradable, key=lambda m: (m.get("close_time", ""), m.get("ticker", "")))

    @staticmethod
    def extract_best_prices(orderbook: dict) -> dict:
        def _price_from_level(level):
            if isinstance(level, (list, tuple)) and level:
                try:
                    return float(level[0])
                except (TypeError, ValueError):
                    return None
            if isinstance(level, dict):
                for key in ("price", "yes_price", "no_price", "value"):
                    if level.get(key) is not None:
                        try:
                            return float(level[key])
                        except (TypeError, ValueError):
                            return None
            return None

        def _best_bid(levels):
            if not isinstance(levels, list):
                return None
            prices = [p for p in (_price_from_level(level) for level in levels) if p is not None and 0 <= p <= 100]
            return max(prices) if prices else None

        root = orderbook.get("orderbook", orderbook) if isinstance(orderbook, dict) else {}
        yes_levels = root.get("yes")
        no_levels = root.get("no")
        if yes_levels is None and isinstance(root.get("yes_bids"), list):
            yes_levels = root.get("yes_bids")
        if no_levels is None and isinstance(root.get("no_bids"), list):
            no_levels = root.get("no_bids")

        yes_bid = _best_bid(yes_levels)
        no_bid = _best_bid(no_levels)

        yes_ask = (100 - no_bid) if no_bid is not None else None
        no_ask = (100 - yes_bid) if yes_bid is not None else None

        if yes_ask is not None and not (0 <= yes_ask <= 100):
            yes_ask = None
        if no_ask is not None and not (0 <= no_ask <= 100):
            no_ask = None

        return {
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
        }

    def get_hourly_series_quote_rows(self) -> list[dict]:
        rows = []
        for market in self.get_open_hourly_series_markets():
            ticker = market.get("ticker", "")
            best = {"yes_bid": None, "yes_ask": None, "no_bid": None, "no_ask": None}
            error = None
            try:
                orderbook = self.get_orderbook_snapshot(ticker)
                best = self.extract_best_prices(orderbook)
            except Exception as exc:
                error = str(exc)
            rows.append(
                {
                    "ticker": ticker,
                    "title": market.get("title", ""),
                    "close_time": market.get("close_time", ""),
                    "strike": parse_btc_threshold(market),
                    "status": market.get("status", ""),
                    "event_ticker": market.get("event_ticker", ""),
                    **best,
                    "last_update": datetime.now(timezone.utc).isoformat(),
                    "error": error,
                }
            )
        return rows

    def resolve_btc_target_market(self) -> Optional[dict]:
        series_rows = self.get_open_hourly_series_markets()
        if series_rows:
            return series_rows[0]

        fallback = self.list_open_markets().get("markets", [])
        candidates = []
        for market in fallback:
            title = market.get("title", "")
            if "BTC" not in title.upper():
                continue
            if self._close_minute(market) != 0:
                continue
            if not self._is_tradable_series_market(market):
                continue
            candidates.append(market)
        return sorted(candidates, key=lambda m: (m.get("close_time", ""), m.get("ticker", "")))[0] if candidates else None

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
