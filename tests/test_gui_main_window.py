import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from app.gui import main_window


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_main_window_has_required_widgets(qapp):
    w = main_window.MainWindow()
    assert w.connection_status_label.text() == "Not connected"
    assert w.cash_balance_label.text() == "Cash Available: --"
    assert w.save_button is not None
    assert w.start_button is not None
    assert w.stop_button is not None


def test_save_credentials_calls_store(qapp, monkeypatch):
    calls = []

    class FakeStore:
        def load(self, _env):
            return type("P", (), {"api_key_id": "", "private_key_path": ""})()

        def save(self, env, profile):
            calls.append((env, profile.api_key_id, profile.private_key_path))

    monkeypatch.setattr(main_window, "SecretStore", lambda _path: FakeStore())
    w = main_window.MainWindow()
    w.env.setText("paper")
    w.api_key.setText("abc")
    w.key_file.setText("C:/tmp/test.key")
    w.save_credentials()
    assert calls == [("paper", "abc", "C:/tmp/test.key")]


def test_balance_helpers(qapp):
    w = main_window.MainWindow()
    assert w._extract_available_balance_cents({"balance": 12345}) == 12345
    assert w._extract_available_balance_cents({"balance": "12345"}) == 12345
    assert w._extract_available_balance_cents({"x": 1}) is None
    assert w._format_cents_as_dollars(12345) == "$123.45"


def test_start_bot_success(qapp, monkeypatch, tmp_path):
    key = tmp_path / "x.key"
    key.write_text("dummy")

    class FakeStore:
        def load(self, _env):
            return type("P", (), {"api_key_id": "", "private_key_path": ""})()

        def save(self, env, profile):
            self.saved = (env, profile.api_key_id, profile.private_key_path)

    class FakeClient:
        def __init__(self, env, api_key_id, key_file):
            self.env = env

        def get_account_summary(self):
            return {"balance": 12345}

        def connect(self):
            return {"balance": 12345}

        def resolve_btc_target_market(self):
            return {"ticker": "KXBTC1H-TEST", "title": "BTC above 100000", "close_time": "2026-01-01T10:00:00Z"}

    monkeypatch.setattr(main_window, "SecretStore", lambda _path: FakeStore())
    monkeypatch.setattr(main_window, "KalshiClient", FakeClient)

    w = main_window.MainWindow()
    w.env.setText("paper")
    w.api_key.setText("key")
    w.key_file.setText(str(key))
    w.start_bot()

    assert w.connection_status_label.text() == "Connected!"
    assert w.cash_balance_label.text() == "Cash Available: $123.45"
    assert "Connected!" in w.logs.toPlainText()


def test_start_bot_failure(qapp, monkeypatch, tmp_path):
    key = tmp_path / "x.key"
    key.write_text("dummy")

    class FakeStore:
        def load(self, _env):
            return type("P", (), {"api_key_id": "", "private_key_path": ""})()

        def save(self, env, profile):
            pass

    class BadClient:
        def __init__(self, env, api_key_id, key_file):
            pass

        def get_account_summary(self):
            raise RuntimeError("auth failed")

    monkeypatch.setattr(main_window, "SecretStore", lambda _path: FakeStore())
    monkeypatch.setattr(main_window, "KalshiClient", BadClient)

    w = main_window.MainWindow()
    w.env.setText("paper")
    w.api_key.setText("key")
    w.key_file.setText(str(key))
    w.start_bot()

    assert w.connection_status_label.text() == "Not connected"
    assert "Connection failed" in w.logs.toPlainText()


def test_stop_bot_updates_state(qapp, monkeypatch):
    class FakeStore:
        def load(self, _env):
            return type("P", (), {"api_key_id": "", "private_key_path": ""})()

        def save(self, env, profile):
            pass

    monkeypatch.setattr(main_window, "SecretStore", lambda _path: FakeStore())
    w = main_window.MainWindow()
    w.set_connected_state("$1.00")
    w.stop_bot()
    assert w.connection_status_label.text() == "Not connected"
    assert "Bot stopped" in w.logs.toPlainText()
