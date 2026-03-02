from app.strategy.engine import decide_entry, should_exit
from app.strategy.regime import classify_regime


def up_prices(n=30):
    return [90000 + i * 10 for i in range(n)]


def down_prices(n=30):
    return [90000 - i * 10 for i in range(n)]


def test_regime_classifier():
    assert classify_regime(up_prices()) == "trend_up"
    assert classify_regime(down_prices()) == "trend_down"


def test_no_trade_blocks():
    quote = {"yes_bid": 40, "yes_ask": 45, "no_bid": 55, "no_ask": 60}
    d = decide_entry(up_prices(), 90500, None, quote, 1800)
    assert d.reason == "missing_strike"
    quote2 = {"yes_bid": 40, "yes_ask": 41, "no_bid": 59, "no_ask": 60}
    d2 = decide_entry(up_prices(), 90500, 90000, quote2, 400)
    assert d2.reason == "near_expiry"


def test_buy_yes_selection():
    quote = {"yes_bid": 40, "yes_ask": 41, "no_bid": 59, "no_ask": 60}
    d = decide_entry(up_prices(), 90500, 90000, quote, 1800)
    assert d.action in {"buy_yes", "no_trade"}


def test_invalidation_exit():
    class F:
        yes_edge_cents = -1
        no_edge_cents = 2

    exit_now, reason = should_exit("buy_yes", "trend_up", F(), True, 0)
    assert exit_now and reason == "fair_value_collapse"
