"""扫描工作区源码，供 Agent prompt 注入项目上下文。

项目目录：每次 API 请求从 Claude Code 的 system prompt 自动解析 working directory。
服务端（run.sh）只通过 CONTEXT* 环境变量控制是否注入、注入多少，不知道用户在哪个项目。
"""

import os
import re
from pathlib import Path

from config import CONTEXT as CONTEXT_CFG
from paths import ROOT_DIR

PROXY_DIR = ROOT_DIR.resolve()
CACHED_ROOT_FILE = PROXY_DIR / ".cache" / "active-project-root.txt"

DEFAULT_IGNORE_DIRS = frozenset({
    ".git",
    ".cache",
    ".profiles",
    ".doubao_browser_profile",
    "venv",
    "node_modules",
    "__pycache__",
    ".codegraph",
    "dist",
    "build",
    ".venv",
})

TEXT_EXTENSIONS = frozenset({
    ".py", ".go", ".js", ".ts", ".tsx", ".jsx", ".md", ".json", ".yaml", ".yml",
    ".sh", ".bash", ".toml", ".rs", ".java", ".kt", ".c", ".cpp", ".h", ".hpp",
    ".css", ".scss", ".html", ".xml", ".sql", ".ini", ".cfg", ".env.example",
})

PROJECT_MARKERS = (
    ".git",
    "go.mod",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "pom.xml",
    "Makefile",
    "README.md",
)

# Claude Code / Cursor 在 system 或 user_info 里传入工作目录
_CWD_PATTERNS = (
    re.compile(
        r"(?:working directory|工作目录|current directory|当前工作目录|"
        r"project directory|项目目录|cwd|workspace folder|工作区|workspace path)"
        r"\s*[:：=]\s*[`'\"]?([^\s\n`'\"<>]+)",
        re.I,
    ),
    re.compile(
        r"(?:User(?:'s)? working directory is|The user(?:'s)? cwd is|"
        r"当前目录为|会话目录)"
        r"\s+[`'\"]?([^\s\n`'\"<>]+)",
        re.I,
    ),
    re.compile(r"<working_directory>\s*([^\s<]+)\s*</working_directory>", re.I),
    re.compile(r"Workspace Path:\s*([^\s\n]+)", re.I),
)

_ABS_PATH = re.compile(
    r"(?:^|[\s\"'=(])((?:/(?:Users|home|tmp|var|opt|mnt|workspace)[^\s\n'\"<>]*)|"
    r"(?:[A-Za-z]:\\[^\s\n'\"<>]+))"
)


def _max_chars() -> int:
    return CONTEXT_CFG.max_chars


def _context_mode() -> str:
    return CONTEXT_CFG.mode


def _max_file_bytes() -> int:
    return CONTEXT_CFG.max_file_bytes


def _ignore_dirs() -> frozenset[str]:
    extra = CONTEXT_CFG.ignore
    names = {x.strip() for x in extra.split(",") if x.strip()}
    return DEFAULT_IGNORE_DIRS | names


def _is_proxy_install_dir(path: Path) -> bool:
    """free-claude 代理自身目录，不是用户项目。"""
    try:
        return path.resolve() == PROXY_DIR
    except OSError:
        return False


def _normalize_path(raw: str, *, allow_proxy: bool = False) -> Path | None:
    raw = raw.strip().strip("`'\".,;:")
    if not raw:
        return None
    try:
        path = Path(raw).expanduser().resolve()
    except OSError:
        return None
    if path.is_file():
        path = path.parent
    if not path.is_dir():
        return None
    if not allow_proxy and _is_proxy_install_dir(path):
        return None
    return path


def _save_cached_root(path: Path) -> None:
    try:
        CACHED_ROOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHED_ROOT_FILE.write_text(str(path.resolve()), encoding="utf-8")
    except OSError:
        pass


def _load_cached_root() -> Path | None:
    try:
        if not CACHED_ROOT_FILE.is_file():
            return None
        raw = CACHED_ROOT_FILE.read_text(encoding="utf-8").strip()
        return _normalize_path(raw, allow_proxy=True)
    except OSError:
        return None


def _collect_prompt_texts(
    system: str | list | None,
    messages: list[dict] | None,
) -> str:
    parts: list[str] = []
    if isinstance(system, str):
        parts.append(system)
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))

    if messages:
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system" and isinstance(content, str):
                parts.append(content)
            elif role == "system" and isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
            elif isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        inner = block.get("content", "")
                        if isinstance(inner, str):
                            parts.append(inner)
    return "\n".join(parts)


