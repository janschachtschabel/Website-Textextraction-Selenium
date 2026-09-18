"""Inbound token bucket for the crawl endpoints, one per process."""

import time


class InboundLimit:
    """Each crawled URL costs one token; ``take`` returns 0 or the seconds to wait.

    One bucket serves all clients: with an API key there is one client identity, and
    without one the configuration only allows loopback clients.
    """

    def __init__(self, rate: float, burst: int, clock=time.monotonic):
        self.rate, self.burst, self.clock = rate, burst, clock
        self.tokens = float(burst)
        self.updated = clock()

    def take(self, cost: int) -> float:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        # A batch larger than the burst runs once the bucket is full, then pays off its excess.
        needed = min(cost, self.burst)
        if self.tokens < needed:
            return (needed - self.tokens) / self.rate
        self.tokens -= cost
        return 0.0
