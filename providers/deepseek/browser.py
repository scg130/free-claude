"""DeepSeek：Playwright 浏览器登录 + 网页 session 凭证。"""

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from paths import ensure_provider_dir, provider_param_file, provider_profile_dir

PROVIDER_ID = "deepseek"
PARAM_FILE = provider_param_file(PROVIDER_ID)
PROFILE_DIR = provider_profile_dir(PROVIDER_ID)
CHAT_URL = "https://chat.deepseek.com/"
DEBUG_PORT = 9333
_PROFILE_LOCK_NAMES = ("SingletonLock", "SingletonSocket", "SingletonCookie")

_browser_lock = asyncio.Lock()
_playwright = None
_context: BrowserContext | None = None
_page: Page | None = None
_cdp_browser: Browser | None = None
_via_cdp = False
_captured_auth: str = ""
_captured_hif_leim: str = ""

_PROFILE_IN_USE_MSG = (
    "DeepSeek 浏览器配置目录已被占用（.profiles/deepseek）。"
    "请先关闭已打开的 DeepSeek/Chrome 窗口，或重新运行 ./run.sh（会自动清理残留锁）。"
    "WSL 若仍失败: ./run.sh --reinstall-system-deps"
)


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _profile_process_alive(profile_dir: Path) -> bool:
    needle = str(profile_dir.resolve())
    if sys.platform == "win32":
        return False
    try:
        result = subprocess.run(
            ["pgrep", "-f", needle],
            capture_output=True,
            timeout=3,
            check=False,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _profile_lock_paths(profile_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for name in _PROFILE_LOCK_NAMES:
        paths.append(profile_dir / name)
        paths.append(profile_dir / "Default" / name)
    return paths


def _clear_stale_profile_lock(profile_dir: Path) -> bool:
    """Chrome 异常退出后可能残留 Singleton* 锁；无 live 进程时安全删除。"""
    profile_dir = Path(profile_dir)
    lock_paths = [p for p in _profile_lock_paths(profile_dir) if p.exists() or p.is_symlink()]
    if not lock_paths:
        return False
    if _port_open(DEBUG_PORT) or _profile_process_alive(profile_dir):
        return False

    removed = False
    for path in lock_paths:
        try:
            path.unlink(missing_ok=True)
            removed = True
        except OSError:
            pass
    return removed


def _chromium_launch_args() -> list[str]:
    args = [
        "--disable-blink-features=AutomationControlled",
        f"--remote-debugging-port={DEBUG_PORT}",
    ]
    if _is_wsl() or sys.platform == "linux":
        args.extend(["--no-sandbox", "--disable-dev-shm-usage"])
    return args


def _save_session(ds_session_id: str, authorization: str) -> dict:
    auth = _normalize_auth(authorization)
    if not auth:
        raise RuntimeError("无法保存 DeepSeek 凭证：Bearer token 无效")
    ensure_provider_dir(PROVIDER_ID)
    data = {"ds_session_id": ds_session_id, "authorization": auth}
    PARAM_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def load_session() -> dict:
    if not PARAM_FILE.exists():
        return {}
    return json.loads(PARAM_FILE.read_text(encoding="utf-8"))


def _is_valid_bearer(token: str) -> bool:
    token = (token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if len(token) < 16:
        return False
    if token.startswith("{") or token.startswith("["):
        return False
    try:
        token.encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def valid_authorization(auth: str) -> bool:
    return _is_valid_bearer(auth)


def session_ready() -> bool:
    s = load_session()
    return bool(s.get("ds_session_id") and _is_valid_bearer(s.get("authorization", "")))


def _normalize_auth(token: str) -> str:
    token = (token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not _is_valid_bearer(token):
        return ""
    return f"Bearer {token}"


async def _read_token_from_page(page: Page) -> str:
    token = await page.evaluate(
        """() => {
        function ok(s) {
            return typeof s === 'string' && s.length >= 16 && !s.startsWith('{');
        }
        function extract(raw) {
            if (!raw) return '';
            if (ok(raw)) return raw;
            try {
                const o = JSON.parse(raw);
                if (typeof o === 'string' && ok(o)) return o;
                if (o && typeof o === 'object') {
                    for (const k of ['value', 'token', 'access_token', 'userToken']) {
                        if (ok(o[k])) return o[k];
                    }
                }
            } catch {}
            return '';
        }
        for (const k of ['userToken', 'USER_TOKEN', 'token', 'auth_token']) {
            const t = extract(localStorage.getItem(k));
            if (t) return t;
        }
        for (let i = 0; i < localStorage.length; i++) {
            const t = extract(localStorage.getItem(localStorage.key(i)));
            if (t) return t;
        }
        return '';
    }"""
    )
    return _normalize_auth(token)


async def _resolve_auth(page: Page) -> str:
    auth = _normalize_auth(_captured_auth)
    if auth:
        return auth
    auth = await _read_token_from_page(page)
    if auth:
        return auth
    saved = load_session().get("authorization", "")
    return _normalize_auth(saved)


async def _fetch_args(page: Page, extra: dict | None = None) -> dict:
    auth = await _resolve_auth(page)
    if not auth:
        raise RuntimeError(
            "DeepSeek 缺少 Bearer token：请运行 python deepseek_auth.py 登录并发送一条消息"
        )
    args = {"auth": auth}
    if _captured_hif_leim:
        args["hif"] = _captured_hif_leim
    if extra:
        args.update(extra)
    return args


async def _read_sessionid(context: BrowserContext) -> tuple[str, str]:
    ds_session_id = ""
    for cookie in await context.cookies("https://chat.deepseek.com"):
        if cookie["name"] == "ds_session_id":
            ds_session_id = cookie["value"]
            break

    auth = ""
    if _page:
        auth = await _resolve_auth(_page)

    if not ds_session_id:
        raise RuntimeError("未登录 DeepSeek（缺少 ds_session_id cookie）")
    if not auth:
        raise RuntimeError(
            "未登录 DeepSeek（userToken 无效或未登录）。"
            "请在浏览器中登录 chat.deepseek.com 并发送一条消息"
        )
    return ds_session_id, auth


async def _wait_login(context: BrowserContext, page: Page) -> tuple[str, str]:
    try:
        return await _read_sessionid(context)
    except RuntimeError:
        pass
    print("[deepseek] 请在浏览器窗口中登录 chat.deepseek.com…")
    for _ in range(300):
        try:
            sid, auth = await _read_sessionid(context)
            print("[deepseek] 登录成功")
            return sid, auth
        except RuntimeError:
            await asyncio.sleep(1)
    raise TimeoutError("登录超时：请在浏览器中完成 DeepSeek 登录")


async def _close_browser(*, force: bool = False) -> None:
    """关闭 Playwright 连接。CDP 复用模式下默认不断开已有 Chrome。"""
    global _playwright, _context, _page, _captured_auth, _via_cdp, _cdp_browser, _captured_hif_leim

    if _via_cdp and not force:
        _page = None
        _context = None
        _captured_auth = ""
        _captured_hif_leim = ""
        return

    if _via_cdp and _cdp_browser:
        try:
            await _cdp_browser.close()
        except Exception:
            pass
    elif _context:
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
    _cdp_browser = None
    _via_cdp = False
    _captured_auth = ""
    _captured_hif_leim = ""


def _attach_auth_listener(page: Page) -> None:
    global _captured_auth, _captured_hif_leim

    def on_request(request):
        global _captured_auth, _captured_hif_leim
        if "chat.deepseek.com" not in request.url:
            return
        auth = _normalize_auth(request.headers.get("authorization", ""))
        if auth:
            _captured_auth = auth
        hif = request.headers.get("x-hif-leim", "")
        if hif:
            _captured_hif_leim = hif

    page.on("request", on_request)


async def _connect_via_cdp() -> Page | None:
    global _playwright, _context, _page, _via_cdp, _cdp_browser

    try:
        if _playwright is None:
            _playwright = await async_playwright().start()
        _cdp_browser = await _playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{DEBUG_PORT}",
            timeout=5000,
        )
        if not _cdp_browser.contexts:
            await _cdp_browser.close()
            _cdp_browser = None
            return None

        _context = _cdp_browser.contexts[0]
        _via_cdp = True
        _page = _context.pages[0] if _context.pages else await _context.new_page()
        _attach_auth_listener(_page)
        if "deepseek.com" not in (_page.url or ""):
            await _page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=90000)
            await _page.wait_for_timeout(1500)
        return _page
    except Exception:
        _via_cdp = False
        _cdp_browser = None
        return None


async def _launch_persistent(*, headless: bool) -> Page:
    global _playwright, _context, _page, _via_cdp, _cdp_browser

    if _playwright is None:
        _playwright = await async_playwright().start()

    _via_cdp = False
    _cdp_browser = None
    launch_args = _chromium_launch_args()
    last_exc: Exception | None = None

    for attempt in range(2):
        try:
            _context = await _playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=headless,
                viewport={"width": 1280, "height": 800},
                locale="zh-CN",
                args=launch_args,
            )
            _page = _context.pages[0] if _context.pages else await _context.new_page()
            _attach_auth_listener(_page)
            await _page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=90000)
            await _page.wait_for_timeout(2000)
            return _page
        except Exception as exc:
            last_exc = exc
            page = await _connect_via_cdp()
            if page:
                print("[deepseek] 配置目录被占用，已连接到现有浏览器会话")
                return page
            if attempt == 0 and _clear_stale_profile_lock(PROFILE_DIR):
                print("[deepseek] 已清除残留 profile 锁，正在重试启动浏览器…")
                continue
            break

    raise RuntimeError(_PROFILE_IN_USE_MSG) from last_exc


