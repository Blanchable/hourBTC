from dataclasses import dataclass

from app.brokers.kalshi_client import KalshiClient, parse_btc_threshold
from app.feeds.btc_feed import BTCFeed
from app.strategy.engine import decide_entry


@dataclass
class AppState:
    connected: bool = False
    market_ticker: str = ""
    strike: float | None = None
    last_decision: str = "idle"
    market_title: str = ""


class Controller:
    def __init__(self, client: KalshiClient, feed: BTCFeed):
        self.client = client
        self.feed = feed
        self.state = AppState()

    def connect(self) -> None:
        self.client.connect()
        self.state.connected = True

    def refresh_market(self, spot_price: float | None = None) -> None:
        market = self.client.resolve_btc_hourly_trade_target_market(spot_price=spot_price)
        if not market:
            self.state.last_decision = "no_market"
            return
        self.state.market_ticker = market["ticker"]
        self.state.market_title = market.get("title", "")
        self.state.strike = parse_btc_threshold(market)

    def evaluate(self, quote: dict, seconds_to_expiry: int) -> str:
        prices = self.feed.prices()
        if not prices:
            self.state.last_decision = "no_feed"
            return "no_feed"
        decision = decide_entry(prices, prices[-1], self.state.strike, quote, seconds_to_expiry, self.feed.is_stale())
        self.state.last_decision = decision.reason
        return decision.action
