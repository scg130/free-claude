from mitmproxy import ctx
from urllib.parse import urlparse, parse_qs
import json

# 全局缓存
class GlobalCache:
    ws_params = {}
    user_prompt = ""
    model_reply = ""
    stream_chunks = []
    is_streaming = False
    conv_id = ""

cache = GlobalCache()
PARAM_FILE = "doubao_ws_params.json"


def parse_ws_frame(data: bytes) -> dict:
    if len(data) < 4:
        return {}
    body_len = int.from_bytes(data[:4], byteorder="big")
    try:
        body = data[4: 4 + body_len]
        return json.loads(body.decode("utf-8", errors="ignore"))
    except Exception:
        return {}


class Addon:
    def request(self, flow):
        url = flow.request.url
        ctx.log.info(f"[REQ] {url[:120]}...")

        # 模糊匹配豆包 WS 接口
        if "doubao.com" in url and "/ws/v2" in url:
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            cache.ws_params = {k: v[0] for k, v in qs.items()}
            with open(PARAM_FILE, "w", encoding="utf-8") as f:
                json.dump(cache.ws_params, f, ensure_ascii=False, indent=2)
            ctx.log.info("[✅ 已保存豆包WS连接参数]")

    # 适配新版 mitmproxy：websocket_message 仅接收 flow + frame
    def websocket_message(self, flow, frame):
        # frame.from_client = True 客户端发消息（提问）
        if frame.from_client:
            payload = parse_ws_frame(frame.content)
            if payload.get("cmd") == 50001:
                cache.user_prompt = payload.get("content", "")
                cache.conv_id = payload.get("local_conversation_id", "")
                cache.stream_chunks.clear()
                cache.model_reply = ""
                cache.is_streaming = True
                ctx.log.info(f"[📝 捕获提问] {cache.user_prompt}")
        else:
            # 服务端返回回答
            payload = parse_ws_frame(frame.content)
            content = payload.get("content", "")
            if content and cache.is_streaming:
                cache.stream_chunks.append(content)
                cache.model_reply += content
            if payload.get("is_end", False):
                cache.is_streaming = False
                ctx.log.info(f"[✅ 回答结束]")


addons = [Addon()]