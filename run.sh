#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

if [[ ! -x "$ROOT/venv/bin/python" ]]; then
  echo "未找到 venv，请先: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

"$ROOT/venv/bin/python" -c "import playwright" 2>/dev/null || {
  "$ROOT/venv/bin/pip" install -r requirements.txt
  "$ROOT/venv/bin/playwright" install chromium
}

exec "$ROOT/venv/bin/python" trans_api.py
