from app.brokers.kalshi_client import (
    BTC_HOURLY_RANGE_SERIES_TICKER,
    BTC_HOURLY_TRADE_SERIES_TICKER,
    KalshiClient,
    KalshiRateLimitError,
)


class FakeClient(KalshiClient):
    def __init__(self):
        self.last_series_diagnostics = {}
        self._market_cache_rows = []
        self._market_cache_ts = 0


def test_series_constants():
    assert BTC_HOURLY_TRADE_SERIES_TICKER == "KXBTCD"
    assert BTC_HOURLY_RANGE_SERIES_TICKER == "KXBTC"


def test_live_fetch_no_fallback_uses_open_only():
    class C(FakeClient):
        def __init__(self):
            super().__init__()
            self.calls = []

        def _get_all_markets_for_series(self, series_ticker, status=None):
            self.calls.append(status)
            return [{"ticker": "KXBTCD-X", "title": "Bitcoin price today at 1:00 AM?", "status": "open"}]

        def _is_tradable(self, market):
            return True

    c = C()
    c.get_open_hourly_trade_markets_live(force_refresh=True)
    assert c.calls == ["open"]


def test_live_market_cache_reuse():
    class C(FakeClient):
        def __init__(self):
            super().__init__()
            self.n = 0

        def _get_all_markets_for_series(self, series_ticker, status=None):
            self.n += 1
            return [{"ticker": "KXBTCD-X", "title": "Bitcoin price today at 1:00 AM?", "status": "open"}]

        def _is_tradable(self, market):
            return True

    c = C()
    c.get_open_hourly_trade_markets_live(force_refresh=True)
    c.get_open_hourly_trade_markets_live(force_refresh=False)
    assert c.n == 1


def test_request_429_raises_rate_limit_error():
    class Resp:
        status_code = 429
        text = "too many"
        headers = {"Retry-After": "2"}

        def raise_for_status(self):
            raise RuntimeError("x")

    class HTTP:
        def request(self, *args, **kwargs):
            return Resp()

    c = FakeClient()
    c.http = HTTP()
    c._auth_headers = lambda *a, **k: {}
    try:
        c._request("GET", "/x")
        assert False
    except KalshiRateLimitError as e:
        assert "retry_after=2" in str(e)
