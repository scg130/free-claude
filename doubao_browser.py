"""Playwright 浏览器内调用豆包 /chat/completion（SSE），自动携带 Cookie 与 a_bogus。"""

import asyncio
import json
import time
import uuid
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

PARAM_FILE = Path("doubao_ws_params.json")
PROFILE_DIR = Path(".doubao_browser_profile")
CHAT_URL = "https://www.doubao.com/chat/"
COMPLETION_URL = "https://www.doubao.com/chat/completion"
DEFAULT_BOT_ID = "7338286299411103781"

JS_STREAM = """
async (args) => {
  const headers = {
    'Content-Type': 'application/json',
    'Agw-Js-Conv': 'str',
    'x-flow-trace': args.trace_id,
    'last-event-id': 'undefined',
  };
  const opts = {
    method: 'POST',
    headers,
    body: args.body,
    credentials: 'include',
    signal: AbortSignal.timeout(180000),
  };
  try {
    const res = await fetch(args.url, opts);
    if (!res.ok) {
      const t = await res.text();
      return { status: res.status, body: t.substring(0, 4000) };
    }
    const rdr = res.body.getReader();
    const dec = new TextDecoder();
    let body = '';
    while (true) {
      const { done, value } = await rdr.read();
      if (done) break;
      body += dec.decode(value, { stream: true });
    }
    return { status: res.status, body };
  } catch (e) {
    return { status: 0, body: 'JS error: ' + e.message };
  }
}
"""

_browser_lock = asyncio.Lock()
_playwright = None
_context: BrowserContext | None = None
_page: Page | None = None


def _save_session(sessionid: str, sessionid_ss: str) -> dict:
    data = {"sessionid": sessionid, "sessionid_ss": sessionid_ss}
    PARAM_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def load_session() -> dict:
    if not PARAM_FILE.exists():
        return {}
    return json.loads(PARAM_FILE.read_text(encoding="utf-8"))


def session_ready() -> bool:
    return bool(load_session().get("sessionid"))


async def _read_sessionid(context: BrowserContext) -> tuple[str, str]:
    sessionid = ""
    sessionid_ss = ""
    for cookie in await context.cookies("https://www.doubao.com"):
        if cookie["name"] == "sessionid":
            sessionid = cookie["value"]
        elif cookie["name"] == "sessionid_ss":
            sessionid_ss = cookie["value"]
    if not sessionid:
        raise RuntimeError("未登录豆包")
    return sessionid, sessionid_ss or sessionid


async def _wait_login(context: BrowserContext) -> tuple[str, str]:
    try:
        return await _read_sessionid(context)
    except RuntimeError:
        pass
    print("[doubao] 请在浏览器窗口中登录豆包…")
    for _ in range(180):
        try:
            sid, ss = await _read_sessionid(context)
            print("[doubao] 登录成功")
            return sid, ss
        except RuntimeError:
            await asyncio.sleep(1)
    raise TimeoutError("登录超时：请在浏览器中完成豆包登录")


async def _close_browser() -> None:
    global _playwright, _context, _page
    if _context:
        await _context.close()
    if _playwright:
        await _playwright.stop()
    _context = None
    _page = None
    _playwright = None


async def _ensure_page(*, headless: bool) -> Page:
    global _playwright, _context, _page
    if _page and not _page.is_closed():
        return _page

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _playwright = await async_playwright().start()
    _context = await _playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1280, "height": 800},
        locale="zh-CN",
        args=["--disable-blink-features=AutomationControlled"],
    )
    _page = _context.pages[0] if _context.pages else await _context.new_page()
    await _page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=90000)
    await _page.wait_for_timeout(2000)
    return _page


def _build_payload(prompt: str, conv_id: str) -> dict:
    now_ms = int(time.time() * 1000)
    return {
        "client_meta": {
            "local_conversation_id": conv_id,
            "conversation_id": "",
            "bot_id": DEFAULT_BOT_ID,
            "last_section_id": "",
            "last_message_index": None,
        },
        "messages": [
            {
                "local_message_id": str(uuid.uuid4()),
                "content_block": [
                    {
                        "block_type": 10000,
                        "content": {
                            "text_block": {
                                "text": prompt,
                                "icon_url": "",
                                "icon_url_dark": "",
                                "summary": "",
                            }
                        },
                        "block_id": str(uuid.uuid4()),
                        "parent_id": "",
                        "meta_info": [],
                        "append_fields": [],
                    }
                ],
                "message_status": 0,
            }
        ],
        "option": {
            "send_message_scene": "",
            "create_time_ms": now_ms,
            "need_create_conversation": True,
            "unique_key": str(uuid.uuid4()),
            "sse_recv_event_options": {"support_chunk_delta": True},
        },
        "ext": {
            "use_deep_think": "0",
            "fp": "free_claude",
            "commerce_credit_config_enable": "0",
        },
    }


