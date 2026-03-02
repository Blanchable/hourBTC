from dataclasses import dataclass

from app.config.defaults import settings
from app.strategy.fair_value import FairValueResult, compute_fair_value
from app.strategy.regime import classify_regime


@dataclass
class Decision:
    action: str
    reason: str
    fair: FairValueResult | None = None


def decide_entry(prices: list[float], spot: float, strike: float | None, quote: dict, seconds_to_expiry: int, feed_stale: bool = False) -> Decision:
    g = settings.global_settings
    if feed_stale:
        return Decision("no_trade", "feed_stale")
    if strike is None:
        return Decision("no_trade", "missing_strike")
    spread = min(quote["yes_ask"] - quote["yes_bid"], quote["no_ask"] - quote["no_bid"])
    if spread > g.spread_filter_cents:
        return Decision("no_trade", "spread_too_wide")
    if seconds_to_expiry <= g.no_entry_before_expiry_seconds:
        return Decision("no_trade", "near_expiry")

    fair = compute_fair_value(spot, strike, quote["yes_ask"], quote["no_ask"], prices, seconds_to_expiry)
    regime = classify_regime(prices)
    min_edge = g.min_edge_after_friction_cents
    if seconds_to_expiry < g.preferred_entry_end_seconds:
        min_edge = g.late_entry_min_edge_cents

    if fair.selected_side == "buy_yes" and regime not in {"trend_up"}:
        return Decision("no_trade", "regime_mismatch_yes", fair)
    if fair.selected_side == "buy_no" and regime not in {"trend_down"}:
        return Decision("no_trade", "regime_mismatch_no", fair)

    edge = max(fair.yes_edge_cents, fair.no_edge_cents)
    if fair.selected_side == "none" or edge < min_edge:
        return Decision("no_trade", "edge_too_small", fair)
    return Decision(fair.selected_side, "qualified", fair)


def should_exit(position_side: str, regime: str, fair: FairValueResult, structure_ok: bool, catastrophe_drawdown: float) -> tuple[bool, str]:
    g = settings.global_settings
    if catastrophe_drawdown >= g.catastrophe_stop_cents:
        return True, "risk_fail_safe"
    if not structure_ok:
        return True, "structure_failure"
    if position_side == "buy_yes" and regime in {"trend_down", "range"} and g.invalidate_on_regime_flip:
        return True, "regime_flip"
    if position_side == "buy_no" and regime in {"trend_up", "range"} and g.invalidate_on_regime_flip:
        return True, "regime_flip"
    if position_side == "buy_yes" and fair.yes_edge_cents < 0:
        return True, "fair_value_collapse"
    if position_side == "buy_no" and fair.no_edge_cents < 0:
        return True, "fair_value_collapse"
    return False, "hold_to_resolution"
