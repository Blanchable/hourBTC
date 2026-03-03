from app.config.defaults import settings
from app.config.runtime_settings import RuntimeSettingsStore, from_defaults


def test_runtime_settings_save_load(tmp_path):
    store = RuntimeSettingsStore(tmp_path / "runtime_settings.json")
    defaults = from_defaults(settings.global_settings)
    loaded = store.load(defaults)
    assert loaded.market_list_refresh_seconds == defaults.market_list_refresh_seconds

    loaded.market_list_refresh_seconds = 99
    loaded.fallback_window_hours = 4
    store.save(loaded)
    loaded2 = store.load(defaults)
    assert loaded2.market_list_refresh_seconds == 99
    assert loaded2.fallback_window_hours == 4


def test_runtime_settings_malformed_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "runtime_settings.json"
    path.write_text("{bad json", encoding="utf-8")
    store = RuntimeSettingsStore(path)
    defaults = from_defaults(settings.global_settings)
    loaded = store.load(defaults)
    assert loaded.fallback_window_hours == defaults.fallback_window_hours