async def _ensure_page(*, headless: bool) -> Page:
    global _page

    if _page and not _page.is_closed():
        return _page

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _clear_stale_profile_lock(PROFILE_DIR)

    page = await _connect_via_cdp()
    if page:
        print("[deepseek] 复用已有浏览器会话")
        return page

    return await _launch_persistent(headless=headless)


async def _refresh_locked(headless: bool | None) -> dict:
    if PARAM_FILE.exists() and not session_ready():
        PARAM_FILE.unlink()
        print("[deepseek] 检测到无效凭证，将重新登录…")

    if headless is not False and PROFILE_DIR.exists() and any(PROFILE_DIR.iterdir()):
        try:
            page = await _ensure_page(headless=True)
            sid, auth = await _read_sessionid(page.context)
            return _save_session(sid, auth)
        except Exception as e:
            print(f"[deepseek] 无头登录检查失败({e})，打开浏览器…")
            await _close_browser(force=not _via_cdp)

    page = await _ensure_page(headless=False)
    sid, auth = await _wait_login(page.context, page)
    result = _save_session(sid, auth)
    await page.wait_for_timeout(1500)
    return result


async def refresh_credentials(headless: bool | None = None) -> dict:
    async with _browser_lock:
        return await _refresh_locked(headless)


JS_FETCH = """
async (args) => {
  const base = 'https://chat.deepseek.com/api/v0';
  function apiHeaders(extra) {
    const h = {
      'content-type': 'application/json',
      'authorization': args.auth,
      'accept': '*/*',
      'x-app-version': '2.0.0',
      'x-client-version': '2.0.0',
      'x-client-platform': 'web',
      'x-client-locale': 'zh_CN',
      'x-client-timezone-offset': String(-new Date().getTimezoneOffset() * 60),
      ...extra,
    };
    if (args.hif) h['x-hif-leim'] = args.hif;
    return h;
  }
  try {
    if (args.step === 'create_session') {
      const res = await fetch(base + '/chat_session/create', {
        method: 'POST',
        headers: apiHeaders(),
        body: '{}',
        credentials: 'include',
        signal: AbortSignal.timeout(180000),
      });
      const data = await res.json();
      if (!res.ok || data.code !== 0) {
        return { error: 'create_session', status: res.status, detail: JSON.stringify(data) };
      }
      const biz = data.data.biz_data;
      const id = biz.id || (biz.chat_session && biz.chat_session.id);
      return { session_id: id };
    }
    if (args.step === 'pow_challenge') {
      const res = await fetch(base + '/chat/create_pow_challenge', {
        method: 'POST',
        headers: apiHeaders({
          referer: 'https://chat.deepseek.com/a/chat/s/' + args.session_id,
        }),
        body: JSON.stringify({ target_path: '/api/v0/chat/completion' }),
        credentials: 'include',
        signal: AbortSignal.timeout(180000),
      });
      const data = await res.json();
      if (!res.ok || data.code !== 0) {
        return { error: 'pow_challenge', status: res.status, detail: JSON.stringify(data) };
      }
      return { challenge: data.data.biz_data.challenge };
    }
    if (args.step === 'completion') {
      const res = await fetch(base + '/chat/completion', {
        method: 'POST',
        headers: apiHeaders({
          'accept': 'text/event-stream',
          'x-ds-pow-response': args.pow,
          referer: 'https://chat.deepseek.com/a/chat/s/' + args.session_id,
        }),
        body: args.body,
        credentials: 'include',
        signal: AbortSignal.timeout(180000),
      });
      if (res.status === 401 || res.status === 403) {
        return { auth_error: true, status: res.status, detail: await res.text() };
      }
      if (!res.ok) {
        return { error: 'completion', status: res.status, detail: (await res.text()).slice(0, 2000) };
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
    }
    return { error: 'unknown_step' };
  } catch (e) {
    return { error: 'js', detail: e.message };
  }
}
"""


