import pytest

try:
    from PySide6.QtWidgets import QApplication
except Exception as exc:  # pragma: no cover
    pytest.skip(f"PySide6 unavailable: {exc}", allow_module_level=True)

from app.gui import main_window


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


def _build(monkeypatch):
    class Store:
        def load(self, env):
            return type("P", (), {"api_key_id": "", "private_key_path": ""})()

        def save(self, env, profile):
            pass

    class DB:
        def bootstrap(self):
            pass

    class RS:
        def load(self, d):
            return d

        def save(self, s):
            pass

    monkeypatch.setattr(main_window, "SecretStore", lambda p: Store())
    monkeypatch.setattr(main_window, "DB", lambda p: DB())
    monkeypatch.setattr(main_window, "RuntimeSettingsStore", lambda p: RS())
    return main_window.MainWindow()


def test_has_settings_tab_and_buttons(qapp, monkeypatch):
    w = _build(monkeypatch)
    tabs = [w.tabs.tabText(i) for i in range(w.tabs.count())]
    assert "Settings" in tabs
    assert w.btn_save_settings.text() == "Save Settings"
    assert w.btn_apply_settings.text() == "Apply to Running Bot"
    assert w.btn_reset_settings.text() == "Reset to Defaults"
    assert w.s_fallback_window.text() != ""


def test_validation_blocks_bad_settings(qapp, monkeypatch):
    w = _build(monkeypatch)
    w.s_market_refresh.setText("1")
    w.save_runtime_settings()
    assert "Market List Refresh" in w.logs.toPlainText()


def test_validation_blocks_bad_fallback_window(qapp, monkeypatch):
    w = _build(monkeypatch)
    w.s_fallback_window.setText("0")
    w.save_runtime_settings()
    assert "Fallback window" in w.logs.toPlainText()


def test_shadow_while_running_uses_cached_mode(qapp, monkeypatch):
    w = _build(monkeypatch)
    w.loop_running = True

    class W:
        def get_cached_shadow_context(self):
            return {
                "rows": [{"ticker": "KXBTCD-A", "yes_bid": 1, "yes_ask": 2, "no_bid": 98, "no_ask": 99}],
                "target": {"ticker": "KXBTCD-A", "title": "Bitcoin price today at 1:00 AM?"},
                "spot": 100000,
                "spot_age": 1.0,
                "quote_age": 1.0,
            }

    w.worker = W()
    w.shadow_order_test()
    assert "Using cached shadow mode" in w.logs.toPlainText()
