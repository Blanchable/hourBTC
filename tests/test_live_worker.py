import pytest

try:
    from PySide6.QtCore import QObject
except Exception as exc:  # pragma: no cover
    pytest.skip(f"PySide6 unavailable: {exc}", allow_module_level=True)

from app.core.controller import Controller
from app.feeds.btc_feed import BTCFeed
from app.runtime.live_worker import LiveWorker


class FakeDB:
    def __init__(self):
        self.opened = []
        self.closed = []

    def log_event(self, *args, **kwargs):
        pass

    def insert_trade_open(self, **kwargs):
        self.opened.append(kwargs)
        return 1

    def close_trade(self, **kwargs):
        self.closed.append(kwargs)


class FakeSpot:
    def fetch_btc_spot(self):
        return 100000.0


class FakeClient:
    def __init__(self):
        self._balance = 10075
        self.last_series_diagnostics = {"open_count": 1, "fallback_count": 0, "tradable_count": 1}
        self.status = {"order": {"status": "resting", "fill_count": 0, "remaining_count": 1}}
        self.canceled = []
        self.entry_calls = []
        self.exit_calls = []

    def validate_hourly_series(self):
        return {"ok": True, "ticker": "KXBTC"}

    def get_hourly_series_quote_rows(self):
        return [
            {
                "ticker": "KXBTC-TEST-1",
                "title": "BTC above 100k",
                "close_time": "2099-01-01T10:00:00Z",
                "strike": 100000,
                "yes_bid": 45,
                "yes_ask": 46,
                "no_bid": 54,
                "no_ask": 55,
                "last_update": "now",
            }
        ]

    def resolve_btc_target_market(self):
        return {"ticker": "KXBTC-TEST-1"}

    def place_entry_order(self, **kwargs):
        self.entry_calls.append(kwargs)
        return {"order": {"order_id": "ord-entry", "status": "resting", "fill_count": 0, "remaining_count": kwargs["count"]}}

    def place_exit_order(self, **kwargs):
        self.exit_calls.append(kwargs)
        return {"order": {"order_id": "ord-exit", "status": "pending", "fill_count": 0, "remaining_count": kwargs["count"]}}

    def get_order_status(self, order_id):
        return self.status

    def cancel_order(self, order_id):
        self.canceled.append(order_id)
        return {"ok": True}

    def get_account_summary(self):
        return {"balance": self._balance}

    def connect(self):
        return {"balance": self._balance}


class FakeController(Controller):
    def __init__(self, client, feed, action="no_trade"):
        super().__init__(client, feed)
        self._action = action

    def evaluate(self, quote, seconds_to_expiry):
        return self._action


def _build_worker(action="no_trade"):
    client = FakeClient()
    feed = BTCFeed()
    feed.push(100000.0)
    controller = FakeController(client, feed, action=action)
    db = FakeDB()
    worker = LiveWorker(client, controller, 10000, db, spot_client=FakeSpot())
    return worker, client, db


def test_worker_logs_validation_and_series_refresh():
    worker, client, _ = _build_worker()
    logs = []
    worker.log_signal.connect(logs.append)

    worker.start = lambda: None
    worker.log_signal.emit("Hourly series validated: KXBTC")
    worker._last_series = 0
    worker._last_balance = 0
    worker._last_spot = 0
    worker._last_eval = 0
    worker.run_cycle()

    assert any("Series quote refresh complete" in m for m in logs)
    assert any("Hourly series validated: KXBTC" in m for m in logs)


def test_resting_entry_timeout_cancels_and_no_position():
    worker, client, _ = _build_worker(action="buy_yes")
    logs = []
    worker.log_signal.connect(logs.append)

    rows = client.get_hourly_series_quote_rows()
    rows[0]["selected"] = "Yes"
    worker._evaluate_and_trade(rows)
    assert worker.pending_entry_order is not None

    worker.pending_entry_order.submitted_ts = "2000-01-01T00:00:00+00:00"
    client.status = {"order": {"status": "resting", "fill_count": 0, "remaining_count": 1}}
    worker._monitor_pending_entry(rows)

    assert worker.pending_entry_order is None
    assert worker.active_position is None
    assert "ord-entry" in client.canceled
    assert any("timed out" in m for m in logs)


def test_filled_entry_creates_active_position_and_db_open():
    worker, client, db = _build_worker(action="buy_yes")
    rows = client.get_hourly_series_quote_rows()
    rows[0]["selected"] = "Yes"
    worker._evaluate_and_trade(rows)

    client.status = {"order": {"status": "executed", "fill_count": 1, "remaining_count": 0}}
    worker._monitor_pending_entry(rows)

    assert worker.active_position is not None
    assert worker.active_position.qty == 1
    assert db.opened


def test_exit_submit_and_finalize_only_after_confirmation():
    worker, client, db = _build_worker(action="no_trade")
    rows = client.get_hourly_series_quote_rows()
    worker.active_position = worker.active_position = type("AP", (), {
        "ticker": "KXBTC-TEST-1",
        "title": "BTC above 100k",
        "side": "buy_yes",
        "qty": 1,
        "entry_price": 46,
        "entry_ts": "2026-01-01T00:00:00+00:00",
        "order_id": "ord-entry",
        "status": "open",
        "exit_rule": "invalidation",
        "current_exit_ref": 0.0,
        "unrealized_pnl": 0.0,
        "close_time": "2099-01-01T10:00:00Z",
        "trade_id": 1,
    })()

    worker._submit_exit_for_active_position("regime_flip", rows[0])
    assert worker.pending_exit_order is not None
    assert worker.active_position is not None
    assert client.exit_calls

    client.status = {"order": {"status": "executed", "fill_count": 1, "remaining_count": 0}}
    worker._monitor_pending_exit(rows)
    assert worker.pending_exit_order is None
    assert worker.active_position is None
    assert db.closed


def test_session_guardrails_block_entries():
    worker, client, _ = _build_worker(action="buy_yes")
    rows = client.get_hourly_series_quote_rows()
    rows[0]["selected"] = "Yes"

    worker.completed_trades = worker.cfg.max_trades_per_session
    worker._evaluate_and_trade(rows)
    assert not client.entry_calls

    worker.completed_trades = 0
    worker.consecutive_losses = worker.cfg.max_consecutive_losses
    worker._evaluate_and_trade(rows)
    assert not client.entry_calls

    worker.consecutive_losses = 0
    worker.current_balance_cents = worker.start_balance_cents - worker.cfg.session_stop_loss_cents
    worker._evaluate_and_trade(rows)
    assert not client.entry_calls


def test_shutdown_cleanup_cancels_resting_entry_before_client_close():
    worker, client, _ = _build_worker(action="buy_yes")
    rows = client.get_hourly_series_quote_rows()
    rows[0]["selected"] = "Yes"
    worker._evaluate_and_trade(rows)
    assert worker.pending_entry_order is not None

    worker.shutdown_cleanup()
    assert "ord-entry" in client.canceled
