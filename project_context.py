"""扫描工作区源码，供 Agent prompt 注入项目上下文。"""

import os
import re
from pathlib import Path

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

# Claude Code 默认 system 里 # Environment 段含 working directory
_CWD_PATTERNS = (
    re.compile(
        r"(?:working directory|工作目录|current directory|当前工作目录|"
        r"project directory|项目目录|cwd|workspace folder|工作区)"
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
)

_ABS_PATH = re.compile(
    r"(?:^|[\s\"'=(])((?:/(?:Users|home|tmp|var|opt|mnt|workspace)[^\s\n'\"<>]*)|"
    r"(?:[A-Za-z]:\\[^\s\n'\"<>]+))"
)


def _max_chars() -> int:
    try:
        return max(4_000, int(os.environ.get("FREE_CLAUDE_CONTEXT_MAX_CHARS", "20000")))
    except ValueError:
        return 20_000


def _context_mode() -> str:
    return os.environ.get("FREE_CLAUDE_CONTEXT_MODE", "tree").strip().lower()


def _max_file_bytes() -> int:
    try:
        return max(1_024, int(os.environ.get("FREE_CLAUDE_CONTEXT_MAX_FILE_BYTES", "51200")))
    except ValueError:
        return 51_200


def _ignore_dirs() -> frozenset[str]:
    extra = os.environ.get("FREE_CLAUDE_CONTEXT_IGNORE", "")
    names = {x.strip() for x in extra.split(",") if x.strip()}
    return DEFAULT_IGNORE_DIRS | names


def _is_proxy_install_dir(path: Path) -> bool:
    """free-claude 代理自身目录，不是用户项目。"""
    try:
        return path.resolve() == PROXY_DIR
    except OSError:
        return False


def _normalize_path(raw: str) -> Path | None:
    raw = raw.strip().strip("`'\".,;:")
    if not raw:
        return None
    try:
        path = Path(raw).expanduser().resolve()
    except OSError:
        return None
    if not path.is_dir() or _is_proxy_install_dir(path):
        return None
    return path


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
            content = msg.get("content", "")
            if isinstance(content, str):
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


def _guess_from_absolute_paths(text: str) -> Path | None:
    best: Path | None = None
    best_score = -1
    seen: set[str] = set()
    for match in _ABS_PATH.finditer(text):
        raw = match.group(1)
        if raw in seen:
            continue
        seen.add(raw)
        path = _normalize_path(raw)
        if not path:
            continue
        score = _score_project_dir(path)
        # 文件路径 → 向上找含项目标记的目录
        if score == 0 and path.is_file():
            for parent in path.parents:
                if _is_proxy_install_dir(parent):
                    break
                parent_score = _score_project_dir(parent)
                if parent_score > 0:
                    path = parent
                    score = parent_score
                    break
        if score > best_score:
            best, best_score = path, score
    return best


def _clear_cached_root() -> None:
    try:
        CACHED_ROOT_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _load_cached_root() -> Path | None:
    try:
        raw = CACHED_ROOT_FILE.read_text(encoding="utf-8").strip()
        return _normalize_path(raw)
    except OSError:
        return None


def _save_cached_root(path: Path) -> None:
    if _is_proxy_install_dir(path):
        return
    try:
        CACHED_ROOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHED_ROOT_FILE.write_text(str(path.resolve()), encoding="utf-8")
    except OSError:
        pass


def _parse_cwd_from_text(text: str) -> Path | None:
    for pattern in _CWD_PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(1).strip(" `'\".,;:")
            if not raw:
                continue
            try:
                resolved = Path(raw).expanduser().resolve()
            except OSError:
                continue
            if _is_proxy_install_dir(resolved):
                _clear_cached_root()
                return None
            path = _normalize_path(raw)
            if path:
                return path
    return _guess_from_absolute_paths(text)


def resolve_project_root(
    system: str | list | None = None,
    messages: list[dict] | None = None,
) -> Path | None:
    """解析用户项目根目录：Claude Code 工作目录，而非 free-claude 安装目录。"""
    env_root = os.environ.get("FREE_CLAUDE_PROJECT_ROOT", "").strip()
    if env_root:
        path = _normalize_path(env_root)
        if path:
            _save_cached_root(path)
            return path
        print(f"[context] FREE_CLAUDE_PROJECT_ROOT 无效: {env_root}")

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
    if os.environ.get("FREE_CLAUDE_CONTEXT", "1").lower() in ("0", "false", "no"):
        return False
    if os.environ.get("FREE_CLAUDE_CONTEXT_ALWAYS", "").lower() in ("1", "true", "yes"):
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


def build_project_context(root: Path) -> str:
    """生成项目上下文。默认 tree 模式（仅目录树，用 Read/MCP 读文件）。"""
    root = root.resolve()
    files = _iter_source_files(root)
    if not files:
        return ""

    mode = _context_mode()
    tree = _build_tree(root, files)
    header = (
        f"## 当前项目代码库\n"
        f"根目录: {root}\n"
        f"请用 Read / MCP 工具按需读取文件，不要假设未读到的代码。\n\n"
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
        key_names = ("README.md", "readme.md", "main.py", "trans_api.py", "app.py")
        for name in key_names:
            path = root / name
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:8000]
            except OSError:
                continue
            block = f"\n--- {name} ---\n{text.rstrip()}\n"
            if used + len(block) > max_chars:
                break
            parts.append(block)
            used += len(block)
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
                f"（超出 FREE_CLAUDE_CONTEXT_MAX_CHARS）\n"
            )
            break
        parts.append(block)
        used += len(block)
        included += 1

    if included == 0 and mode == "full":
        return header
    return "".join(parts)


def maybe_project_context(
    messages: list[dict],
    system: str | list | None,
    tools: list[dict] | None,
) -> str:
    if not should_inject_context(messages, tools):
        return ""
    root = resolve_project_root(system, messages)
    if not root:
        print(
            "[context] 未识别到用户项目目录（请在项目目录运行 claude，"
            "或设置 FREE_CLAUDE_PROJECT_ROOT=$(pwd)）"
        )
        return ""
    ctx = build_project_context(root)
    if ctx:
        print(f"[context] 已注入项目上下文: {root} ({len(ctx)} chars)")
    return ctx
