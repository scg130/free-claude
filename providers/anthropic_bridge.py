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


def _tool_summary(tools: list[dict], *, compact: bool = False) -> str:
    if compact:
        names = [t.get("name", "") for t in tools if t.get("name")]
        head = ", ".join(names[:50])
        if len(names) > 50:
            head += f" …共{len(names)}个"
        return head
    lines = []
    for t in tools:
        name = t.get("name", "")
        desc = t.get("description", "")
        schema = t.get("input_schema", {})
        props = schema.get("properties", {})
        prop_names = ", ".join(props.keys()) if props else "见 schema"
        lines.append(f"- {name}: {desc} (参数: {prop_names})")
    return "\n".join(lines)


def _tool_names(tools: list[dict] | None) -> set[str]:
    if not tools:
        return set()
    return {t.get("name", "") for t in tools if t.get("name")}


def _parse_json_object(text: str) -> dict | None:
    text = text.strip()
    if not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _is_tool_parameter_obj(obj: dict) -> bool:
    """模型常把 MCP/Read 参数误当成文件内容或裸 JSON 输出。"""
    if not obj:
        return False
    keys = set(obj.keys())
    if "tool_uses" in keys:
        return False
    if "symbol" in keys and ("includeCode" in keys or "projectPath" in keys):
        return True
    if "file_path" in keys and ("offset" in keys or "limit" in keys) and "content" not in keys:
        return True
    if keys <= {"file_path"} or keys <= {"path"}:
        return True
    if "query" in keys and len(keys) <= 3:
        return True
    return False


def _is_tool_parameter_json(text: str) -> bool:
    obj = _parse_json_object(text)
    return bool(obj and _is_tool_parameter_obj(obj))


def _infer_tool_from_params(obj: dict, tool_names: set[str]) -> tuple[str, dict] | None:
    """把误输出的 MCP/Read 参数映射为 Claude Code 可执行的工具调用。"""
    if "symbol" in obj:
        mcp = "mcp__codegraph__codegraph_node"
        if mcp in tool_names:
            return mcp, obj
    if "query" in obj:
        mcp = "mcp__codegraph__codegraph_explore"
        if mcp in tool_names:
            return mcp, obj
    file_path = obj.get("file_path") or obj.get("path")
    if file_path and "content" not in obj:
        inp: dict = {"file_path": file_path}
        if "offset" in obj:
            inp["offset"] = obj["offset"]
        if "limit" in obj:
            inp["limit"] = obj["limit"]
        if "Read" in tool_names:
            return "Read", inp
    return None


def _normalize_tool_block(
    name: str, inp: dict, tool_names: set[str], tid: str
) -> ToolUseBlock | None:
    """修正 Write 误用、裸参数 JSON，避免把工具参数写进源文件。"""
    if name == "Write":
        content = inp.get("content", "")
        if isinstance(content, str) and _is_tool_parameter_json(content):
            inner = _parse_json_object(content)
            if inner:
                inferred = _infer_tool_from_params(inner, tool_names)
                if inferred:
                    new_name, new_inp = inferred
                    print(f"[bridge] Write 内容实为工具参数，转为 {new_name}")
                    return ToolUseBlock(id=tid, name=new_name, input=new_inp)
            print("[bridge] 拒绝 Write：content 是工具参数 JSON，不是源码")
            return None
        if "content" not in inp or not str(inp.get("content", "")).strip():
            if _is_tool_parameter_obj(inp):
                inferred = _infer_tool_from_params(inp, tool_names)
                if inferred:
                    new_name, new_inp = inferred
                    print(f"[bridge] Write 参数实为读文件/MCP，转为 {new_name}")
                    return ToolUseBlock(id=tid, name=new_name, input=new_inp)
            print("[bridge] 拒绝 Write：缺少有效 content")
            return None
    elif _is_tool_parameter_obj(inp):
        inferred = _infer_tool_from_params(inp, tool_names)
        if inferred:
            new_name, new_inp = inferred
            print(f"[bridge] 工具 {name} 参数不规范，转为 {new_name}")
            return ToolUseBlock(id=tid, name=new_name, input=new_inp)
    return ToolUseBlock(id=tid, name=name, input=inp)


