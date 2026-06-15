"""按 provider 的简单速率限制（防封禁）。"""

import asyncio
import time

_limiters: dict[str, "RateLimiter"] = {}


class RateLimiter:
    def __init__(self, rpm: float) -> None:
        self._min_interval = 60.0 / rpm if rpm > 0 else 0.0
        self._lock = asyncio.Lock()
        self._last_at = 0.0

    async def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_at = time.monotonic()


def get_limiter(provider_id: str, rpm: float) -> RateLimiter:
    if provider_id not in _limiters:
        _limiters[provider_id] = RateLimiter(rpm)
    return _limiters[provider_id]
