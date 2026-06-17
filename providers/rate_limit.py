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


def _resolve_min_interval(
    rpm: float = 0,
    *,
    qps: float = 0,
    qpm: float = 0,
    use_global: bool = True,
) -> float:
    if use_global and APP.request_interval_sec > 0:
        return APP.request_interval_sec

    intervals: list[float] = []
    if qps > 0:
        intervals.append(1.0 / qps)
    if qpm > 0:
        intervals.append(60.0 / qpm)

    effective_rpm = rpm
    if effective_rpm <= 0 and use_global:
        effective_rpm = APP.rate_limit_rpm
    if effective_rpm > 0:
        intervals.append(60.0 / effective_rpm)

    return max(intervals) if intervals else 0.0


def get_limiter(
    provider_id: str,
    rpm: float = 0,
    *,
    qps: float = 0,
    qpm: float = 0,
    use_global: bool = True,
) -> RateLimiter:
    min_interval = _resolve_min_interval(rpm, qps=qps, qpm=qpm, use_global=use_global)
    limiter = _limiters.get(provider_id)
    if limiter is None or limiter._min_interval != min_interval:
        _limiters[provider_id] = RateLimiter(min_interval)
    return _limiters[provider_id]
