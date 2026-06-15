import json
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from project_context import maybe_project_context

from providers import get_provider, list_models, shutdown_all, startup_all
from providers.doubao.browser import session_ready as doubao_session_ready
from providers.deepseek.browser import session_ready as deepseek_session_ready


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[startup] 初始化 AI 提供商…")
    await startup_all(
        refresh_credentials=not (doubao_session_ready() or deepseek_session_ready())
    )
    print(f"[startup] 已注册模型: {', '.join(list_models())}")
    yield
    print("[shutdown] 关闭提供商资源…")
    try:
        await shutdown_all()
    except Exception as e:
        print(f"[shutdown] 关闭异常（可忽略）: {e}")


app = FastAPI(title="AI -> Claude Code 中转 API", lifespan=lifespan)


def get_conv_id() -> str:
    return f"conv_{int(time.time() * 1000)}"


def _extract_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if parts:
                return "".join(parts)
    return ""


def _resolve_model(model: str) -> str:
    try:
        get_provider(model)
        return model
    except ValueError:
        return "doubao-claude"


async def _run_chat(
    model: str,
    messages: list[dict],
    *,
    system: Any = None,
    tools: list[dict] | None = None,
) -> tuple[Any, str]:
    provider = get_provider(_resolve_model(model))
    conv_id = get_conv_id()
    project_context = maybe_project_context(messages, system, tools)

    if tools:
        result = await provider.chat_agent(
            messages,
            conv_id,
            system=system,
            tools=tools,
            model=model,
            project_context=project_context,
        )
    else:
        user_prompt = _extract_user_text(messages)
        if not user_prompt:
            raise HTTPException(status_code=400, detail="未检测到用户提问")
        if project_context:
            user_prompt = f"{project_context}\n\n## 用户问题\n{user_prompt}"
        result = await provider.chat(user_prompt, conv_id)

    if not result.content and not result.content_blocks:
        raise HTTPException(status_code=502, detail=f"{provider.display_name} 未返回内容")
    return result, provider.display_name


class ChatRequest(BaseModel):
    model: str = "doubao-claude"
    messages: list[dict]
    stream: bool = False
    temperature: float = 0.7


class MessagesRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096
    messages: list[dict]
    stream: bool = False
    system: str | list | None = None
    tools: list[dict] | None = Field(default=None)


@app.api_route("/", methods=["GET", "HEAD"])
async def health():
    return JSONResponse({"status": "ok", "service": "free-claude"})


@app.get("/v1/models")
async def list_available_models():
    return {
        "object": "list",
        "data": [
            {"id": model, "object": "model", "owned_by": model.split("-")[0]}
            for model in list_models()
        ],
    }


def _anthropic_message_dict(
    msg_id: str, model: str, content_blocks: list[dict], stop_reason: str | None
) -> dict:
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def _content_blocks_from_result(result) -> tuple[list[dict], str]:
    if result.content_blocks:
        return result.content_blocks, result.stop_reason
    return [{"type": "text", "text": result.content}], result.stop_reason


@app.post("/v1/messages")
async def anthropic_messages(req: MessagesRequest):
    """Claude Code Anthropic Messages API（支持 tools → tool_use）。"""
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages 不能为空")

    try:
        result, _ = await _run_chat(
            req.model, req.messages, system=req.system, tools=req.tools
        )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    msg_id = f"msg_{int(time.time())}"
    blocks, stop_reason = _content_blocks_from_result(result)

    if req.stream:
        async def anthropic_stream():
            message = _anthropic_message_dict(msg_id, req.model, [], None)
            yield (
                "event: message_start\n"
                f"data: {json.dumps({'type': 'message_start', 'message': message}, ensure_ascii=False)}\n\n"
            )
            for idx, block in enumerate(blocks):
                start_block = block
                if block.get("type") == "tool_use":
                    start_block = {
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": {},
                    }
                yield (
                    "event: content_block_start\n"
                    f"data: {json.dumps({'type': 'content_block_start', 'index': idx, 'content_block': start_block}, ensure_ascii=False)}\n\n"
                )
                if block.get("type") == "text":
                    yield (
                        "event: content_block_delta\n"
                        f"data: {json.dumps({'type': 'content_block_delta', 'index': idx, 'delta': {'type': 'text_delta', 'text': block.get('text', '')}}, ensure_ascii=False)}\n\n"
                    )
                elif block.get("type") == "tool_use":
                    inp = json.dumps(block.get("input", {}), ensure_ascii=False)
                    yield (
                        "event: content_block_delta\n"
                        f"data: {json.dumps({'type': 'content_block_delta', 'index': idx, 'delta': {'type': 'input_json_delta', 'partial_json': inp}}, ensure_ascii=False)}\n\n"
                    )
                yield (
                    "event: content_block_stop\n"
                    f"data: {json.dumps({'type': 'content_block_stop', 'index': idx}, ensure_ascii=False)}\n\n"
                )
            yield (
                "event: message_delta\n"
                f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': {'output_tokens': 0}}, ensure_ascii=False)}\n\n"
            )
            yield (
                "event: message_stop\n"
                f"data: {json.dumps({'type': 'message_stop'}, ensure_ascii=False)}\n\n"
            )

        return StreamingResponse(anthropic_stream(), media_type="text/event-stream")

    return _anthropic_message_dict(msg_id, req.model, blocks, stop_reason)


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    user_prompt = _extract_user_text(req.messages)
    if not user_prompt:
        raise HTTPException(status_code=400, detail="未检测到用户提问")

    try:
        get_provider(req.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    try:
        result, _ = await _run_chat(req.model, req.messages)
        content = result.content
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    if req.stream:
        async def stream_generator():
            sse_data = {
                "choices": [{"delta": {"content": content}, "finish_reason": None}]
            }
            yield f"data: {json.dumps(sse_data, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream_generator(), media_type="text/event-stream")

    return {
        "id": f"chat-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def _check_deps() -> None:
    try:
        import playwright  # noqa: F401
    except ImportError:
        import sys
        from pathlib import Path

        venv_py = Path(__file__).resolve().parent / "venv" / "bin" / "python"
        sys.exit(
            "缺少 playwright。请用项目虚拟环境启动:\n"
            "  source venv/bin/activate && pip install -r requirements.txt\n"
            f"  或: {venv_py} trans_api.py"
        )


if __name__ == "__main__":
    _check_deps()
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
