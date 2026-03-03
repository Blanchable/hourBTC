from dataclasses import dataclass

import httpx

SPOT_SOURCE_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"


@dataclass
class SpotClient:
    url: str = SPOT_SOURCE_URL
    timeout_seconds: float = 5.0

    def fetch_btc_spot(self) -> float:
        with httpx.Client(timeout=self.timeout_seconds) as client:
            resp = client.get(self.url)
            resp.raise_for_status()
            payload = resp.json()
        amount = payload.get("data", {}).get("amount")
        if amount is None:
            raise ValueError("spot payload missing data.amount")
        return float(amount)
