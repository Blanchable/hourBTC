from datetime import datetime, timezone

import pytest

try:
    from PySide6.QtCore import QObject
except Exception as exc:  # pragma: no cover
    pytest.skip(f"PySide6 unavailable: {exc}", allow_module_level=True)

from app.brokers.kalshi_client import KalshiRateLimitError
from app.config.defaults import settings
from app.config.runtime_settings import from_defaults
from app.core.controller import Controller
from app.feeds.btc_feed import BTCFeed
from app.runtime.live_worker import LiveWorker


class FakeDB:
    pass


class Spot:
    def fetch_btc_spot(self):
        return 100000.0


class Client:
    def __init__(self):
        self.calls = []
        self.raise_429 = False
        self.last_series_diagnostics = {}

    def get_live_candidate_trade_markets(self, now=None, force_refresh=False, fallback_window_hours=None, cache_ttl_seconds=None):
        self.calls.append(("live_candidates", fallback_window_hours, cache_ttl_seconds))
        if self.raise_429:
            raise KalshiRateLimitError("429")
        self.last_series_diagnostics = {
            "open_query_count": 0,
            "fallback_query_count": 2,
            "candidate_count": 1,
            "used_fallback": True,
        }
        return [{"ticker": "KXBTCD-A", "title": "Bitcoin price today at 1:00 AM?", "close_time": "2099-01-01T01:00:00Z"}]

    def resolve_btc_hourly_trade_target_market(self, now=None, spot_price=None, markets=None):
        return markets[0] if markets else None

    def get_hourly_series_quote_rows(self, markets):
        return [{"ticker": "KXBTCD-A", "title": markets[0]["title"], "close_time": markets[0]["close_time"], "yes_bid": 45, "yes_ask": 46, "no_bid": 54, "no_ask": 55}]

    def get_account_summary(self):
        return {"balance": 10000}

    def place_entry_order(self, **kwargs):
        self.submitted = kwargs
        return {"order": {"order_id": "1", "status": "resting"}}


class Ctrl(Controller):
    def __init__(self, client, feed):
        super().__init__(client, feed)

    def evaluate(self, quote, seconds_to_expiry):
        return "buy_yes"


def _build():
    c = Client()
    feed = BTCFeed()
    feed.push(100000.0)
    worker = LiveWorker(c, Ctrl(c, feed), 10000, FakeDB(), spot_client=Spot(), runtime_settings=from_defaults(settings.global_settings))
    return worker, c


def test_worker_refresh_uses_candidate_path_and_runtime_settings():
    w, c = _build()
    w.runtime_settings.fallback_window_hours = 3
    w.runtime_settings.market_list_refresh_seconds = 77
    w._refresh_market_snapshot()
    assert c.calls[0] == ("live_candidates", 3, 77)


def test_worker_logs_open_empty_fallback_and_selected_target():
    w, _ = _build()
    logs = []
    w.log_signal.connect(logs.append)
    w._refresh_market_snapshot()
    assert any("open query returned 0 rows; trying bounded fallback" in m for m in logs)
    assert any("fallback window returned 2 rows" in m for m in logs)
    assert any("Selected threshold target: KXBTCD-A" in m for m in logs)


def test_worker_logs_no_candidates_without_crash():
    w, c = _build()
    logs = []
    w.log_signal.connect(logs.append)

    def none_rows(**kwargs):
        c.last_series_diagnostics = {"open_query_count": 0, "fallback_query_count": 0, "candidate_count": 0}
        return []

    c.get_live_candidate_trade_markets = none_rows
    w._refresh_market_snapshot()
    assert any("No live KXBTCD trade candidates found" in m for m in logs)


def test_worker_handles_429_with_backoff():
    w, _ = _build()
    logs = []
    w.log_signal.connect(logs.append)
    w._begin_backoff("429")
    assert any("Backing off" in m for m in logs)


def test_worker_dry_run_no_submit():
    w, c = _build()
    w.runtime_settings.dry_run_mode = True
    w.last_rows = [{"ticker": "KXBTCD-A", "yes_bid": 45, "yes_ask": 46, "no_bid": 54, "no_ask": 55, "close_time": "2099-01-01T01:00:00Z"}]
    w.last_target = {"ticker": "KXBTCD-A"}
    w.last_spot_ts = w.last_quote_ts = datetime.now(timezone.utc)
    logs = []
    w.log_signal.connect(logs.append)
    w._evaluate_target_once()
    assert any("DRY RUN" in m for m in logs)
    assert not hasattr(c, "submitted")


def test_apply_settings_to_running_changes_values():
    w, _ = _build()
    rs = from_defaults(settings.global_settings)
    rs.market_list_refresh_seconds = 99
    w.apply_runtime_settings(rs)
    assert w.runtime_settings.market_list_refresh_seconds == 99
