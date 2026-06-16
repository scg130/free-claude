#!/usr/bin/env bash
# free-claude 启动脚本：按需安装依赖 + 浏览器 profile 清理 + 启动 API
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
CACHE_DIR="$ROOT/.cache"
DEPS_MARKER="$CACHE_DIR/playwright-system-deps.ok"
REINSTALL_SYSTEM_DEPS=0

PY="$ROOT/venv/bin/python"
PIP="$ROOT/venv/bin/pip"
PW="$ROOT/venv/bin/playwright"

mkdir -p "$CACHE_DIR"

load_env_file() {
  local f="$ROOT/.env"
  if [[ -f "$f" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "$f"
    set +a
    echo "[run.sh] 已加载 .env"
  else
    echo "[run.sh] 未找到 .env，使用 .env.example 默认值（可执行: cp .env.example .env）"
  fi
  API_PORT="${API_PORT:-8000}"
  DEEPSEEK_DEBUG_PORT="${DEEPSEEK_DEBUG_PORT:-9333}"
}

print_config_summary() {
  echo "[run.sh] API=${API_HOST:-127.0.0.1}:${API_PORT}"
  echo "[run.sh] 上下文 CONTEXT=${CONTEXT:-1} MODE=${CONTEXT_MODE:-lite}"
  echo "[run.sh] 可靠性 CHECK=${CREDENTIAL_CHECK_INTERVAL:-3600}s RETRY=${RETRY_MAX:-3} RATE=${RATE_LIMIT_RPM:-30}/min"
}

is_wsl() {
  [[ -n "${WSL_DISTRO_NAME:-}" ]] && return 0
  [[ -f /proc/version ]] && grep -qi microsoft /proc/version
}

is_linux() {
  [[ "$(uname -s)" == "Linux" ]]
}

playwright_browser_cache() {
  if [[ -n "${PLAYWRIGHT_BROWSERS_PATH:-}" ]]; then
    echo "$PLAYWRIGHT_BROWSERS_PATH"
  elif [[ "$(uname -s)" == "Darwin" ]]; then
    echo "$HOME/Library/Caches/ms-playwright"
  else
    echo "$HOME/.cache/ms-playwright"
  fi
}

port_open() {
  local port=$1
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -q ":${port} "
    return
  fi
  if command -v nc >/dev/null 2>&1; then
    nc -z 127.0.0.1 "$port" >/dev/null 2>&1
    return
  fi
  "$PY" -c "import socket; s=socket.socket(); s.settimeout(0.5); import sys; sys.exit(0 if s.connect_ex(('127.0.0.1', $port))==0 else 1)" 2>/dev/null
}

profile_process_alive() {
  local profile=$1
  if ! command -v pgrep >/dev/null 2>&1; then
    return 1
  fi
  pgrep -f "$profile" >/dev/null 2>&1
}

python_deps_ok() {
  "$PY" - <<'PY'
import importlib.util
required = (
    "playwright", "fastapi", "uvicorn", "httpx", "wasmtime",
    "websockets", "mitmproxy", "pydantic", "multipart",
)
missing = [m for m in required if importlib.util.find_spec(m) is None]
raise SystemExit(1 if missing else 0)
PY
}

chromium_ok() {
  local cache
  cache="$(playwright_browser_cache)"
  compgen -G "$cache/chromium-"* >/dev/null 2>&1
}

playwright_system_deps_ok() {
  [[ -f "$DEPS_MARKER" ]]
}

clear_system_deps_marker() {
  if [[ -f "$DEPS_MARKER" ]]; then
    rm -f "$DEPS_MARKER"
    echo "[run.sh] 已清除系统依赖标记: $DEPS_MARKER"
  fi
}

usage() {
  cat <<EOF
用法: ./run.sh [选项]

  默认: 依赖齐全则跳过安装，清理浏览器锁后启动 API

选项:
  --reinstall-system-deps, --fresh-deps
                        清除 .cache/playwright-system-deps.ok 并重新安装
                        Chromium 系统依赖 (Linux/WSL)
  -h, --help            显示此帮助
EOF
}

parse_args() {
  for arg in "$@"; do
    case "$arg" in
      --reinstall-system-deps|--fresh-deps)
        REINSTALL_SYSTEM_DEPS=1
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "[run.sh] 未知选项: $arg (使用 --help 查看)" >&2
        exit 1
        ;;
    esac
  done
}

ensure_venv() {
  if [[ -x "$PY" ]]; then
    return
  fi
  echo "[run.sh] 创建虚拟环境 venv…"
  python3 -m venv "$ROOT/venv"
}

ensure_python_deps() {
  if python_deps_ok; then
    return
  fi
  echo "[run.sh] 安装 Python 依赖 (requirements.txt)…"
  "$PIP" install -r "$ROOT/requirements.txt"
  if ! python_deps_ok; then
    echo "[run.sh] 错误: Python 依赖安装后仍不完整" >&2
    exit 1
  fi
}

ensure_playwright_browser() {
  if chromium_ok; then
    return
  fi
  echo "[run.sh] 安装 Playwright Chromium…"
  "$PW" install chromium
  # 新装 Chromium 后重新检查系统依赖
  clear_system_deps_marker
}

ensure_playwright_system_deps() {
  if ! is_linux; then
    return
  fi
  if [[ "$REINSTALL_SYSTEM_DEPS" == "1" ]]; then
    clear_system_deps_marker
    echo "[run.sh] 强制重装 Chromium 系统依赖…"
  fi
  if playwright_system_deps_ok; then
    return
  fi
  echo "[run.sh] 安装 Chromium 系统依赖 (Linux/WSL)…"
  if "$PW" install-deps chromium; then
    touch "$DEPS_MARKER"
  else
    echo "[run.sh] 警告: install-deps 失败，可手动执行后重试:"
    echo "  sudo $PW install-deps chromium"
    echo "  或: ./run.sh --reinstall-system-deps"
  fi
}

clear_profile_locks() {
  local profile_dir=$1
  [[ -d "$profile_dir" ]] || return 0

  shopt -s nullglob
  local locks=(
    "$profile_dir"/SingletonLock
    "$profile_dir"/SingletonSocket
    "$profile_dir"/SingletonCookie
    "$profile_dir"/Default/SingletonLock
    "$profile_dir"/Default/SingletonSocket
    "$profile_dir"/Default/SingletonCookie
  )
  shopt -u nullglob

  local removed=0
  for lock in "${locks[@]}"; do
    if [[ -e "$lock" ]]; then
      rm -f "$lock" && removed=1
    fi
  done
  if [[ "$removed" -eq 1 ]]; then
    echo "[run.sh] 已清除残留锁: $profile_dir"
  fi
}

cleanup_browser_profile() {
  local provider=$1
  local profile="$ROOT/.profiles/$provider"
  local debug_port=${2:-}

  [[ -d "$profile" ]] || return 0

  local has_live=0
  if profile_process_alive "$profile"; then
    has_live=1
  fi
  if [[ -n "$debug_port" ]] && port_open "$debug_port"; then
    has_live=1
  fi

  if [[ "$has_live" -eq 1 ]]; then
    return 0
  fi

  if command -v pgrep >/dev/null 2>&1 && pgrep -f "$profile" >/dev/null 2>&1; then
    echo "[run.sh] 停止残留 $provider 浏览器进程…"
    pkill -f "$profile" 2>/dev/null || true
    sleep 1
  fi

  clear_profile_locks "$profile"
}

cleanup_stale_browsers() {
  cleanup_browser_profile "deepseek" "$DEEPSEEK_DEBUG_PORT"
  cleanup_browser_profile "doubao"
  cleanup_browser_profile "chatgpt"
}

stop_old_api() {
  local pids
  pids=$(lsof -ti :"${API_PORT}" 2>/dev/null || true)
  if [[ -n "$pids" ]]; then
    echo "[run.sh] 停止占用 ${API_PORT} 端口的旧进程: $pids"
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 1
  fi
}

setup_wsl_display_hint() {
  if is_wsl && [[ -z "${DISPLAY:-}" ]] && [[ ! -f "$ROOT/params/deepseek/session.json" ]]; then
    echo "[run.sh] WSL 提示: 首次登录 DeepSeek 需要图形界面，可执行 export DISPLAY=:0"
    echo "[run.sh] 或在 Windows 登录后复制 params/deepseek/session.json 到 WSL"
  fi
}

main() {
  parse_args "$@"
  load_env_file
  print_config_summary
  ensure_venv
  ensure_python_deps
  ensure_playwright_browser
  ensure_playwright_system_deps
  cleanup_stale_browsers
  stop_old_api
  setup_wsl_display_hint

  echo "[run.sh] 启动 trans_api (http://${API_HOST:-127.0.0.1}:${API_PORT})…"
  exec "$PY" "$ROOT/trans_api.py"
}

main "$@"
