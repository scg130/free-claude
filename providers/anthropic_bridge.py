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
    *,
    project_context: str = "",
) -> str:
    """把 Anthropic 多轮对话 + 工具定义压成豆包可读的单一 prompt。"""
    sections: list[str] = []

    if project_context:
        sections.append(project_context.strip())

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
            "- 禁止输出 [调用 Write]、[已调用 Bash]、[工具 xxx 返回]、### 用户 等格式\n"
            "- 禁止用 <function_json>、<bash>、<write>、<read> 等 XML 标签\n"
            "- 禁止模拟工具执行结果或多轮对话，只输出当前这一轮助手回复\n"
            "- 每次只调用一个工具，等待结果后再继续"
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
        "\n## 总结",
        "\n完美！",
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


def _extract_history_tool_calls(text: str) -> tuple[str, list[ToolUseBlock]]:
    """解析 [已调用 Bash id=... input={...}] / [已调用工具 Write id=...] 等仿造格式。"""
    header = re.compile(
        r"\[(?:已调用(?:工具\s+)?|Called(?:\s+tool)?)\s*(\w+)\s+id=([^\s]+)\s+input=",
        re.IGNORECASE,
    )
    tool_uses: list[ToolUseBlock] = []
    spans: list[tuple[int, int]] = []

    for match in header.finditer(text):
        name = match.group(1)
        tid = match.group(2)
        obj, end = _decode_json_object(text, match.end())
        if not obj or not isinstance(obj, dict):
            continue
        if end < len(text) and text[end] == "]":
            end += 1
        if not tid.startswith("toolu_"):
            tid = f"toolu_{tid}"
        tool_uses.append(ToolUseBlock(id=tid, name=name, input=obj))
        spans.append((match.start(), end))

    if not tool_uses:
        return text.strip(), []

    clean_parts: list[str] = []
    last = 0
    for start, end in spans:
        clean_parts.append(text[last:start])
        last = end
    clean_parts.append(text[last:])
    return _truncate_hallucinated_turns("".join(clean_parts).strip()), tool_uses


def _parse_write_tag_inner(inner: str) -> dict | None:
    fp_match = re.search(
        r'file_path:\s*"((?:\\.|[^"\\])*)"',
        inner,
        re.IGNORECASE,
    )
    if fp_match:
        try:
            file_path = json.loads(f'"{fp_match.group(1)}"')
        except json.JSONDecodeError:
            file_path = fp_match.group(1)
    else:
        fp_match = re.search(r"file_path:\s*(\S+)", inner, re.IGNORECASE)
        if not fp_match:
            return None
        file_path = fp_match.group(1).strip().strip('"')

    content_match = re.search(r"content:\s*", inner, re.IGNORECASE)
    if not content_match:
        return None
    rest = inner[content_match.end() :].lstrip()
    if not rest.startswith('"'):
        return None
    content, _ = _read_json_string(rest, 0)
    if not content:
        return None
    return {"file_path": file_path, "content": content}


def _parse_read_tag_inner(inner: str) -> dict | None:
    fp_match = re.search(
        r'file_path:\s*"((?:\\.|[^"\\])*)"',
        inner,
        re.IGNORECASE,
    )
    if fp_match:
        try:
            file_path = json.loads(f'"{fp_match.group(1)}"')
        except json.JSONDecodeError:
            file_path = fp_match.group(1)
    else:
        fp_match = re.search(r"file_path:\s*(\S+)", inner, re.IGNORECASE)
        if not fp_match:
            return None
        file_path = fp_match.group(1).strip().strip('"')
    return {"file_path": file_path}


def _extract_xml_tag_tool_calls(text: str) -> tuple[str, list[ToolUseBlock]]:
    """解析 <bash>、<write>、<read> 等 DeepSeek 伪 XML 工具格式。"""
    tag_specs = (
        (re.compile(r"<bash>\s*(.*?)\s*</bash>", re.DOTALL | re.IGNORECASE), "Bash"),
        (re.compile(r"<write>\s*(.*?)\s*</write>", re.DOTALL | re.IGNORECASE), "Write"),
        (re.compile(r"<read>\s*(.*?)\s*</read>", re.DOTALL | re.IGNORECASE), "Read"),
    )
    candidates: list[tuple[int, int, str, dict]] = []

    for pattern, tool_name in tag_specs:
        for match in pattern.finditer(text):
            inner = match.group(1)
            if tool_name == "Write":
                inp = _parse_write_tag_inner(inner)
            elif tool_name == "Read":
                inp = _parse_read_tag_inner(inner)
            else:
                cmd = inner.strip()
                if not cmd:
                    continue
                inp = {"command": cmd, "description": cmd[:120]}
            if inp:
                candidates.append((match.start(), match.end(), tool_name, inp))

    if not candidates:
        return text.strip(), []

    candidates.sort(key=lambda item: item[0])
    start, _end, tool_name, inp = candidates[0]
    clean = _truncate_hallucinated_turns(text[:start].strip())
    tid = f"toolu_{uuid.uuid4().hex[:12]}"
    return clean, [ToolUseBlock(id=tid, name=tool_name, input=inp)]


