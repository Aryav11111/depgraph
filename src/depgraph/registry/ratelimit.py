"""Token-bucket rate limiter for outbound requests."""

import asyncio
import time
from collections.abc import Awaitable, Callable


class TokenBucket:
    """Allows `rate` acquisitions per second on average, with bursts up to `burst`.

    Tokens refill continuously; `acquire()` waits just long enough for the next
    token. Waiters are served in arrival order (the lock is FIFO), so a burst of
    callers is smoothed into a steady stream instead of a thundering herd.
    Clock and sleep are injectable so tests run instantly and deterministically.
    """

    def __init__(
        self,
        rate: float,
        burst: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = burst if burst is not None else max(1.0, rate)
        self._clock = clock
        self._sleep = sleep
        self._tokens = self.capacity
        self._updated = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            self._refill()
            if self._tokens < 1:
                await self._sleep((1 - self._tokens) / self.rate)
                self._refill()
            # After sleeping the computed wait the token is ours by definition. Not
            # re-checking `>= 1` matters: float rounding can leave 0.9999999 and a
            # re-check loop would then spin on ever-tinier sleeps. A balance that
            # dips a hair below zero just delays the next caller by nanoseconds.
            self._tokens -= 1

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now
