"""Anthropic Messages ↔ 纯文本 LLM 的工具桥接（供 Claude Code Agent 使用）。"""

import json
import re
import uuid
from dataclasses import dataclass, field


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class AgentResult:
    text: str = ""
    tool_uses: list[ToolUseBlock] = field(default_factory=list)

    @property
    def stop_reason(self) -> str:
        return "tool_use" if self.tool_uses else "end_turn"


def _block_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "tool_use":
            parts.append(
                f"[已调用工具 {block.get('name')} id={block.get('id')} "
                f"input={json.dumps(block.get('input', {}), ensure_ascii=False)}]"
            )
        elif btype == "tool_result":
            parts.append(
                f"[工具 {block.get('tool_use_id')} 返回]\n{block.get('content', '')}"
            )
    return "\n".join(p for p in parts if p)


def _tool_summary(tools: list[dict]) -> str:
    lines = []
    for t in tools:
        name = t.get("name", "")
        desc = t.get("description", "")
        schema = t.get("input_schema", {})
        props = schema.get("properties", {})
        prop_names = ", ".join(props.keys()) if props else "见 schema"
        lines.append(f"- {name}: {desc} (参数: {prop_names})")
    return "\n".join(lines)


def build_agent_prompt(
    messages: list[dict],
    system: str | list | None,
    tools: list[dict] | None,
) -> str:
    """把 Anthropic 多轮对话 + 工具定义压成豆包可读的单一 prompt。"""
    sections: list[str] = []

    if system:
        if isinstance(system, str):
            sections.append(f"## 系统指令\n{system}")
        elif isinstance(system, list):
            sys_text = _block_text(system)
            if sys_text:
                sections.append(f"## 系统指令\n{sys_text}")

    if tools:
        sections.append(
            "## 可用工具\n"
            + _tool_summary(tools)
            + "\n\n## 工具调用格式\n"
            "当你需要读文件、写文件、执行命令时，在回复末尾单独一行输出 JSON（不要用 markdown 代码块包裹）：\n"
            '{"tool_uses":[{"id":"toolu_001","name":"Write","input":{"file_path":"test.py","content":"代码内容"}}]}\n'
            "规则：\n"
            "- id 必须以 toolu_ 开头\n"
            "- name 必须是上面列出的工具名之一\n"
            "- input 必须符合该工具的参数\n"
            "- 可同时输出说明文字 + 一行 JSON\n"
            "- 不需要工具时，只输出普通文字，不要输出 JSON\n"
            "- 禁止输出 [调用 Write]、[工具 xxx 返回]、### 用户 等格式\n"
            "- 禁止模拟工具执行结果或多轮对话，只输出当前这一轮助手回复"
        )

    history: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        text = _block_text(msg.get("content", ""))
        if not text:
            continue
        label = {"user": "用户", "assistant": "助手"}.get(role, role)
        history.append(f"### {label}\n{text}")

    if history:
        sections.append("## 对话历史\n" + "\n\n".join(history))

    sections.append("## 请继续\n请根据对话完成用户最新请求。需要操作文件或命令时使用工具 JSON。")
    return "\n\n".join(sections)


def _truncate_hallucinated_turns(text: str) -> str:
    """截断模型编造的多轮对话（### 用户、[工具 xxx 返回] 等）。"""
    for marker in (
        "\n### 用户",
        "\n### User",
        "\n[工具 ",
        "\n[Tool ",
        "\n## 系统提醒",
    ):
        idx = text.find(marker)
        if idx >= 0:
            text = text[:idx]
    return text.strip()


def _parse_inline_tool_json(name: str, raw: str) -> dict | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if "input" in data and isinstance(data["input"], dict):
        return data["input"]
    return data


def _decode_json_object(text: str, start: int) -> tuple[dict | None, int]:
    brace = text.find("{", start)
    if brace < 0:
        return None, start
    try:
        obj, end = json.JSONDecoder().raw_decode(text, brace)
    except json.JSONDecodeError:
        return None, start
    if isinstance(obj, dict):
        return obj, end
    return None, start


def _extract_bracket_tool_calls(text: str) -> tuple[str, list[ToolUseBlock]]:
    """解析 [调用 Write] {...} / [Call Write] {...} 等 DeepSeek 常见格式。"""
    header = re.compile(r"\[(?:调用|Call)\s+(\w+)\]\s*", re.IGNORECASE)
    tool_uses: list[ToolUseBlock] = []
    spans: list[tuple[int, int]] = []

    for match in header.finditer(text):
        name = match.group(1)
        obj, end = _decode_json_object(text, match.end())
        if not obj:
            continue
        inp = obj.get("input") if isinstance(obj.get("input"), dict) else obj
        if not isinstance(inp, dict):
            continue
        tid = f"toolu_{uuid.uuid4().hex[:12]}"
        tool_uses.append(ToolUseBlock(id=tid, name=name, input=inp))
        spans.append((match.start(), end))

    if not tool_uses:
        return text.strip(), []

    clean_parts: list[str] = []
    last = 0
    for start, end in spans:
        clean_parts.append(text[last:start])
        last = end
    clean_parts.append(text[last:])
    clean = _truncate_hallucinated_turns("".join(clean_parts).strip())
    return clean, tool_uses


def _extract_json_tool_block(text: str) -> tuple[str, list[ToolUseBlock]]:
    """从模型回复中解析 tool_uses JSON，返回 (纯文本, 工具列表)。"""
    text = _truncate_hallucinated_turns(text)
    pattern = re.compile(
        r'\{\s*"tool_uses"\s*:\s*\[.*?\]\s*\}',
        re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        fence = re.search(r"```(?:json)?\s*(\{.*?\"tool_uses\".*?\})\s*```", text, re.DOTALL)
        if fence:
            match = fence

    if not match:
        return _extract_bracket_tool_calls(text)

    raw_json = match.group(1) if match.lastindex else match.group(0)
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return _extract_bracket_tool_calls(text)

    tool_uses: list[ToolUseBlock] = []
    for item in data.get("tool_uses", []):
        name = item.get("name", "")
        if not name:
            continue
        tid = item.get("id") or f"toolu_{uuid.uuid4().hex[:12]}"
        if not tid.startswith("toolu_"):
            tid = f"toolu_{tid}"
        tool_uses.append(
            ToolUseBlock(id=tid, name=name, input=item.get("input") or {})
        )

    clean = _truncate_hallucinated_turns(
        (text[: match.start()] + text[match.end() :]).strip()
    )
    if tool_uses:
        return clean, tool_uses
    return _extract_bracket_tool_calls(text)


def parse_agent_response(raw: str) -> AgentResult:
    text, tool_uses = _extract_json_tool_block(raw)
    return AgentResult(text=text, tool_uses=tool_uses)


def to_anthropic_content(result: AgentResult) -> list[dict]:
    blocks: list[dict] = []
    if result.text:
        blocks.append({"type": "text", "text": result.text})
    for tu in result.tool_uses:
        blocks.append(
            {
                "type": "tool_use",
                "id": tu.id,
                "name": tu.name,
                "input": tu.input,
            }
        )
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return blocks