def _normalize_tool_uses(
    tool_uses: list[ToolUseBlock], tool_names: set[str]
) -> list[ToolUseBlock]:
    out: list[ToolUseBlock] = []
    for tu in tool_uses:
        normalized = _normalize_tool_block(tu.name, tu.input, tool_names, tu.id)
        if normalized:
            out.append(normalized)
    return out


_FILE_PATH_RE = re.compile(
    r"[`'\"]?((?:[\w.-]+/)*[\w.-]+\.(?:py|go|ts|tsx|js|jsx|rs|java|md|json|yaml|yml|sh))[`'\"]?",
    re.IGNORECASE,
)

_READ_INTENT_MARKERS = (
    "读取", "读一下", "读 ", "查看", "看看", "打开", "找到", "搜索", "定位", "查找",
    "read ", "look at", "open ", "locate", "search for", "find ",
)


def _extract_target_file(*sources: str) -> str:
    for source in sources:
        if not source:
            continue
        for match in _FILE_PATH_RE.finditer(source):
            path = match.group(1)
            if path:
                return path
    return ""


def _wants_read(raw: str, user_hint: str) -> bool:
    blob = f"{raw} {user_hint}".lower()
    return any(marker in blob for marker in _READ_INTENT_MARKERS)


def _collapse_repetitive_text(text: str) -> str:
    """去掉模型在一轮回复里重复粘贴的同一句空话。"""
    lines = text.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line.strip()
        if not key:
            if out and out[-1] != "":
                out.append("")
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return "\n".join(out).strip()


def _fallback_read_from_intent(
    raw: str,
    *,
    user_hint: str = "",
    messages: list[dict] | None = None,
    tool_names: set[str],
) -> list[ToolUseBlock]:
    """模型口头说要读文件，却没输出 tool JSON 时，自动补 Read 调用。"""
    if "Read" not in tool_names:
        return []
    if not _wants_read(raw, user_hint):
        return []

    sources = [user_hint, raw]
    if messages:
        sources.append(extract_user_text(messages))
        for msg in messages:
            if msg.get("role") != "user":
                continue
            sources.append(_block_text(msg.get("content", "")))
            break

    file_path = _extract_target_file(*sources)
    if not file_path:
        return []

    print(f"[bridge] 口头读文件意图 → 自动 Read: {file_path}")
    return [
        ToolUseBlock(
            id=f"toolu_{uuid.uuid4().hex[:12]}",
            name="Read",
            input={"file_path": file_path},
        )
    ]


def _extract_read_from_partial_json(text: str) -> list[ToolUseBlock]:
    """从被截断的 tool_uses Read JSON 中抢救 file_path。"""
    if '"tool_uses"' not in text and '"name"' not in text:
        return []
    if not re.search(r'"name"\s*:\s*"Read"', text, re.IGNORECASE):
        return []
    fp_match = re.search(r'"file_path"\s*:\s*"((?:\\.|[^"\\])*)"', text)
    if not fp_match:
        return []
    try:
        file_path = json.loads(f'"{fp_match.group(1)}"')
    except json.JSONDecodeError:
        file_path = fp_match.group(1)
    if not file_path:
        return []
    print(f"[bridge] 截断的 Read JSON 已修复: {file_path}")
    return [
        ToolUseBlock(
            id="toolu_001",
            name="Read",
            input={"file_path": file_path},
        )
    ]


