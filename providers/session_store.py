"""凭证文件读写：原子写入 + 备份恢复。"""

import json
import shutil
from pathlib import Path
from typing import Any


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.backup_path = path.with_suffix(path.suffix + ".bak")

    def load(self) -> dict[str, Any]:
        for candidate in (self.path, self.backup_path):
            if not candidate.exists():
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    if candidate is self.backup_path and not self.path.exists():
                        print(f"[session] 从备份恢复: {self.backup_path.name}")
                        self.save(data)
                    return data
            except (json.JSONDecodeError, OSError) as exc:
                print(f"[session] {candidate.name} 损坏: {exc}")
        return {}

    def save(self, data: dict[str, Any]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                shutil.copy2(self.path, self.backup_path)
            except OSError:
                pass
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)
        return data

    def clear(self) -> None:
        for p in (self.path, self.backup_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
