"""ChatGPT：Playwright 浏览器登录 + 页面 DOM 发消息（规避 backend-api 反自动化）。"""

import asyncio
import json
import sys
import time

from playwright.async_api import BrowserContext, Page, async_playwright

from config import APP, CHATGPT
from paths import ensure_provider_dir, provider_param_file, provider_profile_dir
from providers.browser_common import launch_persistent_page, wait_for_credential
from providers.rate_limit import get_limiter
from providers.retry import is_transient_error, with_retry
from providers.session_store import SessionStore

PROVIDER_ID = "chatgpt"
PARAM_FILE = provider_param_file(PROVIDER_ID)
PROFILE_DIR = provider_profile_dir(PROVIDER_ID)
CHAT_URL = CHATGPT.chat_url
_SESSION = SessionStore(PARAM_FILE)

_browser_lock = asyncio.Lock()
_playwright = None
_context: BrowserContext | None = None
_page: Page | None = None
_headless_mode = CHATGPT.headless

_BLOCK_MARKERS = (
    "unusual activity",
    "cf-browser-verification",
    "challenge-platform",
    "just a moment",
)

JS_READ_SESSION = """
async () => {
  const res = await fetch('/api/auth/session', { credentials: 'include' });
  const text = await res.text();
  if (!res.ok || text.trim().startsWith('<')) {
    return { error: true, status: res.status, detail: text.slice(0, 300) };
  }
  try {
    return JSON.parse(text);
  } catch (e) {
    return { error: true, detail: 'invalid json: ' + text.slice(0, 200) };
  }
}
"""


def _save_session(access_token: str) -> dict:
    ensure_provider_dir(PROVIDER_ID)
    return _SESSION.save({"access_token": access_token})


def load_session() -> dict:
    return _SESSION.load()


def session_ready() -> bool:
    return bool(load_session().get("access_token")) or (
        PROFILE_DIR.exists() and any(PROFILE_DIR.iterdir())
    )


def clear_session() -> None:
    _SESSION.clear()


def _is_block_page(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _BLOCK_MARKERS)


def _is_block_response(res: dict) -> bool:
    detail = str(res.get("detail", "")).lower()
    return res.get("blocked") or _is_block_page(detail)


async def _read_access_token(context: BrowserContext) -> str:
    page = _page
    if not page or page.is_closed():
        raise RuntimeError("浏览器未就绪")
    if "chatgpt.com" not in page.url:
        await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(2000)
    data = await page.evaluate(JS_READ_SESSION)
    if isinstance(data, dict) and data.get("error"):
        detail = data.get("detail", "")
        if _is_block_page(detail):
            raise RuntimeError(
                "ChatGPT 检测到自动化访问（Cloudflare/风控）。"
                "请设置 CHATGPT_HEADLESS=0 并在弹出的浏览器中手动登录。"
            )
        raise RuntimeError(f"无法读取 ChatGPT 会话: {detail[:200]}")
    token = (data or {}).get("accessToken", "")
    if not token:
        raise RuntimeError("未登录 ChatGPT，请在浏览器窗口完成登录")
    return token


async def _wait_login(context: BrowserContext) -> str:
    return await wait_for_credential(
        lambda: _read_access_token(context),
        provider_label="chatgpt",
        wait_sec=CHATGPT.login_wait_sec,
    )


async def _close_browser() -> None:
    global _playwright, _context, _page
    if _context:
        try:
            await _context.close()
        except Exception:
            pass
    if _playwright:
        try:
            await _playwright.stop()
        except Exception:
            pass
    _context = None
    _page = None
    _playwright = None


async def _ensure_page(*, headless: bool | None = None) -> Page:
    global _playwright, _context, _page, _headless_mode
    use_headless = _headless_mode if headless is None else headless
    if _page and not _page.is_closed():
        return _page

    await _close_browser()
    _playwright = await async_playwright().start()
    _context, _page = await launch_persistent_page(
        _playwright,
        PROFILE_DIR,
        headless=use_headless,
        chat_url=CHAT_URL,
    )
    return _page