def _parse_sse_text(raw: str) -> str:
    if not raw.strip():
        return ""
    if raw.strip().startswith("{"):
        try:
            return json.loads(raw).get("message", "")[:500]
        except json.JSONDecodeError:
            return ""

    parts: list[str] = []
    event = ""
    data_buf = ""

    def flush() -> None:
        nonlocal event, data_buf
        if not event or not data_buf:
            event, data_buf = "", ""
            return
        try:
            data = json.loads(data_buf) if data_buf != "{}" else {}
        except json.JSONDecodeError:
            event, data_buf = "", ""
            return

        if event == "CHUNK_DELTA":
            parts.append(data.get("text", ""))
        elif event == "STREAM_CHUNK":
            for op in data.get("patch_op", []):
                if op.get("patch_object") == 102:
                    content = op.get("patch_value", {}).get("content", "")
                    if isinstance(content, str) and content:
                        try:
                            parts.append(json.loads(content).get("text", ""))
                        except json.JSONDecodeError:
                            parts.append(content)
                elif op.get("patch_object") == 1:
                    for block in op.get("patch_value", {}).get("content_block", []):
                        if block.get("block_type") == 10000:
                            parts.append(
                                block.get("content", {})
                                .get("text_block", {})
                                .get("text", "")
                            )
        elif event == "STREAM_MSG_NOTIFY":
            for block in data.get("content", {}).get("content_block", []):
                if block.get("block_type") == 10000:
                    parts.append(
                        block.get("content", {})
                        .get("text_block", {})
                        .get("text", "")
                    )
        event, data_buf = "", ""

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            flush()
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            chunk = line[5:].strip()
            data_buf = f"{data_buf}\n{chunk}" if data_buf else chunk
    flush()
    return "".join(parts)


async def _refresh_locked(headless: bool | None) -> dict:
    await _close_browser()

    if headless is not False and PROFILE_DIR.exists() and any(PROFILE_DIR.iterdir()):
        try:
            page = await _ensure_page(headless=True)
            sid, ss = await _read_sessionid(page.context)
            return _save_session(sid, ss)
        except Exception as e:
            print(f"[doubao] 无头登录检查失败({e})，打开浏览器…")
            await _close_browser()

    page = await _ensure_page(headless=False)
    sid, ss = await _wait_login(page.context)
    result = _save_session(sid, ss)
    await page.goto(CHAT_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)
    return result


async def refresh_credentials(headless: bool | None = None) -> dict:
    async with _browser_lock:
        return await _refresh_locked(headless)


async def _chat_locked(prompt: str, conv_id: str, *, retried: bool = False) -> str:
    if not session_ready():
        await _refresh_locked(None)

    page = await _ensure_page(headless=True)
    if "doubao.com" not in page.url:
        await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1500)

    body = json.dumps(_build_payload(prompt, conv_id), ensure_ascii=False)
    res = await page.evaluate(
        JS_STREAM,
        {"url": COMPLETION_URL, "body": body, "trace_id": uuid.uuid4().hex},
    )
    status = res.get("status", 0)
    raw = res.get("body", "")

    if status in (401, 403) and not retried:
        print("[doubao] 会话过期，重新登录…")
        await _close_browser()
        await _refresh_locked(None)
        return await _chat_locked(prompt, conv_id, retried=True)

    if status != 200:
        raise RuntimeError(f"豆包 API HTTP {status}: {raw[:800]}")

    text = _parse_sse_text(raw)
    if not text:
        raise RuntimeError(f"豆包未返回文本，响应片段: {raw[:500]}")
    return text


async def chat_completion(prompt: str, conv_id: str) -> str:
    async with _browser_lock:
        return await _chat_locked(prompt, conv_id)


async def shutdown() -> None:
    """关闭 Playwright 浏览器，供服务退出时调用。"""
    async with _browser_lock:
        await _close_browser()


refresh_ws_params = refresh_credentials
