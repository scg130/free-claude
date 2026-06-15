from providers.base import ChatProvider, ChatResult
from providers.deepseek import browser, client


class DeepSeekProvider(ChatProvider):
    id = "deepseek"
    display_name = "DeepSeek"
    model_aliases = (
        "deepseek-chat",
        "deepseek-reasoner",
        "deepseek-coder",
    )

    async def startup(self, *, refresh_credentials: bool = True) -> None:
        if refresh_credentials and browser.PARAM_FILE.exists():
            browser.PARAM_FILE.unlink()
        if refresh_credentials or not browser.session_ready():
            await browser.refresh_credentials()
        await browser.ensure_runtime_page()
        try:
            from providers.deepseek.pow import get_solver
            get_solver().warmup()
            print("[deepseek] PoW WASM 已预热")
        except Exception as e:
            print(f"[deepseek] PoW 预热跳过: {e}")

    async def shutdown(self) -> None:
        await browser.shutdown()

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
