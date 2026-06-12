"""兼容旧导入，请改用 providers.registry.get_provider。"""

from providers import get_provider
from providers.base import ChatResult


async def send_prompt_to_doubao(prompt: str, conv_id: str) -> ChatResult:
    return await get_provider("doubao").chat(prompt, conv_id)
