"""豆包凭证刷新 CLI（兼容入口）。"""

import asyncio

from providers import get_provider
from providers.doubao.browser import PARAM_FILE

if __name__ == "__main__":
    provider = get_provider("doubao")
    asyncio.run(provider.startup(refresh_credentials=True))
    print(f"[doubao_auth] 已保存 {PARAM_FILE}")
