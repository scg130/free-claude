from providers.base import ChatProvider

_providers: dict[str, ChatProvider] = {}


def register_provider(provider: ChatProvider) -> None:
    _providers[provider.id] = provider


def get_provider(model: str) -> ChatProvider:
    for provider in _providers.values():
        if provider.matches_model(model):
            return provider
    available = ", ".join(list_models()) or "（无）"
    raise ValueError(f"未知模型: {model}，可用: {available}")


def list_providers() -> list[ChatProvider]:
    return list(_providers.values())


def list_models() -> list[str]:
    models: list[str] = []
    for provider in _providers.values():
        models.append(provider.id)
        models.extend(provider.model_aliases)
    return models


async def startup_all(*, refresh_credentials: bool = True) -> None:
    for provider in _providers.values():
        try:
            need_refresh = refresh_credentials
            if refresh_credentials:
                if provider.id == "doubao":
                    from providers.doubao import browser as b

                    need_refresh = not b.session_ready()
                elif provider.id == "deepseek":
                    from providers.deepseek import browser as b

                    need_refresh = not b.session_ready()
                elif provider.id == "chatgpt":
                    from providers.chatgpt import browser as b

                    need_refresh = not b.session_ready()
            await provider.startup(refresh_credentials=need_refresh)
        except Exception as e:
            print(f"[startup] {provider.id} 跳过: {e}")


async def shutdown_all() -> None:
    for provider in _providers.values():
        await provider.shutdown()


async def providers_health() -> dict:
    result: dict = {}
    for provider in _providers.values():
        try:
            result[provider.id] = {
                "display_name": provider.display_name,
                **await provider.check_health(),
            }
        except Exception as exc:
            result[provider.id] = {
                "display_name": provider.display_name,
                "session_ready": False,
                "session_valid": False,
                "error": str(exc),
            }
    return result


def _load_providers() -> None:
    from providers.chatgpt.provider import ChatGPTProvider
    from providers.deepseek.provider import DeepSeekProvider
    from providers.doubao.provider import DoubaoProvider

    register_provider(DoubaoProvider())
    register_provider(DeepSeekProvider())
    register_provider(ChatGPTProvider())


_load_providers()
