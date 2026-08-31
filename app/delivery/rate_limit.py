from __future__ import annotations

import asyncio
import time


class AsyncTokenBucket:
    """Small in-process limiter; leases and state remain durable in MongoDB."""

    def __init__(self, rate_per_second: float, capacity: float | None = None) -> None:
        self.rate = rate_per_second
        self.capacity = capacity or rate_per_second
        self.tokens = self.capacity
        self.updated_at = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated_at) * self.rate)
                self.updated_at = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait_for = (1 - self.tokens) / self.rate
            await asyncio.sleep(wait_for)

