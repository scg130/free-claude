"""DeepSeek 网页 chat.deepseek.com API 客户端（浏览器内 fetch，无需官方 API Key）。"""

import json

from providers.anthropic_bridge import (
    build_agent_prompt,
    parse_agent_response,
    to_anthropic_content,
)
from providers.base import ChatResult
from providers.deepseek import browser

WEB_BASE = "https://chat.deepseek.com"
API_PREFIX = "/api/v0"
COMPLETION_PATH = "/api/v0/chat/completion"


def api_key_ready() -> bool:
    return browser.session_ready()


def _model_flags(model: str | None) -> tuple[str, bool]:
    name = (model or "deepseek-chat").lower()
    model_type = "expert" if any(x in name for x in ("v4", "r4", "expert")) else "default"
    thinking = any(x in name for x in ("r1", "r4", "reasoner", "reasoning"))
    return model_type, thinking


def _parse_sse(raw: str, *, include_thinking: bool = False) -> str:
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
        raise RuntimeError(f"DeepSeek 未返回文本，响应片段: {body[:500]}")
    return text


async def chat_completion(prompt: str, model: str | None = None) -> ChatResult:
    text = await _web_completion(prompt, model=model)
    return ChatResult.from_text(text)


async def chat_agent(
    messages: list[dict],
    *,
    system: str | list | None = None,
    tools: list[dict] | None = None,
    model: str | None = None,
) -> ChatResult:
    if tools:
        prompt = build_agent_prompt(messages, system, tools)
        raw = await _web_completion(prompt, model=model)
        agent = parse_agent_response(raw)
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
