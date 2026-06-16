"""ChatGPT 网页客户端（浏览器登录 + anthropic_bridge Agent）。"""

from config import CHATGPT
from providers.anthropic_bridge import (
    extract_user_text,
    run_agent_parse_loop,
    to_anthropic_content,
)
from providers.base import ChatResult
from providers.chatgpt import browser


def session_ready() -> bool:
    return browser.session_ready() or (
        browser.PROFILE_DIR.exists() and any(browser.PROFILE_DIR.iterdir())
    )


async def validate_session() -> bool:
    return await browser.validate_session()


async def chat_completion(prompt: str, model: str | None = None) -> ChatResult:
    text = await browser.chat_completion(prompt, model=model)
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
            return await browser.chat_completion(prompt, model=model)

        agent = await run_agent_parse_loop(
            fetch_raw,
            messages=messages,
            system=system,
            tools=tools,
            project_context=project_context,
            user_hint=user_hint,
            compact_tools=True,
            max_project_context_chars=CHATGPT.max_project_context_chars,
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


async def shutdown() -> None:
    await browser.shutdown()
