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
        await provider.startup(refresh_credentials=refresh_credentials)


async def shutdown_all() -> None:
    for provider in _providers.values():
        await provider.shutdown()


def _load_providers() -> None:
    from providers.doubao.provider import DoubaoProvider

    register_provider(DoubaoProvider())


_load_providers()
