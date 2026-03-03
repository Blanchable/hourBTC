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

    def insert_trade_open(self, **kwargs):
        return 1

    def close_trade(self, **kwargs):
        pass


class FakeSpot:
    def fetch_btc_spot(self):
        return 100000.0


class FakeClient:
    def __init__(self):
        self._balance = 10075
        self.last_series_diagnostics = {"open_count": 1, "fallback_count": 0, "tradable_count": 1}

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

    def place_limit_order(self, ticker, side, count, limit_price, client_order_id):
        return {"order": {"order_id": "ord-1"}}

    def get_order_status(self, order_id):
        return {"order": {"status": "filled"}}

    def get_account_summary(self):
        return {"balance": self._balance}

    def connect(self):
        return {"balance": self._balance}


class FakeController(Controller):
    def __init__(self, client, feed):
        super().__init__(client, feed)

    def evaluate(self, quote, seconds_to_expiry):
        return "no_trade"


def test_worker_emits_core_updates_and_validation_log():
    client = FakeClient()
    feed = BTCFeed()
    controller = FakeController(client, feed)
    worker = LiveWorker(client, controller, 10000, FakeDB(), spot_client=FakeSpot())

    rows = []
    balances = []
    pnls = []
    logs = []
    worker.series_rows_signal.connect(lambda r: rows.extend(r))
    worker.cash_balance_signal.connect(lambda c: balances.append(c))
    worker.session_pnl_signal.connect(lambda c: pnls.append(c))
    worker.log_signal.connect(lambda m: logs.append(m))

    worker.start = lambda: None
    validation = client.validate_hourly_series()
    if validation["ok"]:
        worker.log_signal.emit(f"Hourly series validated: {validation['ticker']}")

    worker._last_series = 0
    worker._last_balance = 0
    worker._last_spot = 0
    worker._last_eval = 0
    worker.run_cycle()

    assert rows
    assert balances
    assert pnls[-1] == 75
    assert any("Series quote refresh complete" in m for m in logs)
    assert any("Hourly series validated: KXBTC" in m for m in logs)


def test_worker_logs_no_open_series_rows():
    class EmptyClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.last_series_diagnostics = {"open_count": 0, "fallback_count": 8, "tradable_count": 0}

        def get_hourly_series_quote_rows(self):
            return []

    client = EmptyClient()
    worker = LiveWorker(client, FakeController(client, BTCFeed()), 10000, FakeDB(), spot_client=FakeSpot())
    logs = []
    worker.log_signal.connect(lambda m: logs.append(m))

    rows = worker._refresh_series_quotes()
    assert rows == []
    assert any("No open KXBTC contracts found" in m for m in logs)