def _truncate_text(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(已截断)"


def _trim_message_for_prompt(text: str) -> str:
    if "[工具 " in text or "[Tool " in text or "tool_result" in text.lower():
        return _truncate_text(text, 1500)
    return _truncate_text(text, 6000)


def is_qa_request(user_text: str) -> bool:
    """问答/解释类请求：交互模式应直接文字回答，不走 Agent tools。"""
    text = (user_text or "").strip().lower()
    if not text:
        return False
    action_markers = (
        "写", "创建", "新建", "修改", "删除", "运行", "执行", "修复", "实现", "添加",
        "write ", "create ", "fix ", "implement", "run ", "mkdir", "npm ", "git ",
        ".py", ".go", ".ts", ".js", "test.py", "到 test", "到test",
    )
    if any(m in text for m in action_markers):
        return False
    qa_markers = (
        "区别", "差异", "是什么", "什么是", "干嘛", "干啥", "怎么用", "如何使用",
        "为什么", "解释", "说明", "对比", "比较", "有哪些", "什么意思", "介绍一下",
        "分析", "梳理", "总结",
        "what ", "difference", "explain", "how does", "why ", "compare",
    )
    return any(m in text for m in qa_markers)


def build_agent_prompt(
    messages: list[dict],
    system: str | list | None,
    tools: list[dict] | None,
    *,
    project_context: str = "",
    compact_tools: bool = False,
    max_project_context_chars: int | None = None,
) -> str:
    """把 Anthropic 多轮对话 + 工具定义压成豆包可读的单一 prompt。"""
    sections: list[str] = []

    if project_context:
        ctx = project_context.strip()
        if max_project_context_chars:
            ctx = _truncate_text(ctx, max_project_context_chars)
        sections.append(ctx)

    if system:
        if isinstance(system, str):
            sections.append(f"## 系统指令\n{_truncate_text(system, 3000)}")
        elif isinstance(system, list):
            sys_text = _block_text(system)
            if sys_text:
                sections.append(f"## 系统指令\n{_truncate_text(sys_text, 3000)}")

    if tools:
        sections.append(
            "## 可用工具\n"
            + _tool_summary(tools, compact=compact_tools)
            + "\n\n## 工具调用格式\n"
            "当你需要读文件、写文件、执行命令时，在回复末尾单独一行输出 JSON（不要用 markdown 代码块包裹）：\n"
            '{"tool_uses":[{"id":"toolu_001","name":"Read","input":{"file_path":"trans_api.py"}}]}\n'
            '{"tool_uses":[{"id":"toolu_002","name":"Write","input":{"file_path":"trans_api.py","content":"完整源码"}}]}\n'
            "规则：\n"
            "- id 必须以 toolu_ 开头\n"
            "- name 必须是上面列出的工具名之一（含 mcp__ 开头的 MCP 工具）\n"
            "- input 必须符合该工具的参数\n"
            "- 修改文件前必须先 Read 读取当前内容，禁止臆测文件内容\n"
            "- Write 的 content 必须是完整源码，禁止把 Read/MCP 的参数 JSON 当作 content 写入\n"
            "- 禁止只输出裸 JSON（如 {\"file_path\":...,\"offset\":...}），必须包在 tool_uses 里\n"
            "- 可同时输出说明文字 + 一行 JSON\n"
            "- 不需要工具时，只输出普通文字，不要输出 JSON\n"
            "- 禁止输出 [调用 Write]、[已调用 Bash]、[工具 xxx 返回]、### 用户 等格式\n"
            "- 禁止用 <function_json>、<bash>、<bash_command>、<write>、<read>、<tool_call> 等 XML 标签\n"
            "- 禁止说「让我先搜索/读取/查看代码库」却不输出工具 JSON\n"
            "- 禁止只写「我来读取 xxx」而不在末尾附上 tool JSON；需要读文件时必须输出 Read 的 tool_uses\n"
            "- 问答/对比/解释类问题：直接用文字完整回答，不要调用工具\n"
            "- 禁止模拟工具执行结果或多轮对话，只输出当前这一轮助手回复\n"
            "- 每次只调用一个工具，等待结果后再继续"
        )

    history: list[str] = []
    recent = messages[-8:] if len(messages) > 8 else messages
    for msg in recent:
        role = msg.get("role", "user")
        text = _trim_message_for_prompt(_block_text(msg.get("content", "")))
        if not text:
            continue
        label = {"user": "用户", "assistant": "助手"}.get(role, role)
        history.append(f"### {label}\n{text}")

    if history:
        sections.append("## 对话历史\n" + "\n\n".join(history))

    sections.append("## 请继续\n请结合上方项目代码库完成用户最新请求。需要操作文件或命令时使用工具 JSON。")
    return "\n\n".join(sections)


_QA_INSTRUCTION = (
    "你是项目开发助手。下方附带了当前项目的代码库信息。\n"
    "必须结合该项目代码与架构回答，不要给出与项目无关的通用科普。\n"
    "禁止说「我无法访问你的项目/代码/文档」。\n"
)


def build_qa_user_prompt(user_text: str, context: str = "") -> str:
    """问答模式：带项目上下文与回答约束的单轮 prompt。"""
    parts = [_QA_INSTRUCTION.strip()]
    if context:
        parts.append(context.strip())
    parts.append(f"## 用户问题\n{user_text.strip()}")
    return "\n\n".join(parts)


def extract_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if parts:
                return "".join(parts)
    return ""


_TOOL_RETRY_SUFFIX = (
    "\n\n## 系统提醒\n"
    "上次回复未包含合法 tool JSON（`{\"tool_uses\":[...]}`）。"
    "若需读文件，用 Read 工具；写文件前必须先 Read。"
    "若需读文件、写文件或执行命令，必须在回复**末尾单独一行**输出工具 JSON。"
    "不要把工具参数 JSON 写进 Write 的 content。"
)


async def run_agent_parse_loop(
    fetch_raw,
    *,
    messages: list[dict],
    system: str | list | None,
    tools: list[dict],
    project_context: str = "",
    user_hint: str = "",
    max_attempts: int | None = None,
    compact_tools: bool = False,
    max_project_context_chars: int | None = None,
) -> AgentResult:
    """调用 LLM 并解析 tool_use；解析失败时重试（默认 RETRY_MAX 次）。"""
    from config import APP

    attempts = max(1, max_attempts or APP.retry_max)
    base_prompt = build_agent_prompt(
        messages,
        system,
        tools,
        project_context=project_context,
        compact_tools=compact_tools,
        max_project_context_chars=max_project_context_chars,
    )
    last_raw = ""
    agent = AgentResult()

    for attempt in range(1, attempts + 1):
        prompt = base_prompt if attempt == 1 else base_prompt + _TOOL_RETRY_SUFFIX
        if attempt > 1:
            print(f"[bridge] 未解析到 tool_use，第 {attempt}/{attempts} 次重试…")
        last_raw = await fetch_raw(prompt)
        agent = parse_agent_response(
            last_raw,
            user_hint=user_hint,
            messages=messages,
            tools=tools,
            log_if_missing=False,
        )
        if agent.tool_uses:
            return agent

    if last_raw.strip():
        preview = last_raw.strip().replace("\n", " ")[:120]
        print(
            f"[bridge] 未解析到 tool_use（已重试 {attempts} 次），将作为纯文本返回: {preview}…"
        )
    return agent


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
    """解析 [已调用 Bash id=...] / [已调用工具 Read]{...} 等仿造格式。"""
    header_with_id = re.compile(
        r"\[(?:已调用(?:工具\s+)?|Called(?:\s+tool)?)\s*(\w+)\s+id=([^\s]+)\s+input=",
        re.IGNORECASE,
    )
    header_short = re.compile(
        r"\[(?:已调用(?:工具\s+)?|Called(?:\s+tool)?)\s*(\w+)\]\s*",
        re.IGNORECASE,
    )
    tool_uses: list[ToolUseBlock] = []
    spans: list[tuple[int, int]] = []

    for match in header_with_id.finditer(text):
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
        for match in header_short.finditer(text):
            name = match.group(1)
            obj, end = _decode_json_object(text, match.end())
            if not obj or not isinstance(obj, dict):
                continue
            if end < len(text) and text[end] == "]":
                end += 1
            tid = f"toolu_{uuid.uuid4().hex[:12]}"
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
        (re.compile(r"<bash_command>\s*(.*?)\s*</bash_command>", re.DOTALL | re.IGNORECASE), "Bash"),
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


def _parse_tool_call_inner(inner: str) -> dict | None:
    inner = inner.strip()
    args_match = re.search(
        r"<arguments>\s*(.*?)\s*</arguments>",
        inner,
        re.DOTALL | re.IGNORECASE,
    )
    if args_match:
        inner = args_match.group(1).strip()
    obj, _ = _decode_json_object_loose(inner, inner.find("{"))
    if obj and isinstance(obj, dict):
        return obj
    return None


def _extract_tool_call_tag_tool_calls(text: str) -> tuple[str, list[ToolUseBlock]]:
    """解析 <tool_call name=\"Read\">{...}</tool_call> / <arguments> 格式。"""
    pattern = re.compile(
        r'<tool_call\s+name="([^"]+)"\s*>\s*(.*?)\s*</tool_call>',
        re.DOTALL | re.IGNORECASE,
    )
    candidates: list[tuple[int, int, str, dict]] = []
    for match in pattern.finditer(text):
        name = match.group(1).strip()
        inp = _parse_tool_call_inner(match.group(2))
        if name and inp:
            candidates.append((match.start(), match.end(), name, inp))

    if not candidates:
        return text.strip(), []

    candidates.sort(key=lambda item: item[0])
    start, _end, tool_name, inp = candidates[0]
    clean = _truncate_hallucinated_turns(text[:start].strip())
    tid = f"toolu_{uuid.uuid4().hex[:12]}"
    return clean, [ToolUseBlock(id=tid, name=tool_name, input=inp)]


def _unwrap_tool_markup(text: str) -> str:
    """去掉 <function_json> 包裹，保留内部 JSON（不处理 <tool_call>，由专用解析器处理）。"""
    text = re.sub(
        r"<function_json>\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*</function_json>", "", text, flags=re.IGNORECASE)
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


def _tool_uses_from_object(obj: dict, tool_names: set[str]) -> list[ToolUseBlock]:
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
    return _normalize_tool_uses(tool_uses, tool_names)


def _extract_tool_uses_object(
    text: str, tool_names: set[str]
) -> tuple[str, list[ToolUseBlock]]:
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
        tool_uses = _tool_uses_from_object(obj, tool_names)
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


def _extract_standalone_json_tool_calls(
    text: str, tool_names: set[str]
) -> tuple[str, list[ToolUseBlock]]:
    """解析模型只输出一行裸 JSON 参数（未包在 tool_uses 里）的情况。"""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{") or not stripped.endswith("}"):
            continue
        obj = _parse_json_object(stripped)
        if not obj or not _is_tool_parameter_obj(obj):
            continue
        inferred = _infer_tool_from_params(obj, tool_names)
        if not inferred:
            continue
        name, inp = inferred
        clean = text.replace(line, "", 1).strip()
        print(f"[bridge] 裸 JSON 参数已转为 {name} 工具调用")
        return clean, [
            ToolUseBlock(id=f"toolu_{uuid.uuid4().hex[:12]}", name=name, input=inp)
        ]
    return text.strip(), []


def _extract_json_tool_block(
    text: str, tool_names: set[str]
) -> tuple[str, list[ToolUseBlock]]:
    """从模型回复中解析 tool_uses JSON，返回 (纯文本, 工具列表)。"""
    text = _collapse_repetitive_text(_truncate_hallucinated_turns(text))

    clean, tool_uses = _extract_standalone_json_tool_calls(text, tool_names)
    if tool_uses:
        return clean, tool_uses

    clean, tool_uses = _extract_tool_call_tag_tool_calls(text)
    if tool_uses:
        return clean, _normalize_tool_uses(tool_uses, tool_names)

    text = _unwrap_tool_markup(text)

    clean, tool_uses = _extract_tool_uses_object(text, tool_names)
    if tool_uses:
        return clean, tool_uses

    clean, tool_uses = _extract_xml_tag_tool_calls(text)
    if tool_uses:
        return clean, _normalize_tool_uses(tool_uses, tool_names)

    clean, tool_uses = _extract_bracket_tool_calls(text)
    if tool_uses:
        return clean, _normalize_tool_uses(tool_uses, tool_names)

    clean, tool_uses = _extract_history_tool_calls(text)
    if tool_uses:
        return clean, _normalize_tool_uses(tool_uses, tool_names)

    partial = _extract_write_from_partial_json(text)
    if partial:
        json_start = text.find("{")
        clean = text[:json_start].strip() if json_start >= 0 else ""
        normalized = _normalize_tool_uses(partial, tool_names)
        if normalized:
            return clean, normalized

    partial_read = _extract_read_from_partial_json(text)
    if partial_read:
        json_start = text.find("{")
        clean = text[:json_start].strip() if json_start >= 0 else ""
        return clean, _normalize_tool_uses(partial_read, tool_names)

    return text.strip(), []


def _fallback_write_from_fence(text: str, user_hint: str = "") -> list[ToolUseBlock]:
    """模型只输出 markdown 代码块时，回退生成 Write 工具调用。"""
    fence = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if not fence:
        return []
    content = fence.group(1).strip()
    if len(content) < 10:
        return []
    if content.startswith("{") and _is_tool_parameter_json(content):
        return []

    file_path = ""
    for pattern in (
        r"[`'\"]?([\w./_-]+\.py)[`'\"]?",
        r"[`'\"]?([\w./_-]+\.(?:go|ts|js|tsx|jsx|rs|java))[`'\"]?",
    ):
        for source in (user_hint, text):
            m = re.search(pattern, source)
            if m:
                file_path = m.group(1).split("/")[-1]
                break
        if file_path:
            break
    if not file_path:
        return []

    return [
        ToolUseBlock(
            id=f"toolu_{uuid.uuid4().hex[:12]}",
            name="Write",
            input={"file_path": file_path, "content": content},
        )
    ]


def parse_agent_response(
    raw: str,
    *,
    user_hint: str = "",
    messages: list[dict] | None = None,
    tools: list[dict] | None = None,
    log_if_missing: bool = True,
) -> AgentResult:
    tool_names = _tool_names(tools)
    text, tool_uses = _extract_json_tool_block(raw, tool_names)
    if not tool_uses:
        tool_uses = _fallback_write_from_fence(text or raw, user_hint)
        if tool_uses:
            text = re.sub(
                r"```(?:python)?\s*\n.*?```",
                "",
                text or raw,
                flags=re.DOTALL | re.IGNORECASE,
            ).strip()
        tool_uses = _normalize_tool_uses(tool_uses, tool_names)
    if not tool_uses:
        tool_uses = _fallback_read_from_intent(
            raw,
            user_hint=user_hint,
            messages=messages,
            tool_names=tool_names,
        )
        if tool_uses:
            text = ""
    if len(tool_uses) > 1:
        tool_uses = tool_uses[:1]
    if not tool_uses and raw.strip() and log_if_missing:
        preview = raw.strip().replace("\n", " ")[:120]
        print(f"[bridge] 未解析到 tool_use，将作为纯文本返回: {preview}…")
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
            "<bash_command>",
            "<write>",
            "<read>",
            "<tool_call",
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
