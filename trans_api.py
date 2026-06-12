import asyncio
import json
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from doubao_browser import PARAM_FILE, refresh_credentials, shutdown as browser_shutdown
from mitm_addon import cache
from ws_bridge import send_prompt_to_doubao


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[startup] 删除旧凭证并重新登录豆包…")
    PARAM_FILE.unlink(missing_ok=True)
    try:
        await refresh_credentials()
        print(f"[startup] 已写入 {PARAM_FILE}")
    except Exception as e:
        print(f"[startup] 凭证获取失败: {e}")
        print("[startup] 服务仍将启动，首次请求时会重试获取")
    yield
    print("[shutdown] 正在关闭浏览器…")
    try:
        await browser_shutdown()
    except Exception as e:
        print(f"[shutdown] 浏览器关闭异常（可忽略）: {e}")


app = FastAPI(title="Doubao -> Claude Code 中转API", lifespan=lifespan)

# 模拟会话ID池
def get_conv_id() -> str:
    return f"conv_{int(time.time() * 1000)}"

# OpenAI 接口请求模型
class ChatRequest(BaseModel):
    model: str = "doubao-claude"
    messages: list[dict]
    stream: bool = False
    temperature: float = 0.7

@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    # 提取用户最后一轮提问
    user_prompt = ""
    for msg in reversed(req.messages):
        if msg["role"] == "user":
            user_prompt = msg["content"]
            break
    if not user_prompt:
        raise HTTPException(status_code=400, detail="未检测到用户提问")

    conv_id = get_conv_id()
    await send_prompt_to_doubao(user_prompt, conv_id)

    if cache.last_error:
        raise HTTPException(status_code=502, detail=cache.last_error)
    if not cache.model_reply:
        raise HTTPException(
            status_code=502,
            detail="豆包未返回内容，请重新运行 python doubao_auth.py 登录",
        )

    # 场景1：流式返回（Claude Code 优先使用流式）
    if req.stream:
        async def stream_generator():
            # 按照OpenAI SSE格式返回片段
            for chunk in cache.stream_chunks:
                sse_data = {
                    "choices": [{"delta": {"content": chunk}, "finish_reason": None}]
                }
                yield f"data: {json.dumps(sse_data, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.05)
            # 结束标记
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream"
        )

    # 场景2：非流式返回
    return {
        "id": f"chat-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "doubao-claude",
        "choices": [
            {
                "message": {"role": "assistant", "content": cache.model_reply},
                "finish_reason": "stop"
            }
        ]
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
            "  cd free-claude && source venv/bin/activate\n"
            "  pip install -r requirements.txt && playwright install chromium\n"
            f"  python trans_api.py\n"
            f"或直接: {venv_py} trans_api.py"
        )


if __name__ == "__main__":
    _check_deps()
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)