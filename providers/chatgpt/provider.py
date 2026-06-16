from providers.base import ChatProvider, ChatResult
from providers.chatgpt import browser, client


class ChatGPTProvider(ChatProvider):
    id = "chatgpt"
    display_name = "ChatGPT"
    model_aliases = (
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        "o3-mini",
        "o4-mini",
    )

    async def startup(self, *, refresh_credentials: bool = True) -> None:
        if refresh_credentials:
            browser.clear_session()
            await browser.refresh_credentials()
        elif not browser.session_ready():
            await browser.refresh_credentials()
        elif not await browser.validate_session():
            print("[chatgpt] 凭证失效，自动刷新…")
            await browser.refresh_credentials()

    async def shutdown(self) -> None:
        await client.shutdown()

    async def chat(self, prompt: str, conv_id: str) -> ChatResult:
        return await client.chat_completion(prompt)

    async def chat_agent(
        self,
        messages: list[dict],
        conv_id: str,
        *,
        system: str | list | None = None,
        tools: list[dict] | None = None,
        model: str | None = None,
        project_context: str = "",
    ) -> ChatResult:
        return await client.chat_agent(
            messages,
            system=system,
            tools=tools,
            model=model,
            project_context=project_context,
        )

    async def check_health(self) -> dict:
        ready = client.session_ready()
        valid = await client.validate_session() if ready else False
        return {"session_ready": ready, "session_valid": valid}
