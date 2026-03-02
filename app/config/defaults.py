from dataclasses import dataclass, field


@dataclass
class GlobalSettings:
    strategy_mode: str = "1h"
    btc_1h_series_ticker: str = "KXBTC1H"
    spread_filter_cents: int = 3
    min_price_cents: int = 15
    max_price_cents: int = 85
    min_edge_after_friction_cents: float = 7.0
    late_entry_min_edge_cents: float = 10.0
    preferred_entry_start_seconds: int = 35 * 60
    preferred_entry_end_seconds: int = 15 * 60
    no_entry_before_expiry_seconds: int = 600
    rollover_before_expiry_seconds: int = 300
    max_position_size: int = 1
    max_notional_exposure: float = 100.0
    daily_max_loss: float = 100.0
    session_max_loss: float = 50.0
    cooldown_after_loss_seconds: int = 60
    entry_order_timeout_seconds: int = 20
    max_entry_attempts: int = 2
    max_requote_drift_cents: int = 2
    invalidate_on_regime_flip: bool = True
    invalidation_grace_seconds: int = 30
    catastrophe_stop_cents: int = 12
    operational_fail_safe_poll_limit: int = 3
    hold_to_resolution_enabled: bool = True
    allow_marketable_limit_on_stop: bool = False


@dataclass
class Settings:
    global_settings: GlobalSettings = field(default_factory=GlobalSettings)


settings = Settings()
