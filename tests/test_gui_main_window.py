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

    monkeypatch.setattr(main_window, "SecretStore", lambda _path: FakeStore())
    monkeypatch.setattr(main_window, "DB", lambda _path: FakeDB())
    return main_window.MainWindow()


def test_gui_has_target_labels_and_tabs(qapp, monkeypatch):
    w = _build_window(monkeypatch)
    assert w.trade_series_label.text() == "Trade Series: KXBTCD"
    assert w.display_series_label.text() == "Display Series: KXBTC"
    assert w.target_family_label.text() == "Selected Target Family: Threshold"
    assert w.mode_label.text().startswith("Mode: ")
    tab_names = [w.tabs.tabText(i) for i in range(w.tabs.count())]
    assert "Series Order Book" in tab_names


def test_feed_age_labels_update(qapp, monkeypatch):
    w = _build_window(monkeypatch)
    w.update_spot_meta("2026-01-01T00:00:00+00:00", 3.4)
    assert "2026-01-01" in w.last_spot_update_label.text()
    assert "3.4s" in w.feed_age_label.text()


def test_series_table_shows_quotes(qapp, monkeypatch):
    w = _build_window(monkeypatch)
    w.update_series_orderbook([
        {
            "ticker": "KXBTCD-TEST-1",
            "title": "Bitcoin price today at 1:00 AM?",
            "close_time": "2026-01-01T01:00:00Z",
            "strike": 100000,
            "yes_bid": 45,
            "yes_ask": 47,
            "no_bid": 53,
            "no_ask": 55,
            "selected": "Yes",
            "last_update": "t",
        }
    ])
    assert w.series_table.item(0, 0).text() == "KXBTCD-TEST-1"
    assert w.series_table.item(0, 4).text() == "45"
    assert w.series_table.item(0, 7).text() == "55"


def test_smoke_test_disabled_in_production(qapp, monkeypatch):
    w = _build_window(monkeypatch)
    w.env.setText("production")
    w.paper_smoke_test()
    assert "disabled in production" in w.logs.toPlainText()
