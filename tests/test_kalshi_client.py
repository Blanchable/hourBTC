from datetime import datetime, timedelta, timezone

from app.brokers.kalshi_client import BTC_HOURLY_RANGE_SERIES_TICKER, BTC_HOURLY_TRADE_SERIES_TICKER, KalshiClient, KalshiRateLimitError


class FakeClient(KalshiClient):
    def __init__(self):
        self.last_series_diagnostics = {}
        self._market_cache_rows = []
        self._market_cache_ts = 0


def test_series_constants():
    assert BTC_HOURLY_TRADE_SERIES_TICKER == "KXBTCD"
    assert BTC_HOURLY_RANGE_SERIES_TICKER == "KXBTC"


def test_open_empty_uses_bounded_fallback_and_diagnostics():
    now = datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc)
    close = (now + timedelta(minutes=55)).isoformat().replace("+00:00", "Z")

    class C(FakeClient):
        def __init__(self):
            super().__init__()
            self.calls = []

        def _get_all_markets_for_series(self, series_ticker, status=None, close_time_start_ts=None, close_time_end_ts=None):
            self.calls.append((status, close_time_start_ts, close_time_end_ts))
            if status == "open":
                return []
            return [{"ticker": "KXBTCD-T", "series_ticker": "KXBTCD", "title": "Bitcoin price today at 1:00 PM?", "status": "active", "close_time": close}]

    c = C()
    rows = c.get_live_candidate_trade_markets(now=now, force_refresh=True, fallback_window_hours=2)
    assert len(rows) == 1
    assert c.calls[0][0] == "open"
    assert c.calls[1][0] is None
    assert c.calls[1][1] is not None and c.calls[1][2] is not None
    assert c.last_series_diagnostics["used_fallback"] is True
    assert c.last_series_diagnostics["open_query_count"] == 0
    assert c.last_series_diagnostics["candidate_count"] == 1


def test_candidate_filter_keeps_only_live_threshold_rows():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    future = (now + timedelta(minutes=20)).isoformat().replace("+00:00", "Z")
    past = (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    c = FakeClient()
    rows = [
        {"ticker": "A", "series_ticker": "KXBTCD", "title": "Bitcoin price today at 1:00 PM?", "status": "active", "close_time": future},
        {"ticker": "B", "series_ticker": "KXBTCD", "title": "Bitcoin price range 1:00 PM", "status": "active", "close_time": future},
        {"ticker": "C", "series_ticker": "KXBTCD", "title": "Bitcoin price today at 1:00 PM?", "status": "closed", "close_time": future},
        {"ticker": "D", "series_ticker": "KXBTCD", "title": "Bitcoin price today at 11:00 AM?", "status": "active", "close_time": past},
    ]
    out = c.filter_live_trade_candidates(rows, now=now)
    assert [r["ticker"] for r in out] == ["A"]


def test_resolver_uses_next_event_and_nearest_threshold():
    now = datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc)
    next_close = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    later_close = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    c = FakeClient()
    markets = [
        {"ticker": "KXBTCD-LOW", "series_ticker": "KXBTCD", "title": "Bitcoin price today at 1:00 PM? above $99950", "close_time": next_close},
        {"ticker": "KXBTCD-HI", "series_ticker": "KXBTCD", "title": "Bitcoin price today at 1:00 PM? above $100050", "close_time": next_close},
        {"ticker": "KXBTCD-LATER", "series_ticker": "KXBTCD", "title": "Bitcoin price today at 2:00 PM? above $100000", "close_time": later_close},
    ]
    selected = c.resolve_btc_hourly_trade_target_market(now=now, spot_price=100040, markets=markets)
    assert selected["ticker"] == "KXBTCD-HI"


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