def _score_project_dir(path: Path) -> int:
    score = 0
    for marker in PROJECT_MARKERS:
        if (path / marker).exists():
            score += 10 if marker != "README.md" else 1
    return score


def _guess_from_absolute_paths(text: str, *, allow_proxy: bool = False) -> Path | None:
    best: Path | None = None
    best_score = -1
    seen: set[str] = set()
    for match in _ABS_PATH.finditer(text):
        raw = match.group(1)
        if raw in seen:
            continue
        seen.add(raw)
        path = _normalize_path(raw, allow_proxy=allow_proxy)
        if not path:
            continue
        score = _score_project_dir(path)
        if score > best_score:
            best, best_score = path, score
    return best


def _guess_from_user_info(text: str) -> Path | None:
    """从 Cursor / Claude Code 的 <user_info> 段解析工作区。"""
    if "user_info" not in text.lower() and "workspace path" not in text.lower():
        return None
    for pattern in _CWD_PATTERNS:
        for match in pattern.finditer(text):
            path = _normalize_path(match.group(1), allow_proxy=True)
            if path:
                return path
    return _guess_from_absolute_paths(text, allow_proxy=True)


def _parse_cwd_from_text(text: str) -> Path | None:
    for pattern in _CWD_PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(1).strip(" `'\".,;:")
            if not raw:
                continue
            path = _normalize_path(raw, allow_proxy=True)
            if path:
                return path
    guessed = _guess_from_user_info(text)
    if guessed:
        return guessed
    return _guess_from_absolute_paths(text, allow_proxy=True)


def resolve_project_root(
    system: str | list | None = None,
    messages: list[dict] | None = None,
) -> Path | None:
    """从 Claude Code 请求的 system / messages 解析工作目录（不由 run.sh 服务端指定）。"""
    blob = _collect_prompt_texts(system, messages)
    if blob:
        parsed = _parse_cwd_from_text(blob)
        if parsed:
            _save_cached_root(parsed)
            return parsed
        if any(p.search(blob) for p in _CWD_PATTERNS):
            return None

    cached = _load_cached_root()
    if cached:
        print(f"[context] 使用缓存工作目录: {cached}")
        return cached

    return None


def is_conversation_start(messages: list[dict]) -> bool:
    """首轮 Agent 对话（尚无 tool_result）时注入项目上下文。"""
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return False
    return True


def should_inject_context(messages: list[dict], tools: list[dict] | None) -> bool:
    if not CONTEXT_CFG.enabled:
        return False
    if CONTEXT_CFG.always:
        return True
    return is_conversation_start(messages)


def _iter_source_files(root: Path) -> list[Path]:
    ignore = _ignore_dirs()
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in ignore and not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            if path.suffix.lower() not in TEXT_EXTENSIONS:
                continue
            try:
                if path.stat().st_size > _max_file_bytes():
                    continue
            except OSError:
                continue
            files.append(path)
    return files


def _build_tree(root: Path, files: list[Path]) -> str:
    rel_paths = sorted(p.relative_to(root).as_posix() for p in files)
    if not rel_paths:
        return "(无源码文件)"
    return "\n".join(f"  {p}" for p in rel_paths)


def build_project_context(root: Path, *, mode: str | None = None) -> str:
    """生成项目上下文。默认 tree 模式（仅目录树，用 Read/MCP 读文件）。"""
    root = root.resolve()
    files = _iter_source_files(root)
    if not files:
        return ""

    mode = (mode or _context_mode()).strip().lower()
    tree = _build_tree(root, files)
    header = (
        f"## 当前项目代码库\n"
        f"根目录: {root}\n"
        f"回答与编码任务必须结合本项目代码；未列出的文件请用 Read / MCP 工具读取，不要臆测。\n\n"
        f"### 文件列表\n{tree}\n"
    )

    if mode == "tree":
        return header

    max_chars = _max_chars()
    parts = [header]
    used = len(header)

    if mode == "lite":
        parts.append("### 关键文件\n")
        used += len(parts[-1])
        key_names = (
            "README.md", "readme.md", "main.py", "trans_api.py", "app.py",
            "go.mod", "pyproject.toml", "package.json", "Cargo.toml",
        )
        included: set[str] = set()
        for name in key_names:
            path = root / name
            if not path.is_file() or name in included:
                continue
            included.add(name)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:8000]
            except OSError:
                continue
            block = f"\n--- {name} ---\n{text.rstrip()}\n"
            if used + len(block) > max_chars:
                break
            parts.append(block)
            used += len(block)
        for path in sorted(root.iterdir()):
            if used >= max_chars:
                break
            if not path.is_file() or path.name in included or path.name.startswith("."):
                continue
            if path.suffix.lower() not in TEXT_EXTENSIONS:
                continue
            try:
                if path.stat().st_size > _max_file_bytes():
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")[:8000]
            except OSError:
                continue
            block = f"\n--- {path.name} ---\n{text.rstrip()}\n"
            if used + len(block) > max_chars:
                break
            parts.append(block)
            used += len(block)
            included.add(path.name)
        return "".join(parts)

    parts.append("### 源码内容\n")
    used += len(parts[-1])
    included = 0
    for path in files:
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        block = f"\n--- {rel} ---\n{text.rstrip()}\n"
        if used + len(block) > max_chars:
            parts.append(
                f"\n… 其余 {len(files) - included} 个文件已省略"
                f"（超出 CONTEXT_MAX_CHARS）\n"
            )
            break
        parts.append(block)
        used += len(block)
        included += 1

    if included == 0 and mode == "full":
        return header
    return "".join(parts)


