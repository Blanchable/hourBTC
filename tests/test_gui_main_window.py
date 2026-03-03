import pytest

try:
    from PySide6.QtWidgets import QApplication
except Exception as exc:  # pragma: no cover
    pytest.skip(f"PySide6 unavailable: {exc}", allow_module_level=True)

from app.gui import main_window


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _build_window(monkeypatch):
    class FakeStore:
        def load(self, _env):
            return type("P", (), {"api_key_id": "", "private_key_path": ""})()

        def save(self, env, profile):
            pass

    class FakeDB:
        def bootstrap(self):
            pass

        def log_event(self, *args, **kwargs):
            pass

    monkeypatch.setattr(main_window, "SecretStore", lambda _path: FakeStore())
    monkeypatch.setattr(main_window, "DB", lambda _path: FakeDB())
    return main_window.MainWindow()


def test_gui_has_tabs_and_labels(qapp, monkeypatch):
    w = _build_window(monkeypatch)
    tab_names = [w.tabs.tabText(i) for i in range(w.tabs.count())]
    assert tab_names == ["Logs", "Series Order Book", "Active Positions", "Session History"]
    assert w.connection_status_label.text() == "Not connected"
    assert w.cash_balance_label.text().startswith("Cash Available")
    assert w.session_pnl_label.text().startswith("Session PnL")
    assert w.selected_market_label.text().startswith("Selected Market")


def test_series_table_update(qapp, monkeypatch):
    w = _build_window(monkeypatch)
    w.update_series_orderbook(
        [
            {
                "ticker": "KXBTC1H-1",
                "title": "BTC above 100k",
                "close_time": "2026-01-01T10:00:00Z",
                "strike": 100000,
                "yes_bid": 45,
                "yes_ask": 47,
                "no_bid": 53,
                "no_ask": 55,
                "selected": "Yes",
                "last_update": "t",
            }
        ]
    )
    assert w.series_table.rowCount() == 1
    assert w.series_table.item(0, 0).text() == "KXBTC1H-1"
    assert w.series_table.item(0, 4).text() == "45"
    assert w.series_table.item(0, 7).text() == "55"


def test_balance_based_session_pnl(qapp, monkeypatch):
    w = _build_window(monkeypatch)
    w.update_cash_balance(10075)
    w.update_session_pnl(75)
    assert w.cash_balance_label.text() == "Cash Available: $100.75"
    assert w.session_pnl_label.text() == "Session PnL: $0.75"

    w.update_session_pnl(-46)
    assert w.session_pnl_label.text() == "Session PnL: -$0.46"
