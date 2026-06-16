"""按 provider 的简单速率限制（防封禁）。"""

import asyncio
import time

from config import APP

_limiters: dict[str, "RateLimiter"] = {}


class RateLimiter:
    def __init__(self, min_interval: float) -> None:
        self._min_interval = max(0.0, min_interval)
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


def _resolve_min_interval(rpm: float = 0) -> float:
    if APP.request_interval_sec > 0:
        return APP.request_interval_sec
    effective_rpm = rpm if rpm > 0 else APP.rate_limit_rpm
    if effective_rpm > 0:
        return 60.0 / effective_rpm
    return 0.0


def get_limiter(provider_id: str, rpm: float = 0) -> RateLimiter:
    min_interval = _resolve_min_interval(rpm)
    limiter = _limiters.get(provider_id)
    if limiter is None or limiter._min_interval != min_interval:
        _limiters[provider_id] = RateLimiter(min_interval)
    return _limiters[provider_id]
