from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ChatResult:
    content: str
    chunks: list[str] = field(default_factory=list)
    content_blocks: list[dict] = field(default_factory=list)
    stop_reason: str = "end_turn"

    @classmethod
    def from_text(cls, text: str) -> "ChatResult":
        return cls(content=text, chunks=[text] if text else [])

    @classmethod
    def from_blocks(cls, blocks: list[dict], stop_reason: str = "end_turn") -> "ChatResult":
        text = "".join(
            b.get("text", "") for b in blocks if b.get("type") == "text"
        )
        return cls(
            content=text,
            chunks=[text] if text else [],
            content_blocks=blocks,
            stop_reason=stop_reason,
        )


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

    async def chat_agent(
        self,
        messages: list[dict],
        conv_id: str,
        *,
        system: str | list | None = None,
        tools: list[dict] | None = None,
        model: str | None = None,
    ) -> ChatResult:
        """Agent 模式：支持 tools 时默认走 chat（子类可覆盖）。"""
        from providers.anthropic_bridge import build_agent_prompt, parse_agent_response, to_anthropic_content

        prompt = build_agent_prompt(messages, system, tools)
        raw = await self.chat(prompt, conv_id)
        if not tools:
            return raw
        agent = parse_agent_response(raw.content)
        blocks = to_anthropic_content(agent)
        return ChatResult.from_blocks(blocks, stop_reason=agent.stop_reason)
