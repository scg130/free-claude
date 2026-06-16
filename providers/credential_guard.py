"""后台凭证健康检查与自动续期。"""

import asyncio
from typing import Any

from config import APP


async def _validate_doubao() -> bool:
    from providers.doubao import browser as doubao_browser

    if not doubao_browser.session_ready():
        return False
    try:
        return await doubao_browser.validate_session()
    except Exception as exc:
        print(f"[credential] doubao 校验失败: {exc}")
        return False


async def _validate_deepseek() -> bool:
    from providers.deepseek import browser as deepseek_browser

    if not deepseek_browser.session_ready():
        return False
    try:
        return await deepseek_browser.validate_session()
    except Exception as exc:
        print(f"[credential] deepseek 校验失败: {exc}")
        return False


async def _refresh_doubao() -> None:
    from providers.doubao import browser as doubao_browser

    print("[credential] doubao 自动刷新凭证…")
    await doubao_browser.refresh_credentials(headless=None)


async def _refresh_deepseek() -> None:
    from providers.deepseek import browser as deepseek_browser

    print("[credential] deepseek 自动刷新凭证…")
    await deepseek_browser.refresh_credentials(headless=None)
    await deepseek_browser.ensure_runtime_page()


async def _validate_chatgpt() -> bool:
    from providers.chatgpt import browser as chatgpt_browser

    if not chatgpt_browser.PROFILE_DIR.exists() or not any(
        chatgpt_browser.PROFILE_DIR.iterdir()
    ):
        return False
    try:
        return await chatgpt_browser.validate_session()
    except Exception as exc:
        print(f"[credential] chatgpt 校验失败: {exc}")
        return False


async def _refresh_chatgpt() -> None:
    from providers.chatgpt import browser as chatgpt_browser

    print("[credential] chatgpt 自动刷新凭证…")
    await chatgpt_browser.refresh_credentials(headless=None)


_CHECKERS = {
    "doubao": (_validate_doubao, _refresh_doubao),
    "deepseek": (_validate_deepseek, _refresh_deepseek),
    "chatgpt": (_validate_chatgpt, _refresh_chatgpt),
}


async def check_all_credentials() -> dict[str, Any]:
    status: dict[str, Any] = {}
    for pid, (validate, refresh) in _CHECKERS.items():
        ok = await validate()
        status[pid] = {"valid": ok}
        if not ok:
            try:
                await refresh()
                status[pid]["refreshed"] = True
                status[pid]["valid"] = await validate()
            except Exception as exc:
                status[pid]["refreshed"] = False
                status[pid]["error"] = str(exc)
    return status


async def credential_guard_loop() -> None:
    interval = APP.credential_check_interval
    if interval <= 0:
        return
    print(f"[credential] 后台检查已启动，间隔 {interval}s")
    while True:
        await asyncio.sleep(interval)
        try:
            await check_all_credentials()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[credential] 后台检查异常: {exc}")
