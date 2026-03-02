from dataclasses import dataclass

from app.strategy.regime import classify_regime


@dataclass
class FairValueResult:
    fair_yes_cents: float
    fair_no_cents: float
    yes_edge_cents: float
    no_edge_cents: float
    selected_side: str
    reasons: list[str]


def compute_fair_value(spot: float, strike: float, yes_ask: float, no_ask: float, prices: list[float], seconds_to_expiry: int) -> FairValueResult:
    regime = classify_regime(prices)
    momentum = (prices[-1] - prices[-10]) if len(prices) >= 10 else 0
    vol = (max(prices[-20:]) - min(prices[-20:])) / max(spot, 1) if len(prices) >= 20 else 0.005
    distance = (spot - strike) / max(strike, 1)

    base = 50 + distance * 700 + momentum * 0.02
    if regime == "trend_up":
        base += 6
    elif regime == "trend_down":
        base -= 6
    if seconds_to_expiry < 20 * 60:
        base += distance * 120
    base -= vol * 120

    fair_yes = max(1.0, min(99.0, base))
    fair_no = 100.0 - fair_yes
    yes_edge = fair_yes - yes_ask
    no_edge = fair_no - no_ask
    side = "none"
    if yes_edge > 0 and yes_edge >= no_edge:
        side = "buy_yes"
    elif no_edge > 0:
        side = "buy_no"

    reasons = [f"regime={regime}", f"distance={distance:.4f}", f"vol={vol:.4f}"]
    return FairValueResult(fair_yes, fair_no, yes_edge, no_edge, side, reasons)
