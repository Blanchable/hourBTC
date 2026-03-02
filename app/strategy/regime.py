from statistics import mean


def classify_regime(prices: list[float]) -> str:
    if len(prices) < 20:
        return "unstable"
    short = prices[-1] - prices[-5]
    medium = prices[-1] - prices[-20]
    avg = mean(prices[-20:])
    dispersion = max(prices[-20:]) - min(prices[-20:])
    if dispersion > avg * 0.015:
        return "unstable"
    if short > 0 and medium > 0:
        return "trend_up"
    if short < 0 and medium < 0:
        return "trend_down"
    return "range"