PROVIDER_QA_REL_PATHS = (
    "README.md",
    "providers/registry.py",
    "providers/base.py",
    "providers/doubao/provider.py",
    "providers/doubao/browser.py",
    "providers/deepseek/provider.py",
    "providers/deepseek/client.py",
    "providers/deepseek/browser.py",
    "trans_api.py",
)


def is_proxy_provider_qa(user_text: str) -> bool:
    """问的是 free-claude 里 doubao / deepseek 实现差异（非 AI 产品科普）。"""
    text = (user_text or "").strip().lower()
    if not text:
        return False
    providers = ("doubao", "deepseek", "豆包", "深度求索")
    if not any(p in text for p in providers):
        return False
    scope = ("项目", "本项目", "当前", "free-claude", "providers", "代理", "provider", "代码")
    qa = ("区别", "差异", "对比", "比较", "是什么", "分析", "梳理", "explain", "difference", "compare")
    return any(s in text for s in scope) or any(q in text for q in qa)


def build_proxy_provider_context(*, max_chars: int | None = None) -> str:
    """注入 free-claude 仓库内 doubao/deepseek 相关源码（问答模式专用）。"""
    limit = max_chars or min(_max_chars(), 25_000)
    per_file = 6_000
    parts = [
        "## free-claude 代理项目（Doubao / DeepSeek 实现）\n",
        f"安装目录: {PROXY_DIR}\n",
        "以下为本仓库源码，请据此对比两个 provider 的实现差异。\n",
    ]
    used = sum(len(p) for p in parts)
    for rel in PROVIDER_QA_REL_PATHS:
        path = PROXY_DIR / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text) > per_file:
            text = text[:per_file] + "\n…(文件已截断)"
        block = f"\n--- {rel} ---\n{text.rstrip()}\n"
        if used + len(block) > limit:
            parts.append("\n… 其余文件已省略（超出字符上限）\n")
            break
        parts.append(block)
        used += len(block)
    return "".join(parts)


def _log_missing_project_root() -> None:
    print(
        "[context] 未注入：请求中未识别到工作目录。"
        "请在项目目录运行 claude（Claude Code 会在 system prompt 传入 working directory）。"
    )


def resolve_request_context(
    messages: list[dict],
    system: str | list | None,
    user_text: str = "",
    *,
    for_qa: bool = False,
) -> str:
    """问答与 coding 均注入当前项目上下文（.env 中 CONTEXT=0 可关闭）。"""
    if not should_inject_context(messages, None):
        return ""

    root = resolve_project_root(system, messages)
    if not root:
        if is_proxy_provider_qa(user_text):
            ctx = build_proxy_provider_context()
            if ctx:
                print(f"[context] 已注入 free-claude provider 源码 ({len(ctx)} chars)")
            return ctx
        _log_missing_project_root()
        return ""

    mode = _context_mode()
    if for_qa and mode == "tree":
        mode = "lite"

    ctx = build_project_context(root, mode=mode)
    if ctx:
        label = "问答" if for_qa else "coding"
        print(f"[context] {label} 已注入项目上下文: {root} mode={mode} ({len(ctx)} chars)")
    return ctx


def maybe_project_context(
    messages: list[dict],
    system: str | list | None,
    tools: list[dict] | None,
) -> str:
    user_text = ""
    if messages:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    user_text = content
                break
    return resolve_request_context(messages, system, user_text, for_qa=False)


def maybe_qa_context(
    user_text: str,
    messages: list[dict],
    system: str | list | None,
    *,
    tools: list[dict] | None,
) -> str:
    return resolve_request_context(messages, system, user_text, for_qa=True)
