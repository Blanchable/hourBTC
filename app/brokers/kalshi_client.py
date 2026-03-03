import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from app.config.defaults import settings

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except Exception:  # pragma: no cover
    hashes = serialization = padding = None

BTC_HOURLY_TRADE_SERIES_TICKER = "KXBTCD"
BTC_HOURLY_RANGE_SERIES_TICKER = "KXBTC"


@dataclass
class KalshiEnvironment:
    name: str
    base_url: str


ENVIRONMENTS = {
    "paper": KalshiEnvironment("paper", "https://demo-api.kalshi.co"),
    "production": KalshiEnvironment("production", "https://api.elections.kalshi.com"),
}


class KalshiRequestError(RuntimeError):
    pass


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
        try:
            resp.raise_for_status()
        except Exception as exc:
            raise KalshiRequestError(
                f"Kalshi request failed: {method} {path} status={getattr(resp, 'status_code', 'unknown')} body={resp.text}"
            ) from exc
        return resp.json()

    def connect(self) -> dict:
        return self.get_account_summary()

    def close(self) -> None:
        if self.http is not None:
            self.http.close()

    def get_account_summary(self) -> dict:
        return self._request("GET", "/trade-api/v2/portfolio/balance")

    def get_orders(self, status: str | None = None, ticker: str | None = None) -> dict:
        cursor = None
        rows = []
        while True:
            query = {"limit": 100}
            if status:
                query["status"] = status
            if ticker:
                query["ticker"] = ticker
            if cursor:
                query["cursor"] = cursor
            payload = self._request("GET", f"/trade-api/v2/portfolio/orders?{urlencode(query)}")
            rows.extend(payload.get("orders", []))
            cursor = payload.get("cursor")
            if not cursor:
                break
        return {"orders": rows}

    def get_open_orders(self, status: str = "resting") -> dict:
        return self.get_orders(status=status)

    def get_order_status(self, order_id: str) -> dict:
        return self._request("GET", f"/trade-api/v2/portfolio/orders/{order_id}")

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/trade-api/v2/portfolio/orders/{order_id}")

    def place_limit_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        limit_price: int,
        client_order_id: str,
        *,
        post_only: bool = False,
        time_in_force: str | None = None,
        reduce_only: bool = False,
        expiration_ts: int | None = None,
        cancel_order_on_pause: bool = True,
    ) -> dict:
        payload = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "type": "limit",
            "count": count,
            "client_order_id": client_order_id,
            "post_only": post_only,
            "reduce_only": reduce_only,
            "cancel_order_on_pause": cancel_order_on_pause,
        }
        if side == "yes":
            payload["yes_price"] = limit_price
        elif side == "no":
            payload["no_price"] = limit_price
        else:
            raise ValueError("side must be yes or no")
        if time_in_force:
            payload["time_in_force"] = time_in_force
        if expiration_ts is not None:
            payload["expiration_ts"] = expiration_ts
        return self._request("POST", "/trade-api/v2/portfolio/orders", payload)

    def place_entry_order(self, ticker: str, side: str, count: int, limit_price: int, client_order_id: str, **kwargs) -> dict:
        return self.place_limit_order(ticker, side, "buy", count, limit_price, client_order_id, **kwargs)

    def place_exit_order(self, ticker: str, side: str, count: int, limit_price: int, client_order_id: str, **kwargs) -> dict:
        return self.place_limit_order(ticker, side, "sell", count, limit_price, client_order_id, reduce_only=True, **kwargs)

    def get_orderbook_snapshot(self, ticker: str) -> dict:
        return self._request("GET", f"/trade-api/v2/markets/{ticker}/orderbook")

    def _get_all_markets_for_series(self, series_ticker: str, status: str | None = None) -> list[dict]:
        cursor = None
        rows = []
        while True:
            query = {"series_ticker": series_ticker, "limit": 100}
            if status:
                query["status"] = status
            if cursor:
                query["cursor"] = cursor
            payload = self._request("GET", f"/trade-api/v2/markets?{urlencode(query)}")
            rows.extend(payload.get("markets", []))
            cursor = payload.get("cursor")
            if not cursor:
                break
        return rows

    def _is_tradable_series_market(self, market: dict) -> bool:
        status = str(market.get("status", "")).lower()
        if status and status not in {"open", "active", "initialized", "paused"}:
            return False
        close = market.get("close_time")
        if close:
            try:
                if datetime.fromisoformat(close.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                    return False
            except Exception:
                pass
        return True

    def is_threshold_hourly_btc_market(self, row: dict) -> bool:
        title = str(row.get("title", "")).lower()
        return "bitcoin price today at" in title and "range" not in title

    def is_range_hourly_btc_market(self, row: dict) -> bool:
        return "bitcoin price range" in str(row.get("title", "")).lower()

    def get_open_hourly_trade_markets(self) -> list[dict]:
        series = settings.global_settings.btc_hourly_trade_series_ticker
        open_rows = self._get_all_markets_for_series(series, status="open")
        candidates = [r for r in open_rows if self.is_threshold_hourly_btc_market(r) and self._is_tradable_series_market(r)]
        if not candidates:
            fallback = self._get_all_markets_for_series(series, status=None)
            candidates = [r for r in fallback if self.is_threshold_hourly_btc_market(r) and self._is_tradable_series_market(r)]
            self.last_series_diagnostics = {
                "series_ticker": series,
                "open_count": len(open_rows),
                "fallback_count": len(fallback),
                "tradable_count": len(candidates),
            }
        else:
            self.last_series_diagnostics = {
                "series_ticker": series,
                "open_count": len(open_rows),
                "fallback_count": 0,
                "tradable_count": len(candidates),
            }
        return sorted(candidates, key=lambda m: (m.get("close_time", ""), m.get("ticker", "")))

    def get_open_hourly_range_markets(self) -> list[dict]:
        series = settings.global_settings.btc_hourly_range_series_ticker
        rows = self._get_all_markets_for_series(series, status="open")
        return [r for r in rows if self.is_range_hourly_btc_market(r)]

    def validate_hourly_series(self) -> dict:
        series = settings.global_settings.btc_hourly_trade_series_ticker
        try:
            self._request("GET", f"/trade-api/v2/series/{series}")
            return {"ok": True, "ticker": series}
        except Exception as exc:
            return {"ok": False, "ticker": series, "error": str(exc)}

    @staticmethod
    def extract_best_prices(orderbook: dict) -> dict:
        def _p(level):
            if isinstance(level, (list, tuple)) and level:
                try:
                    return float(level[0])
                except Exception:
                    return None
            if isinstance(level, dict):
                for k in ("price", "yes_price", "no_price", "value"):
                    if level.get(k) is not None:
                        try:
                            return float(level[k])
                        except Exception:
                            return None
            return None

        def _best(levels):
            if not isinstance(levels, list):
                return None
            vals = [v for v in (_p(x) for x in levels) if v is not None and 0 <= v <= 100]
            return max(vals) if vals else None

        root = orderbook.get("orderbook", orderbook) if isinstance(orderbook, dict) else {}
        yes_bid = _best(root.get("yes") or root.get("yes_bids"))
        no_bid = _best(root.get("no") or root.get("no_bids"))
        yes_ask = 100 - no_bid if no_bid is not None else None
        no_ask = 100 - yes_bid if yes_bid is not None else None
        if yes_ask is not None and not (0 <= yes_ask <= 100):
            yes_ask = None
        if no_ask is not None and not (0 <= no_ask <= 100):
            no_ask = None
        return {"yes_bid": yes_bid, "yes_ask": yes_ask, "no_bid": no_bid, "no_ask": no_ask}

    def select_best_threshold_contract(self, markets: list[dict], spot_price: float | None) -> dict | None:
        if not markets:
            return None
        parsed = []
        for m in markets:
            strike = parse_btc_threshold(m)
            if strike is not None:
                parsed.append((m, strike))
        if not parsed:
            mid = sorted(markets, key=lambda x: x.get("ticker", ""))[len(markets) // 2]
            return mid
        if spot_price is None:
            parsed_sorted = sorted(parsed, key=lambda x: x[1])
            return parsed_sorted[len(parsed_sorted) // 2][0]
        parsed_sorted = sorted(parsed, key=lambda x: (abs(x[1] - spot_price), x[0].get("ticker", "")))
        return parsed_sorted[0][0]

    def resolve_btc_hourly_trade_target_market(self, now: datetime | None = None, spot_price: float | None = None) -> dict | None:
        now = now or datetime.now(timezone.utc)
        markets = self.get_open_hourly_trade_markets()
        if not markets:
            return None
        next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        by_close = {}
        for m in markets:
            close = m.get("close_time")
            if not close:
                continue
            try:
                close_dt = datetime.fromisoformat(close.replace("Z", "+00:00"))
            except Exception:
                continue
            by_close.setdefault(close_dt, []).append(m)
        if not by_close:
            return None
        eligible = [c for c in by_close if c >= next_hour]
        chosen_close = min(eligible) if eligible else min([c for c in by_close if c > now], default=min(by_close))
        return self.select_best_threshold_contract(by_close.get(chosen_close, []), spot_price)

    def get_hourly_series_quote_rows(self, markets: list[dict] | None = None) -> list[dict]:
        rows = []
        source = markets if markets is not None else self.get_open_hourly_trade_markets()
        for market in source:
            ticker = market.get("ticker", "")
            quotes = {"yes_bid": None, "yes_ask": None, "no_bid": None, "no_ask": None}
            err = None
            try:
                quotes = self.extract_best_prices(self.get_orderbook_snapshot(ticker))
            except Exception as exc:
                err = str(exc)
            rows.append(
                {
                    "ticker": ticker,
                    "title": market.get("title", ""),
                    "close_time": market.get("close_time", ""),
                    "strike": parse_btc_threshold(market),
                    "status": market.get("status", ""),
                    "event_ticker": market.get("event_ticker", ""),
                    **quotes,
                    "last_update": datetime.now(timezone.utc).isoformat(),
                    "error": err,
                }
            )
        return rows



def parse_btc_threshold(market: dict) -> Optional[float]:
    text = " ".join([market.get("title", ""), market.get("subtitle", ""), market.get("ticker", "")])
    match = re.search(r"(?:above|over|below|under|>=|<=|>|<|at)\s*\$?([0-9]{2,}(?:,[0-9]{3})*(?:\.[0-9]+)?)", text, re.I)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))