def _unwrap_tool_markup(text: str) -> str:
    """去掉 <function_json>、<tool_call> 等包裹，保留内部 JSON。"""
    text = re.sub(
        r"<function_json>\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*</function_json>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<tool_call>\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*</tool_call>", "", text, flags=re.IGNORECASE)
    return text


def _repair_tool_json_text(raw: str) -> str:
    """修复 DeepSeek 常见截断：缺少数组 ]，如 ...\"}}} 应为 ...\"}}]}。"""
    raw = raw.strip()
    if raw.endswith('"}}}'):
        return raw[:-1] + "]" + raw[-1]
    if raw.endswith('"}}'):
        return raw + "]}"
    return raw


def _read_json_string(text: str, start: int) -> tuple[str | None, int]:
    """从 text[start] 处的 opening quote 读取 JSON 字符串（含转义）。"""
    if start >= len(text) or text[start] != '"':
        return None, start
    i = start + 1
    out: list[str] = []
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            if i + 1 >= len(text):
                return None, start
            nxt = text[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "t":
                out.append("\t")
            elif nxt == "r":
                out.append("\r")
            elif nxt == '"':
                out.append('"')
            elif nxt == "\\":
                out.append("\\")
            elif nxt == "u" and i + 5 < len(text):
                try:
                    out.append(chr(int(text[i + 2 : i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    return None, start
            else:
                out.append(nxt)
            i += 2
            continue
        if ch == '"':
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    return None, start


def _extract_write_from_partial_json(text: str) -> list[ToolUseBlock]:
    """JSON 无法完整解析时，从 tool_uses Write 片段提取 file_path / content。"""
    if '"tool_uses"' not in text and "<function_json>" not in text.lower():
        return []

    fp_match = re.search(r'"file_path"\s*:\s*"((?:\\.|[^"\\])*)"', text)
    if not fp_match:
        return []
    try:
        file_path = json.loads(f'"{fp_match.group(1)}"')
    except json.JSONDecodeError:
        file_path = fp_match.group(1)

    content_key = text.find('"content"')
    if content_key < 0:
        return []
    colon = text.find(":", content_key)
    if colon < 0:
        return []
    quote = text.find('"', colon + 1)
    if quote < 0:
        return []
    content, _ = _read_json_string(text, quote)
    if not content or len(content) < 10:
        return []

    return [
        ToolUseBlock(
            id="toolu_001",
            name="Write",
            input={"file_path": file_path, "content": content},
        )
    ]


def _decode_json_object_loose(text: str, start: int) -> tuple[dict | None, int]:
    obj, end = _decode_json_object(text, start)
    if obj:
        return obj, end

    end_bound = len(text)
    for marker in ("</function_json>", "</tool_call>", "\n### ", "\n[工具 ", "\n[Tool "):
        idx = text.find(marker, start)
        if idx >= 0:
            end_bound = min(end_bound, idx)

    raw = _repair_tool_json_text(text[start:end_bound])
    if not raw.startswith("{"):
        return None, start

    candidates = [raw]
    if raw != text[start:end_bound].strip():
        candidates.append(text[start:end_bound].strip())

    suffixes = ["", "}", "]}", "]}"] 
    if raw.endswith("}}}"):
        suffixes = ["]}", "]}"] + suffixes
    elif raw.endswith("}}"):
        suffixes = ["]}", "]}"] + suffixes

    for base in candidates:
        for suffix in suffixes:
            try:
                obj, rel_end = json.JSONDecoder().raw_decode(base + suffix)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj, start + rel_end
    return None, start


def _tool_uses_from_object(obj: dict) -> list[ToolUseBlock]:
    tool_uses: list[ToolUseBlock] = []
    for item in obj.get("tool_uses", []):
        name = item.get("name", "")
        if not name:
            continue
        tid = item.get("id") or f"toolu_{uuid.uuid4().hex[:12]}"
        if not tid.startswith("toolu_"):
            tid = f"toolu_{tid}"
        tool_uses.append(
            ToolUseBlock(id=tid, name=name, input=item.get("input") or {})
        )
    return tool_uses


def _extract_tool_uses_object(text: str) -> tuple[str, list[ToolUseBlock]]:
    """用 JSONDecoder 解析 {\"tool_uses\":[...]}，避免 content 里的 ] 截断正则。"""
    tool_uses: list[ToolUseBlock] = []
    search_from = 0
    found_span: tuple[int, int] | None = None

    while True:
        key_idx = text.find('"tool_uses"', search_from)
        if key_idx < 0:
            break
        obj_start = text.rfind("{", 0, key_idx)
        if obj_start < 0:
            search_from = key_idx + 1
            continue
        obj, end = _decode_json_object_loose(text, obj_start)
        if not obj or "tool_uses" not in obj:
            search_from = key_idx + 1
            continue
        found_span = (obj_start, end)
        tool_uses = _tool_uses_from_object(obj)
        break

    if not tool_uses or not found_span:
        return text.strip(), []

    start, end = found_span
    clean = text[:start] + text[end:]
    clean = re.sub(
        r"<function_json>\s*</function_json>",
        "",
        clean,
        flags=re.IGNORECASE | re.DOTALL,
    )
    clean = _truncate_hallucinated_turns(clean.strip())
    return clean, tool_uses


def _extract_json_tool_block(text: str) -> tuple[str, list[ToolUseBlock]]:
    """从模型回复中解析 tool_uses JSON，返回 (纯文本, 工具列表)。"""
    text = _unwrap_tool_markup(_truncate_hallucinated_turns(text))

    clean, tool_uses = _extract_tool_uses_object(text)
    if tool_uses:
        return clean, tool_uses

    clean, tool_uses = _extract_xml_tag_tool_calls(text)
    if tool_uses:
        return clean, tool_uses

    clean, tool_uses = _extract_bracket_tool_calls(text)
    if tool_uses:
        return clean, tool_uses

    clean, tool_uses = _extract_history_tool_calls(text)
    if tool_uses:
        return clean, tool_uses

    partial = _extract_write_from_partial_json(text)
    if partial:
        json_start = text.find("{")
        clean = text[:json_start].strip() if json_start >= 0 else ""
        return clean, partial

    return text.strip(), []


def _fallback_write_from_fence(text: str, user_hint: str = "") -> list[ToolUseBlock]:
    """模型只输出 markdown 代码块时，回退生成 Write 工具调用。"""
    fence = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if not fence:
        return []
    content = fence.group(1).strip()
    if len(content) < 10:
        return []

    file_path = "test.py"
    m = re.search(r"[`'\"]?([\w./_-]+\.py)[`'\"]?", user_hint)
    if m:
        file_path = m.group(1).split("/")[-1]
    else:
        m2 = re.search(r"[`'\"]?([\w./_-]+\.py)[`'\"]?", text)
        if m2:
            file_path = m2.group(1).split("/")[-1]

    return [
        ToolUseBlock(
            id=f"toolu_{uuid.uuid4().hex[:12]}",
            name="Write",
            input={"file_path": file_path, "content": content},
        )
    ]


def parse_agent_response(raw: str, *, user_hint: str = "") -> AgentResult:
    text, tool_uses = _extract_json_tool_block(raw)
    if not tool_uses:
        tool_uses = _fallback_write_from_fence(text or raw, user_hint)
        if tool_uses:
            text = re.sub(r"```(?:python)?\s*\n.*?```", "", text or raw, flags=re.DOTALL | re.IGNORECASE).strip()
    if len(tool_uses) > 1:
        tool_uses = tool_uses[:1]
    return AgentResult(text=text, tool_uses=tool_uses)


def to_anthropic_content(result: AgentResult) -> list[dict]:
    blocks: list[dict] = []
    text = result.text.strip()
    if result.tool_uses and text:
        # 去掉模型附带的 tool JSON / 仿造调用行，避免 Claude Code 当正文打印
        for marker in (
            '{"tool_uses"',
            '"tool_uses"',
            "[已调用",
            "[Called",
            "<bash>",
            "<write>",
            "<read>",
        ):
            idx = text.find(marker)
            if idx >= 0:
                text = text[:idx].strip()
                break
    if text:
        blocks.append({"type": "text", "text": text})
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
