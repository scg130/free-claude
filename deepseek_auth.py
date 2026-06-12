"""DeepSeek 凭证刷新 CLI。"""

import asyncio

from providers import get_provider
from providers.deepseek.browser import PARAM_FILE

if __name__ == "__main__":
    provider = get_provider("deepseek-chat")
    asyncio.run(provider.startup(refresh_credentials=True))
    print(f"[deepseek_auth] 已保存 {PARAM_FILE}")
