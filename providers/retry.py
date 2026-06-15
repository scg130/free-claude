"""异步重试（网络波动、临时失败）。"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    retry_if: Callable[[BaseException], bool] | None = None,
    label: str = "request",
) -> T:
    last: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except BaseException as exc:
            last = exc
            if attempt >= max_attempts:
                break
            if retry_if is not None and not retry_if(exc):
                break
            delay = base_delay * (2 ** (attempt - 1))
            print(f"[retry] {label} 第 {attempt} 次失败({exc})，{delay:.1f}s 后重试…")
            await asyncio.sleep(delay)
    assert last is not None
    raise last


def is_transient_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    transient = (
        "timeout",
        "timed out",
        "connection",
        "reset",
        "503",
        "502",
        "504",
        "429",
        "temporarily",
        "network",
        "econnreset",
        "js error",
    )
    return any(t in msg for t in transient)
