"""Small in-memory per-IP limiter. One process only; it resets on restart."""

from __future__ import annotations

import threading
import time
from collections import deque

from fastapi import HTTPException, Request

WINDOW_SECONDS = 60.0
_SWEEP_AT = 1000  # tracked IPs before idle ones are dropped


class RateLimiter:
    def __init__(self, limit_per_minute: int):
        self.limit = limit_per_minute
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> float | None:
        """Record a hit. Return the seconds to wait when the key is over its limit."""
        if self.limit <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            if len(self._hits) >= _SWEEP_AT:
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < WINDOW_SECONDS}
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= WINDOW_SECONDS:
                hits.popleft()
            if len(hits) >= self.limit:
                return max(1.0, WINDOW_SECONDS - (now - hits[0]))
            hits.append(now)
            return None


def limit_chat(request: Request) -> None:
    limiter: RateLimiter = request.app.state.chat_limiter
    wait = limiter.check(request.client.host if request.client else "unknown")
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait a moment and try again.",
            headers={"Retry-After": str(int(wait) + 1)},
        )
