import json
from dataclasses import asdict, dataclass
from pathlib import Path

from app.config.defaults import GlobalSettings


@dataclass
class RuntimeSettings:
    session_stop_loss_cents: int
    session_take_profit_cents: int
    max_position_contracts: int
    max_position_notional_cents: int
    max_total_open_notional_cents: int
    max_trades_per_session: int
    max_consecutive_losses: int
    entry_order_timeout_seconds: int
    cooldown_after_loss_seconds: int
    market_list_refresh_seconds: int
    rate_limit_backoff_seconds: int
    rate_limit_backoff_max_seconds: int
    dry_run_mode: bool
    entry_post_only: bool
    disable_new_entries_after_stop_hit: bool
    quote_stale_stop_seconds: int
    max_api_errors_per_session: int
    max_order_rejections_per_session: int


def from_defaults(d: GlobalSettings) -> RuntimeSettings:
    return RuntimeSettings(
        session_stop_loss_cents=d.session_stop_loss_cents,
        session_take_profit_cents=d.session_take_profit_cents,
        max_position_contracts=d.max_position_contracts,
        max_position_notional_cents=d.max_position_notional_cents,
        max_total_open_notional_cents=d.max_total_open_notional_cents,
        max_trades_per_session=d.max_trades_per_session,
        max_consecutive_losses=d.max_consecutive_losses,
        entry_order_timeout_seconds=d.entry_order_timeout_seconds,
        cooldown_after_loss_seconds=d.cooldown_after_loss_seconds,
        market_list_refresh_seconds=d.market_list_refresh_seconds,
        rate_limit_backoff_seconds=d.rate_limit_backoff_seconds,
        rate_limit_backoff_max_seconds=d.rate_limit_backoff_max_seconds,
        dry_run_mode=d.dry_run_mode,
        entry_post_only=d.entry_post_only,
        disable_new_entries_after_stop_hit=d.disable_new_entries_after_stop_hit,
        quote_stale_stop_seconds=d.quote_stale_stop_seconds,
        max_api_errors_per_session=d.max_api_errors_per_session,
        max_order_rejections_per_session=d.max_order_rejections_per_session,
    )


class RuntimeSettingsStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self, defaults: RuntimeSettings) -> RuntimeSettings:
        if not self.path.exists():
            return defaults
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return defaults
        payload = asdict(defaults)
        payload.update({k: v for k, v in data.items() if k in payload})
        return RuntimeSettings(**payload)

    def save(self, settings: RuntimeSettings) -> None:
        self.path.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
