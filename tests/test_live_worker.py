import pytest

try:
    from PySide6.QtCore import QObject
except Exception as exc:  # pragma: no cover
    pytest.skip(f"PySide6 unavailable: {exc}", allow_module_level=True)

from app.core.controller import Controller
from app.feeds.btc_feed import BTCFeed
from app.runtime.live_worker import LiveWorker


class FakeDB:
    def log_event(self, *args, **kwargs):
        pass


class FakeSpot:
    def fetch_btc_spot(self):
        return 100050.0


class FakeClient:
    def __init__(self):
        self._balance = 10075
        self.last_series_diagnostics = {"open_count": 1, "fallback_count": 0, "tradable_count": 1}
        self.entry_calls = []
        self.exit_calls = []
        self.status = {"order": {"status": "filled", "fill_count": 1}}

    def validate_hourly_series(self):
        return {"ok": True, "ticker": "KXBTCD"}

    def get_open_hourly_trade_markets(self):
        return [{"ticker": "KXBTCD-TEST-1", "title": "Bitcoin price today at 1:00 AM above $100000?", "close_time": "2099-01-01T01:00:00Z"}]

    def get_hourly_series_quote_rows(self, markets=None):
        return [{"ticker": "KXBTCD-TEST-1", "title": "Bitcoin price today at 1:00 AM above $100000?", "close_time": "2099-01-01T01:00:00Z", "strike": 100000, "yes_bid": 45, "yes_ask": 46, "no_bid": 54, "no_ask": 55, "last_update": "now"}]

    def resolve_btc_hourly_trade_target_market(self, now=None, spot_price=None):
        return {"ticker": "KXBTCD-TEST-1", "title": "Bitcoin price today at 1:00 AM above $100000?", "close_time": "2099-01-01T01:00:00Z", "strike": 100000}

    def place_entry_order(self, **kwargs):
        self.entry_calls.append(kwargs)
        return {"order": {"order_id": "ord-entry", "status": "resting"}}

    def place_exit_order(self, **kwargs):
        self.exit_calls.append(kwargs)
        return {"order": {"order_id": "ord-exit", "status": "pending"}}

    def get_order_status(self, order_id):
        return self.status

    def cancel_order(self, order_id):
        return {"ok": True}

    def get_account_summary(self):
        return {"balance": self._balance}


class FakeController(Controller):
    def __init__(self, client, feed, action="no_trade"):
        super().__init__(client, feed)
        self._action = action

    def evaluate(self, quote, seconds_to_expiry):
        return self._action


def _build(action="no_trade"):
    c = FakeClient()
    feed = BTCFeed()
    feed.push(100000.0)
    return LiveWorker(c, FakeController(c, feed, action=action), 10000, FakeDB(), spot_client=FakeSpot()), c


def test_trade_series_target_is_threshold_family():
    worker, _ = _build()
    rows, target = worker._refresh_series_quotes()
    assert target["ticker"].startswith("KXBTCD")
    assert "Bitcoin price today at" in target["title"]


def test_dry_run_builds_payload_without_submit():
    worker, client = _build(action="buy_yes")
    worker.cfg.dry_run_mode = True
    rows, target = worker._refresh_series_quotes()
    logs = []
    worker.log_signal.connect(logs.append)
    worker.last_spot_update_ts = worker.last_quote_update_ts = __import__('datetime').datetime.now(__import__('datetime').timezone.utc)
    worker._evaluate_and_trade(rows, target)
    assert not client.entry_calls
    assert any("DRY RUN" in m for m in logs)


def test_stale_feed_logs_detailed_reason():
    worker, _ = _build(action="buy_yes")
    rows, target = worker._refresh_series_quotes()
    logs = []
    worker.log_signal.connect(logs.append)
    worker.last_spot_update_ts = None
    worker.last_quote_update_ts = None
    worker._evaluate_and_trade(rows, target)
    assert any("spot_age=" in m and "quote_age=" in m for m in logs)


def test_shadow_like_path_builds_target_and_quote():
    worker, _ = _build()
    rows, target = worker._refresh_series_quotes()
    assert rows
    assert target["ticker"] == "KXBTCD-TEST-1"
