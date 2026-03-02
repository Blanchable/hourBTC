from collections import deque
from dataclasses import dataclass
from time import time


@dataclass
class FeedPoint:
    ts: float
    price: float


class BTCFeed:
    def __init__(self, maxlen: int = 500):
        self.history = deque(maxlen=maxlen)

    def push(self, price: float) -> None:
        self.history.append(FeedPoint(time(), price))

    def is_stale(self, stale_seconds: int = 15) -> bool:
        if not self.history:
            return True
        return (time() - self.history[-1].ts) > stale_seconds

    def prices(self) -> list[float]:
        return [p.price for p in self.history]
