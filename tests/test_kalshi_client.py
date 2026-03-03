from datetime import datetime, timedelta, timezone

from app.brokers.kalshi_client import (
    BTC_HOURLY_RANGE_SERIES_TICKER,
    BTC_HOURLY_TRADE_SERIES_TICKER,
    KalshiClient,
    parse_btc_threshold,
)


class FakeClient(KalshiClient):
    def __init__(self):
        self.last_series_diagnostics = {}


def test_series_constants():
    assert BTC_HOURLY_TRADE_SERIES_TICKER == "KXBTCD"
    assert BTC_HOURLY_RANGE_SERIES_TICKER == "KXBTC"


def test_title_family_filters_and_threshold_parse():
    c = FakeClient()
    t = {"title": "Bitcoin price today at 1:00 AM above $98,500?"}
    r = {"title": "Bitcoin price range 1:00 AM"}
    assert c.is_threshold_hourly_btc_market(t)
    assert not c.is_threshold_hourly_btc_market(r)
    assert c.is_range_hourly_btc_market(r)
    assert parse_btc_threshold(t) == 98500


def test_select_best_threshold_contract_nearest_spot():
    c = FakeClient()
    m1 = {"ticker": "A", "title": "Bitcoin price today at 1:00 AM above $100000?"}
    m2 = {"ticker": "B", "title": "Bitcoin price today at 1:00 AM above $101000?"}
    chosen = c.select_best_threshold_contract([m1, m2], 100800)
    assert chosen["ticker"] == "B"


def test_extract_best_prices_bid_only_payload():
    payload = {"orderbook": {"yes": [[42, 10], [40, 5]], "no": [[55, 8], [50, 2]]}}
    out = KalshiClient.extract_best_prices(payload)
    assert out == {"yes_bid": 42, "yes_ask": 45, "no_bid": 55, "no_ask": 58}


def test_trade_market_fetch_filters_range_titles():
    class RC(FakeClient):
        def _get_all_markets_for_series(self, series_ticker, status=None):
            now = datetime.now(timezone.utc)
            future = (now + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
            return [
                {"ticker": "T1", "title": "Bitcoin price today at 1:00 AM above $100000?", "status": "open", "close_time": future},
                {"ticker": "R1", "title": "Bitcoin price range 1:00 AM", "status": "open", "close_time": future},
            ]

    c = RC()
    rows = c.get_open_hourly_trade_markets()
    assert [r["ticker"] for r in rows] == ["T1"]
