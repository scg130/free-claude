from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import asyncio
import time
from threading import Thread
from mitm_addon import cache
from ws_bridge import send_prompt_to_doubao

app = FastAPI(title="Doubao -> Claude Code 中转API")

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
    # 异步发送提问（不阻塞接口）
    Thread(target=lambda: asyncio.run(send_prompt_to_doubao(user_prompt, conv_id))).start()

    # 等待流式数据就绪
    timeout = 0
    while cache.is_streaming and len(cache.stream_chunks) == 0 and timeout < 20:
        await asyncio.sleep(0.2)
        timeout += 1

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)