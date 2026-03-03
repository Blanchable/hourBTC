from app.config.defaults import settings
from app.config.runtime_settings import RuntimeSettingsStore, from_defaults


def test_runtime_settings_save_load(tmp_path):
    store = RuntimeSettingsStore(tmp_path / "runtime_settings.json")
    defaults = from_defaults(settings.global_settings)
    loaded = store.load(defaults)
    assert loaded.market_list_refresh_seconds == defaults.market_list_refresh_seconds

    loaded.market_list_refresh_seconds = 99
    store.save(loaded)
    loaded2 = store.load(defaults)
    assert loaded2.market_list_refresh_seconds == 99
