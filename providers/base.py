from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ChatResult:
    content: str
    chunks: list[str] = field(default_factory=list)


class ChatProvider(ABC):
    """各 AI 平台统一接口（豆包 / DeepSeek / ChatGPT …）。"""

    id: str
    display_name: str
    model_aliases: tuple[str, ...] = ()

    def matches_model(self, model: str) -> bool:
        if model in self.model_aliases:
            return True
        return model == self.id or model.startswith(f"{self.id}-")

    @abstractmethod
    async def startup(self, *, refresh_credentials: bool = True) -> None:
        """服务启动：登录、刷新凭证等。"""

    @abstractmethod
    async def shutdown(self) -> None:
        """服务退出：释放浏览器等资源。"""

    @abstractmethod
    async def chat(self, prompt: str, conv_id: str) -> ChatResult:
        """发送用户消息并返回完整回复。"""
