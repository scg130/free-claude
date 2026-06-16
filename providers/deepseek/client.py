"""DeepSeek 网页 chat.deepseek.com API 客户端（浏览器内 fetch，无需官方 API Key）。"""

import asyncio
import json

from config import APP, DEEPSEEK
from providers.anthropic_bridge import (
    extract_user_text,
    run_agent_parse_loop,
    to_anthropic_content,
)
from providers.base import ChatResult
from providers.deepseek import browser
from providers.rate_limit import get_limiter
from providers.retry import is_transient_error

WEB_BASE = "https://chat.deepseek.com"
API_PREFIX = "/api/v0"
COMPLETION_PATH = "/api/v0/chat/completion"


class DeepSeekRateLimitError(RuntimeError):
    """DeepSeek SSE 返回 rate_limit_reached。"""


def _deepseek_rpm() -> float:
    """DeepSeek 网页端限流比全局更严，默认 cap 12/min。"""
    if DEEPSEEK.rate_limit_rpm > 0:
        return DEEPSEEK.rate_limit_rpm
    if APP.rate_limit_rpm <= 0:
        return 12.0
    return min(APP.rate_limit_rpm, 12.0)


def api_key_ready() -> bool:
    return browser.session_ready()


def _extract_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str) and content:
            return content
    return ""


def _model_flags(model: str | None) -> tuple[str, bool]:
    name = (model or "deepseek-chat").lower()
    model_type = "expert" if any(x in name for x in ("v4", "r4", "expert")) else "default"
    thinking = any(x in name for x in ("r1", "r4", "reasoner", "reasoning"))
    return model_type, thinking


def _check_sse_rate_limit(raw: str) -> None:
    """解析 event: hint 中的 rate_limit_reached（无 RESPONSE 正文时也会出现）。"""
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("finish_reason") != "rate_limit_reached" and obj.get("type") != "error":
            continue
        content = str(obj.get("content") or "")
        if obj.get("finish_reason") == "rate_limit_reached" or "频繁" in content:
            raise DeepSeekRateLimitError(content or "消息发送过于频繁，请稍后重试")


def _parse_sse(raw: str, *, include_thinking: bool = False) -> str:
    _check_sse_rate_limit(raw)
    think_parts: list[str] = []
    response_parts: list[str] = []
    current_kind: str | None = None

    def norm_kind(fragment_type: str | None) -> str | None:
        if fragment_type == "THINK":
            return "think"
        if fragment_type == "RESPONSE":
            return "response"
        return None

    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue

        v = obj.get("v")
        p = obj.get("p")
        o = obj.get("o")

        if isinstance(v, dict):
            response = v.get("response", {})
            for fragment in response.get("fragments", []):
                kind = norm_kind(fragment.get("type"))
                content = fragment.get("content") or ""
                if kind:
                    current_kind = kind
                if content and kind == "response":
                    response_parts.append(content)
                elif content and kind == "think" and include_thinking:
                    think_parts.append(content)
            continue

        if p == "response/fragments" and o == "APPEND" and isinstance(v, list):
            for fragment in v:
                kind = norm_kind(fragment.get("type"))
                content = fragment.get("content") or ""
                if kind:
                    current_kind = kind
                if content and kind == "response":
                    response_parts.append(content)
                elif content and kind == "think" and include_thinking:
                    think_parts.append(content)
            continue

        if p == "response/fragments/-1/content" and isinstance(v, str):
            if current_kind == "response":
                response_parts.append(v)
            elif current_kind == "think" and include_thinking:
                think_parts.append(v)
            continue

        if isinstance(v, str) and "p" not in obj and "o" not in obj:
            if current_kind == "response":
                response_parts.append(v)
            elif current_kind == "think" and include_thinking:
                think_parts.append(v)

    if include_thinking and think_parts:
        return "".join(think_parts) + "\n\n" + "".join(response_parts)
    return "".join(response_parts)


async def _web_completion(
    prompt: str,
    *,
    model: str | None = None,
    retried: bool = False,
) -> str:
    model_type, thinking = _model_flags(model)
    last_rate_limit: DeepSeekRateLimitError | None = None

    async def _call() -> str:
        raw = await browser.chat_via_browser(
            prompt,
            model_type=model_type,
            thinking_enabled=thinking,
        )
        if raw.get("auth_error") and not retried:
            print("[deepseek] 会话过期，重新登录…")
            await browser.refresh_credentials()
            await browser.ensure_runtime_page()
            return await _web_completion(prompt, model=model, retried=True)

        if raw.get("error"):
            raise RuntimeError(f"DeepSeek {raw['error']}: {raw.get('detail', '')[:800]}")

        body = raw.get("body", "")
        text = _parse_sse(body, include_thinking=thinking)
        if not text:
            _check_sse_rate_limit(body)
            raise RuntimeError(f"DeepSeek 未返回文本，响应片段: {body[:500]}")
        return text

    for attempt in range(1, APP.retry_max + 1):
        await get_limiter("deepseek", _deepseek_rpm()).acquire()
        try:
            return await _call()
        except DeepSeekRateLimitError as exc:
            last_rate_limit = exc
            if attempt >= APP.retry_max:
                break
            wait = DEEPSEEK.rate_limit_backoff_sec * attempt
            print(
                f"[deepseek] 限流({exc})，{wait:.0f}s 后重试 "
                f"({attempt}/{APP.retry_max})…"
            )
            await asyncio.sleep(wait)
        except Exception as exc:
            if attempt >= APP.retry_max or not is_transient_error(exc):
                raise
            delay = APP.retry_base_delay * (2 ** (attempt - 1))
            print(f"[retry] deepseek 第 {attempt} 次失败({exc})，{delay:.1f}s 后重试…")
            await asyncio.sleep(delay)

    if last_rate_limit is not None:
        rpm = _deepseek_rpm()
        raise RuntimeError(
            f"DeepSeek 限流: {last_rate_limit}。"
            f"请等待 1–2 分钟后重试；可在 .env 设置 DEEPSEEK_RATE_LIMIT_RPM={max(6, int(rpm) - 2)} "
            f"或增大 DEEPSEEK_RATE_LIMIT_BACKOFF_SEC（当前退避 {DEEPSEEK.rate_limit_backoff_sec:.0f}s）"
        ) from last_rate_limit

    raise RuntimeError("DeepSeek 请求失败")


async def chat_completion(prompt: str, model: str | None = None) -> ChatResult:
    text = await _web_completion(prompt, model=model)
    return ChatResult.from_text(text)


async def chat_agent(
    messages: list[dict],
    *,
    system: str | list | None = None,
    tools: list[dict] | None = None,
    model: str | None = None,
    project_context: str = "",
) -> ChatResult:
    if tools:
        user_hint = extract_user_text(messages)

        async def fetch_raw(prompt: str) -> str:
            return await _web_completion(prompt, model=model)

        agent = await run_agent_parse_loop(
            fetch_raw,
            messages=messages,
            system=system,
            tools=tools,
            project_context=project_context,
            user_hint=user_hint,
        )
        blocks = to_anthropic_content(agent)
        return ChatResult.from_blocks(blocks, stop_reason=agent.stop_reason)

    user_parts: list[str] = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str) and content:
            user_parts.append(content)
    prompt = user_parts[-1] if user_parts else ""
    if not prompt:
        raise RuntimeError("未检测到用户提问")
    return await chat_completion(prompt, model=model)
