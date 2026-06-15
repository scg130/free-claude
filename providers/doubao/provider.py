from providers.base import ChatProvider, ChatResult
from providers.doubao import browser


class DoubaoProvider(ChatProvider):
    id = "doubao"
    display_name = "豆包"
    model_aliases = ("doubao-claude", "doubao-pro")

    async def startup(self, *, refresh_credentials: bool = True) -> None:
        if refresh_credentials:
            browser.clear_session()
            await browser.refresh_credentials()
        elif not browser.session_ready():
            await browser.refresh_credentials()
        elif not await browser.validate_session():
            print("[doubao] 凭证失效，自动刷新…")
            await browser.refresh_credentials()

    async def shutdown(self) -> None:
        await browser.shutdown()

    async def chat(self, prompt: str, conv_id: str) -> ChatResult:
        text = await browser.chat_completion(prompt, conv_id)
        return ChatResult.from_text(text)

    async def check_health(self) -> dict:
        ready = browser.session_ready()
        valid = await browser.validate_session() if ready else False
        return {"session_ready": ready, "session_valid": valid}
