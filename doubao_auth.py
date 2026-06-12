"""兼容入口：凭证刷新已迁移至 doubao_browser.py。"""

import asyncio

from doubao_browser import PARAM_FILE, refresh_credentials, refresh_ws_params

__all__ = ["PARAM_FILE", "refresh_credentials", "refresh_ws_params"]

if __name__ == "__main__":
    result = asyncio.run(refresh_credentials())
    print(f"[doubao_auth] 已保存 {PARAM_FILE}，sessionid={result['sessionid'][:8]}…")
