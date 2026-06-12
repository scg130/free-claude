import json
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from providers import get_provider, list_models, shutdown_all, startup_all


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[startup] 初始化 AI 提供商…")
    try:
        await startup_all(refresh_credentials=True)
        print(f"[startup] 已就绪: {', '.join(list_models())}")
    except Exception as e:
        print(f"[startup] 凭证获取失败: {e}")
        print("[startup] 服务仍将启动，首次请求时会重试")
    yield
    print("[shutdown] 关闭提供商资源…")
    try:
        await shutdown_all()
    except Exception as e:
        print(f"[shutdown] 关闭异常（可忽略）: {e}")


app = FastAPI(title="AI -> Claude Code 中转 API", lifespan=lifespan)


def get_conv_id() -> str:
    return f"conv_{int(time.time() * 1000)}"


class ChatRequest(BaseModel):
    model: str = "doubao-claude"
    messages: list[dict]
    stream: bool = False
    temperature: float = 0.7


@app.get("/v1/models")
async def list_available_models():
    return {
        "object": "list",
        "data": [
            {"id": model, "object": "model", "owned_by": model.split("-")[0]}
            for model in list_models()
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    user_prompt = ""
    for msg in reversed(req.messages):
        if msg["role"] == "user":
            user_prompt = msg["content"]
            break
    if not user_prompt:
        raise HTTPException(status_code=400, detail="未检测到用户提问")

    try:
        provider = get_provider(req.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    conv_id = get_conv_id()
    try:
        result = await provider.chat(user_prompt, conv_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    if not result.content:
        raise HTTPException(status_code=502, detail=f"{provider.display_name} 未返回内容")

    if req.stream:
        async def stream_generator():
            for chunk in result.chunks:
                sse_data = {
                    "choices": [{"delta": {"content": chunk}, "finish_reason": None}]
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
                "message": {"role": "assistant", "content": result.content},
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
