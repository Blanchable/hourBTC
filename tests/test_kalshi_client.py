from app.brokers.kalshi_client import KalshiClient, parse_btc_threshold


class FakeClient(KalshiClient):
    def __init__(self):
        pass

    def list_series_markets(self):
        return {"markets": []}

    def list_open_markets(self):
        return {
            "markets": [
                {"title": "BTC above 100000", "ticker": "X", "close_time": "2026-01-01T10:00:00Z"},
                {"title": "ETH above 1000", "ticker": "Y", "close_time": "2026-01-01T10:30:00Z"},
            ]
        }


def test_parse_threshold():
    assert parse_btc_threshold({"title": "Will BTC close above $98,500?"}) == 98500
    assert parse_btc_threshold({"title": "No number here"}) is None


def test_fallback_market_resolution():
    c = FakeClient()
    m = c.resolve_btc_target_market()
    assert m["ticker"] == "X"


def test_extract_best_prices_happy_path():
    orderbook = {
        "yes": {"bids": [{"price": 45}], "asks": [{"price": 47}]},
        "no": {"bids": [{"price": 53}], "asks": [{"price": 55}]},
    }
    out = KalshiClient.extract_best_prices(orderbook)
    assert out == {"yes_bid": 45.0, "yes_ask": 47.0, "no_bid": 53.0, "no_ask": 55.0}


def test_extract_best_prices_missing_or_malformed():
    out = KalshiClient.extract_best_prices({"yes": {"bids": []}, "no": {"asks": []}})
    assert out["yes_bid"] is None
    assert out["yes_ask"] is None
    assert out["no_bid"] is None
    assert out["no_ask"] is None

    out2 = KalshiClient.extract_best_prices({"bad": "shape"})
    assert out2 == {"yes_bid": None, "yes_ask": None, "no_bid": None, "no_ask": None}
