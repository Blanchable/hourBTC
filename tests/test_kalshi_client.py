from datetime import datetime, timedelta, timezone

from app.brokers.kalshi_client import BTC_1H_SERIES_TICKER, KalshiClient, parse_btc_threshold


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
