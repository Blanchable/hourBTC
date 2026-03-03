import base64
import json
import re
import time
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


class KalshiRateLimitError(KalshiRequestError):
    pass


class KalshiClient:
    def __init__(self, environment: str, api_key_id: str, private_key_path: str):
        self.environment = ENVIRONMENTS[environment]
        self.api_key_id = api_key_id
        self.private_key_path = private_key_path
        self.http = httpx.Client(base_url=self.environment.base_url, timeout=10) if httpx else None
        self.last_series_diagnostics: dict = {}
        self._market_cache_rows: list[dict] = []
        self._market_cache_ts: float = 0.0

    def _sign(self, message: bytes) -> str:
        if not serialization or not padding or not hashes:
            raise RuntimeError("cryptography dependency missing")
        with open(self.private_key_path, "rb") as fp:
            key = serialization.load_pem_private_key(fp.read(), password=None)
        sig = key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
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
        if self.http is None:
            raise RuntimeError("httpx dependency missing")
        resp = self.http.request(method, path, headers=self._auth_headers(method, path, text), content=text if text else None)
        try:
            resp.raise_for_status()
        except Exception as exc:
            status = getattr(resp, "status_code", "unknown")
            msg = f"Kalshi request failed: {method} {path} status={status} body={resp.text}"
            if status == 429:
                retry_after = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
                raise KalshiRateLimitError(f"{msg} retry_after={retry_after}") from exc
            raise KalshiRequestError(msg) from exc
        return resp.json()

    def connect(self) -> dict:
        return self.get_account_summary()

    def close(self) -> None:
        if self.http is not None:
            self.http.close()

    def get_account_summary(self) -> dict:
        return self._request("GET", "/trade-api/v2/portfolio/balance")

    def get_orders(self, status: str | None = None, ticker: str | None = None) -> dict:
        cursor, rows = None, []
        while True:
            q = {"limit": 100}
            if status:
                q["status"] = status
            if ticker:
                q["ticker"] = ticker
            if cursor:
                q["cursor"] = cursor
            payload = self._request("GET", f"/trade-api/v2/portfolio/orders?{urlencode(q)}")
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

    def place_limit_order(self, ticker: str, side: str, action: str, count: int, limit_price: int, client_order_id: str, **kwargs) -> dict:
        payload = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "type": "limit",
            "count": count,
            "client_order_id": client_order_id,
            "post_only": kwargs.get("post_only", False),
            "reduce_only": kwargs.get("reduce_only", False),
            "cancel_order_on_pause": kwargs.get("cancel_order_on_pause", True),
        }
        if side == "yes":
            payload["yes_price"] = limit_price
        elif side == "no":
            payload["no_price"] = limit_price
        else:
            raise ValueError("side must be yes or no")
        if kwargs.get("time_in_force"):
            payload["time_in_force"] = kwargs["time_in_force"]
        if kwargs.get("expiration_ts") is not None:
            payload["expiration_ts"] = kwargs["expiration_ts"]
        return self._request("POST", "/trade-api/v2/portfolio/orders", payload)

    def place_entry_order(self, ticker: str, side: str, count: int, limit_price: int, client_order_id: str, **kwargs) -> dict:
        return self.place_limit_order(ticker, side, "buy", count, limit_price, client_order_id, **kwargs)

    def place_exit_order(self, ticker: str, side: str, count: int, limit_price: int, client_order_id: str, **kwargs) -> dict:
        return self.place_limit_order(ticker, side, "sell", count, limit_price, client_order_id, reduce_only=True, **kwargs)

    def get_orderbook_snapshot(self, ticker: str) -> dict:
        return self._request("GET", f"/trade-api/v2/markets/{ticker}/orderbook")

    def _get_all_markets_for_series(
        self,
        series_ticker: str,
        status: str | None = None,
        close_time_start_ts: int | None = None,
        close_time_end_ts: int | None = None,
    ) -> list[dict]:
        cursor, rows = None, []
        while True:
            q = {"series_ticker": series_ticker, "limit": 100}
            if status:
                q["status"] = status
            if close_time_start_ts is not None:
                q["close_time_start_ts"] = int(close_time_start_ts)
            if close_time_end_ts is not None:
                q["close_time_end_ts"] = int(close_time_end_ts)
            if cursor:
                q["cursor"] = cursor
            payload = self._request("GET", f"/trade-api/v2/markets?{urlencode(q)}")
            rows.extend(payload.get("markets", []))
            cursor = payload.get("cursor")
            if not cursor:
                break
        return rows

    def _is_tradable(self, row: dict, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        status = str(row.get("status", "")).lower()
        if status in {"closed", "settled", "determined", "finalized", "expired"}:
            return False
        close = row.get("close_time")
        if not close:
            return False
        try:
            close_dt = datetime.fromisoformat(close.replace("Z", "+00:00"))
        except Exception:
            return False
        return close_dt > now - timedelta(minutes=2)

    def is_threshold_hourly_btc_market(self, row: dict) -> bool:
        t = str(row.get("title", "")).lower()
        return "bitcoin price today at" in t and "range" not in t

    def is_range_hourly_btc_market(self, row: dict) -> bool:
        return "bitcoin price range" in str(row.get("title", "")).lower()

    def is_live_trade_candidate(self, row: dict, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        series = str(row.get("series_ticker", settings.global_settings.btc_hourly_trade_series_ticker))
        if series != settings.global_settings.btc_hourly_trade_series_ticker:
            return False
        if not self.is_threshold_hourly_btc_market(row):
            return False
        if self.is_range_hourly_btc_market(row):
            return False
        if not self._is_tradable(row, now=now):
            return False
        status = str(row.get("status", "")).lower()
        return status in {"", "open", "active", "initialized", "paused", "inactive"}

    def filter_live_trade_candidates(self, rows: list[dict], now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        out = [r for r in rows if self.is_live_trade_candidate(r, now=now)]
        return sorted(out, key=lambda m: (m.get("close_time", ""), m.get("ticker", "")))

    def _get_windowed_trade_markets(self, now: datetime | None = None, fallback_window_hours: int | None = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        fallback_window_hours = fallback_window_hours or settings.global_settings.fallback_window_hours
        window_start = now - timedelta(hours=1)
        window_end = now + timedelta(hours=fallback_window_hours)
        rows = self._get_all_markets_for_series(
            settings.global_settings.btc_hourly_trade_series_ticker,
            status=None,
            close_time_start_ts=int(window_start.timestamp()),
            close_time_end_ts=int(window_end.timestamp()),
        )
        bounded = []
        for row in rows:
            try:
                close_dt = datetime.fromisoformat(str(row.get("close_time", "")).replace("Z", "+00:00"))
            except Exception:
                continue
            if window_start <= close_dt <= window_end:
                bounded.append(row)
        return bounded

    def get_open_hourly_trade_markets_live(self, force_refresh: bool = False, cache_ttl_seconds: int | None = None) -> list[dict]:
        now = time.time()
        ttl = cache_ttl_seconds or settings.global_settings.market_list_refresh_seconds
        if not force_refresh and self._market_cache_rows and (now - self._market_cache_ts) < ttl:
            return self._market_cache_rows
        open_rows = self._get_all_markets_for_series(settings.global_settings.btc_hourly_trade_series_ticker, status="open")
        filtered = self.filter_live_trade_candidates(open_rows)
        self._market_cache_rows = filtered
        self._market_cache_ts = now
        self.last_series_diagnostics = {
            "series_ticker": settings.global_settings.btc_hourly_trade_series_ticker,
            "open_query_count": len(open_rows),
            "fallback_query_count": 0,
            "used_fallback": False,
            "window_start_ts": None,
            "window_end_ts": None,
            "candidate_count": len(filtered),
        }
        return filtered

    def get_live_candidate_trade_markets(
        self,
        now: datetime | None = None,
        force_refresh: bool = False,
        fallback_window_hours: int | None = None,
        cache_ttl_seconds: int | None = None,
    ) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        open_rows = self.get_open_hourly_trade_markets_live(force_refresh=force_refresh, cache_ttl_seconds=cache_ttl_seconds)
        open_diag = dict(self.last_series_diagnostics or {})
        if open_rows:
            return open_rows

        bounded = self._get_windowed_trade_markets(now=now, fallback_window_hours=fallback_window_hours)
        candidates = self.filter_live_trade_candidates(bounded, now=now)
        window_start = (now - timedelta(hours=1)).isoformat()
        window_end = (now + timedelta(hours=(fallback_window_hours or settings.global_settings.fallback_window_hours))).isoformat()
        self.last_series_diagnostics = {
            "series_ticker": settings.global_settings.btc_hourly_trade_series_ticker,
            "open_query_count": open_diag.get("open_query_count", 0),
            "fallback_query_count": len(bounded),
            "used_fallback": True,
            "window_start_ts": window_start,
            "window_end_ts": window_end,
            "candidate_count": len(candidates),
        }
        self._market_cache_rows = candidates
        self._market_cache_ts = time.time()
        return candidates

    def get_open_hourly_trade_markets_debug(self) -> list[dict]:
        open_rows = self._get_all_markets_for_series(settings.global_settings.btc_hourly_trade_series_ticker, status="open")
        rows = self.filter_live_trade_candidates(open_rows)
        if rows:
            return rows
        fallback = self._get_all_markets_for_series(settings.global_settings.btc_hourly_trade_series_ticker, status=None)
        return self.filter_live_trade_candidates(fallback)

    def get_open_hourly_range_markets(self) -> list[dict]:
        rows = self._get_all_markets_for_series(settings.global_settings.btc_hourly_range_series_ticker, status="open")
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
        def _price(level):
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
            vals = [v for v in (_price(x) for x in levels) if v is not None and 0 <= v <= 100]
            return max(vals) if vals else None

        root = orderbook.get("orderbook", orderbook) if isinstance(orderbook, dict) else {}
        yes_bid = _best(root.get("yes") or root.get("yes_bids"))
        no_bid = _best(root.get("no") or root.get("no_bids"))
        yes_ask = (100 - no_bid) if no_bid is not None else None
        no_ask = (100 - yes_bid) if yes_bid is not None else None
        return {"yes_bid": yes_bid, "yes_ask": yes_ask, "no_bid": no_bid, "no_ask": no_ask}

    def select_best_threshold_contract(self, markets: list[dict], spot_price: float | None) -> dict | None:
        if not markets:
            return None
        parsed = [(m, parse_btc_threshold(m)) for m in markets]
        parsed = [(m, s) for m, s in parsed if s is not None]
        if not parsed:
            s = sorted(markets, key=lambda x: x.get("ticker", ""))
            return s[len(s) // 2]
        if spot_price is None:
            p = sorted(parsed, key=lambda x: x[1])
            return p[len(p) // 2][0]
        return sorted(parsed, key=lambda x: (abs(x[1] - spot_price), x[0].get("ticker", "")))[0][0]

    def resolve_btc_hourly_trade_target_market(self, now: datetime | None = None, spot_price: float | None = None, markets: list[dict] | None = None) -> dict | None:
        now = now or datetime.now(timezone.utc)
        rows = markets if markets is not None else self.get_live_candidate_trade_markets(now=now)
        if not rows:
            return None
        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        grouped: dict[datetime, list[dict]] = {}
        for m in rows:
            try:
                close_dt = datetime.fromisoformat(m.get("close_time", "").replace("Z", "+00:00"))
            except Exception:
                continue
            grouped.setdefault(close_dt, []).append(m)
        if not grouped:
            return None
        eligible = [dt for dt in grouped if dt >= next_hour]
        chosen_close = min(eligible) if eligible else min([dt for dt in grouped if dt > now], default=min(grouped))
        return self.select_best_threshold_contract(grouped[chosen_close], spot_price)

    def get_hourly_series_quote_rows(self, markets: list[dict]) -> list[dict]:
        out = []
        for m in markets:
            best = {"yes_bid": None, "yes_ask": None, "no_bid": None, "no_ask": None}
            err = None
            try:
                best = self.extract_best_prices(self.get_orderbook_snapshot(m.get("ticker", "")))
            except Exception as exc:
                err = str(exc)
            out.append(
                {
                    "ticker": m.get("ticker", ""),
                    "title": m.get("title", ""),
                    "close_time": m.get("close_time", ""),
                    "strike": parse_btc_threshold(m),
                    "status": m.get("status", ""),
                    "event_ticker": m.get("event_ticker", ""),
                    **best,
                    "last_update": datetime.now(timezone.utc).isoformat(),
                    "error": err,
                }
            )
        return out


def parse_btc_threshold(market: dict) -> Optional[float]:
    text = " ".join([market.get("title", ""), market.get("subtitle", ""), market.get("ticker", "")])
    m = re.search(r"(?:above|over|below|under|>=|<=|>|<|at)\s*\$?([0-9]{2,}(?:,[0-9]{3})*(?:\.[0-9]+)?)", text, re.I)
    return float(m.group(1).replace(",", "")) if m else None
