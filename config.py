"""集中配置：启动时自动加载项目根目录 .env 文件。"""

import os
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path | None = None, *, override: bool = False) -> None:
    """加载 .env 到 os.environ（已存在的环境变量默认不覆盖）。"""
    env_path = path or _ROOT / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value


load_dotenv()


def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "ServerConfig":
        return cls(
            host=env_str("API_HOST", "127.0.0.1"),
            port=max(1, env_int("API_PORT", 8000)),
        )


@dataclass(frozen=True)
class ContextConfig:
    enabled: bool
    mode: str
    max_chars: int
    max_file_bytes: int
    ignore: str
    always: bool

    @classmethod
    def from_env(cls) -> "ContextConfig":
        return cls(
            enabled=env_bool("CONTEXT", True),
            mode=env_str("CONTEXT_MODE", "lite").lower(),
            max_chars=max(4_000, env_int("CONTEXT_MAX_CHARS", 20_000)),
            max_file_bytes=max(1_024, env_int("CONTEXT_MAX_FILE_BYTES", 51_200)),
            ignore=env_str("CONTEXT_IGNORE", ""),
            always=env_bool("CONTEXT_ALWAYS", False),
        )


@dataclass(frozen=True)
class AppConfig:
    retry_max: int
    retry_base_delay: float
    rate_limit_rpm: float
    credential_check_interval: int
    fetch_timeout_ms: int

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            retry_max=max(1, env_int("RETRY_MAX", 3)),
            retry_base_delay=max(0.1, env_float("RETRY_BASE_DELAY", 1.0)),
            rate_limit_rpm=max(0.0, env_float("RATE_LIMIT_RPM", 30.0)),
            credential_check_interval=max(0, env_int("CREDENTIAL_CHECK_INTERVAL", 3600)),
            fetch_timeout_ms=max(30_000, env_int("FETCH_TIMEOUT_MS", 180_000)),
        )


@dataclass(frozen=True)
class DoubaoConfig:
    chat_url: str
    completion_url: str
    default_bot_id: str
    login_wait_sec: int

    @classmethod
    def from_env(cls) -> "DoubaoConfig":
        return cls(
            chat_url=env_str("DOUBAO_CHAT_URL", "https://www.doubao.com/chat/"),
            completion_url=env_str(
                "DOUBAO_COMPLETION_URL", "https://www.doubao.com/chat/completion"
            ),
            default_bot_id=env_str("DOUBAO_BOT_ID", "7338286299411103781"),
            login_wait_sec=max(60, env_int("DOUBAO_LOGIN_WAIT_SEC", 180)),
        )


@dataclass(frozen=True)
class DeepSeekConfig:
    chat_url: str
    api_base: str
    debug_port: int
    session_ttl_sec: int
    reuse_session: bool
    login_wait_sec: int
    wasm_url: str

    @classmethod
    def from_env(cls) -> "DeepSeekConfig":
        return cls(
            chat_url=env_str("DEEPSEEK_CHAT_URL", "https://chat.deepseek.com/"),
            api_base=env_str("DEEPSEEK_API_BASE", "https://chat.deepseek.com/api/v0"),
            debug_port=max(1024, env_int("DEEPSEEK_DEBUG_PORT", 9333)),
            session_ttl_sec=max(60, env_int("DEEPSEEK_SESSION_TTL_SEC", 300)),
            reuse_session=env_bool("DEEPSEEK_REUSE_SESSION", True),
            login_wait_sec=max(60, env_int("DEEPSEEK_LOGIN_WAIT_SEC", 300)),
            wasm_url=env_str(
                "DEEPSEEK_POW_WASM_URL",
                "https://fe-static.deepseek.com/chat/static/sha3_wasm_bg.7b9ca65ddd.wasm",
            ),
        )


@dataclass(frozen=True)
class ChatGPTConfig:
    chat_url: str
    default_model: str
    login_wait_sec: int
    headless: bool
    max_prompt_chars: int
    max_project_context_chars: int

    @classmethod
    def from_env(cls) -> "ChatGPTConfig":
        return cls(
            chat_url=env_str("CHATGPT_CHAT_URL", "https://chatgpt.com/"),
            default_model=env_str("CHATGPT_MODEL", "gpt-4o"),
            login_wait_sec=max(60, env_int("CHATGPT_LOGIN_WAIT_SEC", 300)),
            headless=env_bool("CHATGPT_HEADLESS", False),
            max_prompt_chars=max(2_000, env_int("CHATGPT_MAX_PROMPT_CHARS", 8_000)),
            max_project_context_chars=max(
                1_000, env_int("CHATGPT_MAX_PROJECT_CONTEXT_CHARS", 4_000)
            ),
        )


SERVER = ServerConfig.from_env()
CONTEXT = ContextConfig.from_env()
APP = AppConfig.from_env()
DOUBAO = DoubaoConfig.from_env()
DEEPSEEK = DeepSeekConfig.from_env()
CHATGPT = ChatGPTConfig.from_env()

# 浏览器通用（一般无需修改）
BROWSER_VIEWPORT = {"width": 1280, "height": 800}
BROWSER_LOCALE = "zh-CN"
BROWSER_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
