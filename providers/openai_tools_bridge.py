"""Anthropic Messages ↔ OpenAI Chat Completions（含 tools）格式转换。"""

import json
import uuid
from typing import Any

from providers.base import ChatResult


def anthropic_tools_to_openai(tools: list[dict]) -> list[dict]:
    oai_tools = []
    for t in tools:
        oai_tools.append(
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    return oai_tools


def _system_text(system: str | list | None) -> str | None:
    if system is None:
        return None
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts) if parts else None
    return None


def _tool_result_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False)


def anthropic_messages_to_openai(
    messages: list[dict],
    system: str | list | None,
) -> tuple[str | None, list[dict]]:
    """Anthropic messages → OpenAI messages（含 tool_calls / tool）。"""
    oai: list[dict] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "user":
            if isinstance(content, str):
                if content:
                    oai.append({"role": "user", "content": content})
                continue
            texts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    texts.append(block["text"])
                elif block.get("type") == "tool_result":
                    oai.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": _tool_result_content(block.get("content", "")),
                        }
                    )
            if texts:
                oai.append({"role": "user", "content": "\n".join(texts)})

        elif role == "assistant":
            if isinstance(content, str):
                oai.append({"role": "assistant", "content": content})
                continue
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    text_parts.append(block["text"])
                elif block.get("type") == "tool_use":
                    tool_calls.append(
                        {
                            "id": block.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(
                                    block.get("input") or {}, ensure_ascii=False
                                ),
                            },
                        }
                    )
            amsg: dict[str, Any] = {"role": "assistant"}
            if text_parts:
                amsg["content"] = "\n".join(text_parts)
            else:
                amsg["content"] = None
            if tool_calls:
                amsg["tool_calls"] = tool_calls
            oai.append(amsg)

    return _system_text(system), oai


def openai_message_to_anthropic(message: dict, finish_reason: str = "") -> ChatResult:
    """OpenAI assistant message → Anthropic content blocks。"""
    blocks: list[dict] = []
    content = message.get("content")
    if content:
        blocks.append({"type": "text", "text": content})

    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        try:
            inp = json.loads(raw_args)
        except json.JSONDecodeError:
            inp = {"raw": raw_args}
        blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:12]}",
                "name": fn.get("name", ""),
                "input": inp,
            }
        )

    if not blocks:
        blocks.append({"type": "text", "text": ""})

    stop_reason = "end_turn"
    if any(b.get("type") == "tool_use" for b in blocks):
        stop_reason = "tool_use"
    elif finish_reason == "tool_calls":
        stop_reason = "tool_use"

    return ChatResult.from_blocks(blocks, stop_reason=stop_reason)
