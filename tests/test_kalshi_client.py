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
