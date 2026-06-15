"""浏览器 Provider 共享逻辑。"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from config import BROWSER_LAUNCH_ARGS, BROWSER_LOCALE, BROWSER_VIEWPORT
from playwright.async_api import BrowserContext, Page

T = TypeVar("T")

ProfileChecker = Callable[[], bool]
LockCleaner = Callable[[], bool]


async def wait_for_credential(
    read_fn: Callable[[], Awaitable[T]],
    *,
    provider_label: str,
    wait_sec: int = 180,
) -> T:
    try:
        return await read_fn()
    except RuntimeError:
        pass
    print(f"[{provider_label}] 请在浏览器窗口中登录…")
    for _ in range(wait_sec):
        try:
            result = await read_fn()
            print(f"[{provider_label}] 登录成功")
            return result
        except RuntimeError:
            await asyncio.sleep(1)
    raise TimeoutError(f"{provider_label} 登录超时")


async def launch_persistent_page(
    playwright,
    profile_dir,
    *,
    headless: bool,
    chat_url: str,
    extra_args: list[str] | None = None,
) -> tuple[BrowserContext, Page]:
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = list(BROWSER_LAUNCH_ARGS)
    if extra_args:
        args.extend(extra_args)
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        viewport=BROWSER_VIEWPORT,
        locale=BROWSER_LOCALE,
        args=args,
    )
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto(chat_url, wait_until="domcontentloaded", timeout=90_000)
    await page.wait_for_timeout(2000)
    return context, page
