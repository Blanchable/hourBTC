from datetime import datetime, timedelta, timezone

import pytest

from app.brokers.kalshi_client import BTC_1H_SERIES_TICKER, KalshiClient, KalshiRequestError, parse_btc_threshold


class FakeClient(KalshiClient):
    def __init__(self):
        self.last_series_diagnostics = {}

    def list_open_markets(self):
        return {
            "markets": [
                {"title": "BTC above 100000", "ticker": "X", "close_time": "2099-01-01T10:00:00Z", "status": "open"},
                {"title": "ETH above 1000", "ticker": "Y", "close_time": "2099-01-01T10:30:00Z", "status": "open"},
            ]
        }


def test_hourly_series_constant():
    assert BTC_1H_SERIES_TICKER == "KXBTC"


def test_parse_threshold():
    assert parse_btc_threshold({"title": "Will BTC close above $98,500?"}) == 98500
    assert parse_btc_threshold({"title": "No number here"}) is None


def test_extract_best_prices_bid_only_payload():
    payload = {"orderbook": {"yes": [[42, 10], [40, 5]], "no": [[55, 8], [50, 2]]}}
    out = KalshiClient.extract_best_prices(payload)
    assert out["yes_bid"] == 42
    assert out["no_bid"] == 55
    assert out["yes_ask"] == 45
    assert out["no_ask"] == 58


def test_extract_best_prices_missing_side():
    out = KalshiClient.extract_best_prices({"orderbook": {"yes": [[42, 10]]}})
    assert out["yes_bid"] == 42
    assert out["no_ask"] == 58
    assert out["no_bid"] is None
    assert out["yes_ask"] is None


def test_extract_best_prices_empty_payload():
    out = KalshiClient.extract_best_prices({})
    assert out == {"yes_bid": None, "yes_ask": None, "no_bid": None, "no_ask": None}


def test_fallback_market_resolution_uses_open_market_search():
    c = FakeClient()
    c.get_open_hourly_series_markets = lambda: []
    m = c.resolve_btc_target_market()
    assert m["ticker"] == "X"


def test_get_open_hourly_series_markets_status_fallback_filters():
    class RequestClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.calls = []

        def _request(self, method: str, path: str, body=None):
            self.calls.append(path)
            now = datetime.now(timezone.utc)
            future = (now + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
            past = (now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
            if "status=open" in path:
                return {"markets": [], "cursor": None}
            return {
                "markets": [
                    {"ticker": "A", "close_time": future, "status": "active", "title": "BTC A"},
                    {"ticker": "B", "close_time": past, "status": "open", "title": "BTC B"},
                    {"ticker": "C", "close_time": future, "status": "settled", "title": "BTC C"},
                ],
                "cursor": None,
            }

    c = RequestClient()
    rows = c.get_open_hourly_series_markets()
    assert [r["ticker"] for r in rows] == ["A"]
    assert c.last_series_diagnostics["open_count"] == 0
    assert c.last_series_diagnostics["fallback_count"] == 3
    assert c.last_series_diagnostics["tradable_count"] == 1


def test_place_limit_order_payload_yes_no_fields():
    class ReqClient(FakeClient):
        def _request(self, method: str, path: str, body=None):
            return {"method": method, "path": path, "body": body}

    c = ReqClient()
    yes = c.place_limit_order("T", "yes", "buy", 1, 25, "id1")
    no = c.place_limit_order("T", "no", "sell", 1, 77, "id2")

    assert yes["body"]["action"] == "buy"
    assert yes["body"]["yes_price"] == 25
    assert "no_price" not in yes["body"]

    assert no["body"]["action"] == "sell"
    assert no["body"]["no_price"] == 77
    assert "yes_price" not in no["body"]


def test_request_error_contains_status_and_body():
    class FakeResp:
        status_code = 400
        text = "bad request body"

        def raise_for_status(self):
            raise RuntimeError("boom")

    class FakeHTTP:
        def request(self, *args, **kwargs):
            return FakeResp()

    c = FakeClient()
    c.http = FakeHTTP()
    c._auth_headers = lambda method, path, body="": {}

    with pytest.raises(KalshiRequestError) as exc:
        c._request("GET", "/x")
    msg = str(exc.value)
    assert "status=400" in msg
    assert "bad request body" in msg


def test_place_entry_and_exit_wrappers():
    class ReqClient(FakeClient):
        def _request(self, method: str, path: str, body=None):
            return {"body": body}

    c = ReqClient()
    entry = c.place_entry_order("T", "yes", 1, 22, "e1", post_only=True)
    exit_o = c.place_exit_order("T", "no", 1, 70, "x1")

    assert entry["body"]["action"] == "buy"
    assert exit_o["body"]["action"] == "sell"
    assert exit_o["body"]["reduce_only"] is True
