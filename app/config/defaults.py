from dataclasses import dataclass, field


@dataclass
class GlobalSettings:
    strategy_mode: str = "1h"
    btc_hourly_trade_series_ticker: str = "KXBTCD"
    btc_hourly_range_series_ticker: str = "KXBTC"
    spread_filter_cents: int = 3
    min_price_cents: int = 15
    max_price_cents: int = 85
    min_edge_after_friction_cents: float = 7.0
    late_entry_min_edge_cents: float = 10.0
    preferred_entry_start_seconds: int = 35 * 60
    preferred_entry_end_seconds: int = 15 * 60
    no_entry_before_expiry_seconds: int = 600
    rollover_before_expiry_seconds: int = 300

    # Position sizing / exposure
    max_position_contracts: int = 1
    max_position_notional_cents: int = 500
    max_total_open_notional_cents: int = 1000

    # Session guardrails
    session_stop_loss_cents: int = 800
    session_take_profit_cents: int = 1200
    max_trades_per_session: int = 3
    max_consecutive_losses: int = 2

    # Order behavior
    entry_post_only: bool = True
    dry_run_mode: bool = True
    entry_time_in_force: str = "good_till_canceled"
    entry_order_timeout_seconds: int = 15
    exit_time_in_force: str = "immediate_or_cancel"
    emergency_exit_time_in_force: str = "immediate_or_cancel"
    reduce_only_exits: bool = True
    cancel_order_on_pause: bool = True

    # Safety / fail-safe controls
    max_order_rejections_per_session: int = 2
    max_api_errors_per_session: int = 3
    max_consecutive_loop_errors: int = 3
    quote_stale_stop_seconds: int = 10
    market_list_refresh_seconds: int = 45
    rate_limit_backoff_seconds: int = 10
    rate_limit_backoff_max_seconds: int = 30
    fallback_window_hours: int = 2
    disable_new_entries_after_stop_hit: bool = True

    # Operational controls
    session_runtime_limit_minutes: int = 180
    cooldown_after_loss_seconds: int = 300

    # Existing strategy/risk values retained
    max_position_size: int = 1
    max_notional_exposure: float = 100.0
    daily_max_loss: float = 100.0
    session_max_loss: float = 50.0
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