async def validate_session() -> bool:
    if not PROFILE_DIR.exists() or not any(PROFILE_DIR.iterdir()):
        return False
    async with _browser_lock:
        for headless in (CHATGPT.headless, False):
            try:
                page = await _ensure_page(headless=headless)
                await _read_access_token(page.context)
                _headless_mode = headless
                return True
            except Exception:
                await _close_browser()
        return False


async def _refresh_locked(headless: bool | None) -> dict:
    await _close_browser()
    global _headless_mode

    try_headless = CHATGPT.headless if headless is None else headless
    if try_headless and PROFILE_DIR.exists() and any(PROFILE_DIR.iterdir()):
        try:
            page = await _ensure_page(headless=True)
            token = await _read_access_token(page.context)
            _headless_mode = True
            return _save_session(token)
        except Exception as e:
            print(f"[chatgpt] 无头登录检查失败({e})，打开可见浏览器…")
            await _close_browser()

    page = await _ensure_page(headless=False)
    _headless_mode = False
    token = await _wait_login(page.context)
    result = _save_session(token)
    await page.goto(CHAT_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    return result


async def refresh_credentials(headless: bool | None = None) -> dict:
    async with _browser_lock:
        return await _refresh_locked(headless)


def _resolve_web_model(model: str | None) -> str:
    if not model:
        return CHATGPT.default_model
    name = model.strip().lower()
    if name in ("chatgpt", "openai"):
        return CHATGPT.default_model
    return model


def _truncate_prompt(prompt: str) -> str:
    limit = CHATGPT.max_prompt_chars
    if len(prompt) <= limit:
        return prompt
    tail = min(limit // 2, 4000)
    head = limit - tail - 60
    print(f"[chatgpt] prompt 过长 ({len(prompt)} chars)，截断至 {limit}")
    return (
        prompt[:head]
        + "\n\n…(前文已截断，ChatGPT 网页输入长度受限)\n\n"
        + prompt[-tail:]
    )


async def _composer_locator(page: Page):
    for sel in (
        "div#prompt-textarea[contenteditable='true']",
        "#prompt-textarea",
        "div.ProseMirror[contenteditable='true']",
        "div[contenteditable='true'][role='textbox']",
        "textarea[data-testid='prompt-textarea']",
        "[data-testid='composer-text-input']",
    ):
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0 and await loc.is_visible():
                return loc
        except Exception:
            continue
    return None


async def _insert_prompt(page: Page, composer, prompt: str) -> None:
    """ProseMirror contenteditable 不能用 fill；优先 keyboard.insert_text。"""
    await composer.click()
    await page.wait_for_timeout(150)
    mod = "Meta" if sys.platform == "darwin" else "Control"
    try:
        await page.keyboard.press(f"{mod}+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.insert_text(prompt)
    except Exception:
        await composer.evaluate(
            """(el, text) => {
              el.focus();
              if (el.isContentEditable) {
                el.innerHTML = '';
                document.execCommand('insertText', false, text);
                el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText' }));
                return;
              }
              if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
                el.value = text;
                el.dispatchEvent(new Event('input', { bubbles: true }));
              }
            }""",
            prompt,
        )
    # 等待发送按钮变为可点
    send = page.locator('button[data-testid="send-button"]').first
    for _ in range(20):
        try:
            if await send.count() > 0 and await send.is_enabled():
                return
        except Exception:
            pass
        await page.wait_for_timeout(150)


async def _click_send(page: Page) -> None:
    sent = await page.evaluate(
        """() => {
      const selectors = [
        'button[data-testid="send-button"]',
        'button[aria-label*="Send"]',
        'button[aria-label*="发送"]',
      ];
      for (const sel of selectors) {
        const btn = document.querySelector(sel);
        if (btn && !btn.disabled) { btn.click(); return sel; }
      }
      return '';
    }"""
    )
    if sent:
        print(f"[chatgpt] 已点击发送 ({sent})")
        return
    await page.keyboard.press("Enter")
    print("[chatgpt] 已按 Enter 发送（发送按钮不可用）")


async def _wait_assistant_reply(page: Page, before: int) -> str:
    deadline = time.monotonic() + APP.fetch_timeout_ms / 1000
    last_text = ""
    last_log = 0.0
    while time.monotonic() < deadline:
        await page.wait_for_timeout(800)
        if time.monotonic() - last_log > 10:
            print("[chatgpt] 等待模型回复…")
            last_log = time.monotonic()

        if await page.locator(
            '[data-testid="stop-button"], button[aria-label*="Stop"], button[aria-label*="停止"]'
        ).count():
            continue

        count = await page.locator('[data-message-author-role="assistant"]').count()
        if count <= before:
            continue

        last = page.locator('[data-message-author-role="assistant"]').last
        text = (await last.inner_text()).strip()
        if not text:
            continue
        if text != last_text:
            last_text = text
            await page.wait_for_timeout(1500)
            if not await page.locator(
                '[data-testid="stop-button"], button[aria-label*="Stop"], button[aria-label*="停止"]'
            ).count():
                return text

    if last_text:
        return last_text
    raise RuntimeError("ChatGPT DOM 等待回复超时（请确认可见浏览器中已登录且未被风控）")


async def _chat_via_dom(page: Page, prompt: str) -> str:
    """在页面里输入并等待回复（不调用 backend-api）。"""
    prompt = _truncate_prompt(prompt)
    await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=90_000)
    await page.wait_for_timeout(2000)

    composer = await _composer_locator(page)
    if not composer:
        body = await page.content()
        if _is_block_page(body):
            raise RuntimeError(
                "ChatGPT 风控拦截（Unusual activity）。请 CHATGPT_HEADLESS=0，"
                "在可见浏览器中登录后重试，或稍后再试。"
            )
        raise RuntimeError("未找到 ChatGPT 输入框，请确认已在浏览器中登录")

    before = await page.locator('[data-message-author-role="assistant"]').count()
    print(f"[chatgpt] 输入 prompt ({len(prompt)} chars)…")
    await _insert_prompt(page, composer, prompt)
    await page.wait_for_timeout(300)
    inserted = await composer.evaluate(
        "(el) => (el.innerText || el.textContent || el.value || '').trim().length"
    )
    if inserted < min(20, len(prompt) // 10):
        raise RuntimeError(
            f"ChatGPT 输入框未写入内容（仅 {inserted} chars），"
            "可能 prompt 仍过长或页面未就绪"
        )
    await _click_send(page)
    return await _wait_assistant_reply(page, before)


async def _chat_locked(
    prompt: str,
    *,
    model: str | None = None,
    retried: bool = False,
) -> str:
    global _headless_mode
    del model  # 网页 DOM 模式暂不按 model 切换（使用账号默认模型）

    if not PROFILE_DIR.exists() or not any(PROFILE_DIR.iterdir()):
        await _refresh_locked(None)

    page = await _ensure_page(headless=_headless_mode)

    try:
        return await _chat_via_dom(page, prompt)
    except RuntimeError as e:
        msg = str(e)
        if _is_block_page(msg) and _headless_mode and not retried:
            print("[chatgpt] 无头模式被风控，切换可见浏览器…")
            await _close_browser()
            _headless_mode = False
            page = await _ensure_page(headless=False)
            try:
                await _read_access_token(page.context)
            except RuntimeError:
                await _refresh_locked(headless=False)
                page = await _ensure_page(headless=False)
            return await _chat_via_dom(page, prompt)
        if "未登录" in msg and not retried:
            print("[chatgpt] 会话失效，重新登录…")
            await _close_browser()
            await _refresh_locked(headless=False)
            return await _chat_locked(prompt, model=model, retried=True)
        raise


async def chat_completion(prompt: str, *, model: str | None = None) -> str:
    await get_limiter(PROVIDER_ID, APP.rate_limit_rpm).acquire()

    async def _call() -> str:
        async with _browser_lock:
            return await _chat_locked(prompt, model=model)

    return await with_retry(
        _call,
        max_attempts=APP.retry_max,
        base_delay=APP.retry_base_delay,
        retry_if=is_transient_error,
        label="chatgpt",
    )


async def shutdown() -> None:
    async with _browser_lock:
        await _close_browser()
