from providers.base import ChatProvider, ChatResult
from providers.doubao import browser


class DoubaoProvider(ChatProvider):
    id = "doubao"
    display_name = "豆包"
    model_aliases = ("doubao-claude", "doubao-pro")

    async def startup(self, *, refresh_credentials: bool = True) -> None:
        if refresh_credentials and browser.PARAM_FILE.exists():
            browser.PARAM_FILE.unlink()
        if refresh_credentials or not browser.session_ready():
            await browser.refresh_credentials()

    async def shutdown(self) -> None:
        await browser.shutdown()

    async def chat(self, prompt: str, conv_id: str) -> ChatResult:
        text = await browser.chat_completion(prompt, conv_id)
        return ChatResult.from_text(text)