async def ensure_runtime_page() -> Page:
    """保持无头浏览器常驻，供 API 调用（类似豆包）。"""
    async with _browser_lock:
        return await _ensure_page(headless=True)


async def chat_via_browser(
    prompt: str,
    *,
    model_type: str = "default",
    thinking_enabled: bool = False,
) -> dict:
    """在浏览器上下文中调用 DeepSeek API（自动携带 authorization / x-hif-leim / cookies）。"""
    from providers.deepseek.pow import PowChallenge, get_solver

    async with _browser_lock:
        page = await _ensure_page(headless=True)
        if "deepseek.com" not in page.url:
            await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1500)

        fetch_args = await _fetch_args(page)

        sess = await page.evaluate(JS_FETCH, {**fetch_args, "step": "create_session"})
        if sess.get("error"):
            return sess

        session_id = sess["session_id"]
        pow_res = await page.evaluate(
            JS_FETCH, {**fetch_args, "step": "pow_challenge", "session_id": session_id}
        )
        if pow_res.get("error"):
            return pow_res

        raw = pow_res["challenge"]
        challenge = PowChallenge(
            algorithm=raw["algorithm"],
            challenge=raw["challenge"],
            salt=raw["salt"],
            signature=raw["signature"],
            difficulty=int(raw["difficulty"]),
            expire_at=int(raw["expire_at"]),
            target_path=raw["target_path"],
        )
        pow_header = get_solver().build_header(challenge)

        body = json.dumps(
            {
                "chat_session_id": session_id,
                "parent_message_id": None,
                "model_type": model_type,
                "prompt": prompt,
                "ref_file_ids": [],
                "thinking_enabled": thinking_enabled,
                "search_enabled": False,
                "action": None,
                "preempt": False,
            },
            ensure_ascii=False,
        )

        return await page.evaluate(
            JS_FETCH,
            {
                **fetch_args,
                "step": "completion",
                "session_id": session_id,
                "pow": pow_header,
                "body": body,
            },
        )


async def shutdown() -> None:
    async with _browser_lock:
        await _close_browser(force=True)
