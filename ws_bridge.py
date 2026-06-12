from mitm_addon import cache
from doubao_browser import chat_completion, refresh_credentials, session_ready


async def ensure_credentials(force: bool = False) -> None:
    if not force and session_ready():
        return
    await refresh_credentials()


async def send_prompt_to_doubao(prompt: str, conv_id: str):
    """通过浏览器内 SSE 调用豆包，写入 cache。"""
    cache.last_error = ""
    cache.user_prompt = prompt
    cache.conv_id = conv_id
    cache.stream_chunks.clear()
    cache.model_reply = ""
    cache.is_streaming = True

    try:
        reply = await chat_completion(prompt, conv_id)
        cache.model_reply = reply
        cache.stream_chunks.append(reply)
    except Exception as e:
        cache.last_error = str(e)
        print(f"豆包请求异常: {e}")
    finally:
        cache.is_streaming = False
