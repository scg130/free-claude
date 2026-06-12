import asyncio
import websockets
import urllib.parse
import json
import time

PARAM_FILE = "doubao_ws_params.json"
WSS_BASE = "wss://wss100-normal.doubao.com/ws/v2"

def load_ws_params() -> dict:
    """加载抓包得到的签名/设备参数"""
    with open(PARAM_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def build_wss_url() -> str:
    params = load_ws_params()
    return f"{WSS_BASE}?{urllib.parse.urlencode(params)}"

def build_send_frame(prompt: str, conv_id: str) -> bytes:
    """构造豆包标准WS请求帧（4字节长度头 + JSON）"""
    payload = {
        "cmd": 50001,
        "local_conversation_id": conv_id,
        "content": prompt,
        "create_time": int(time.time() * 1000),
        "msg_id": str(int(time.time() * 1000)),
        "msg_type": 1
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = len(body).to_bytes(4, byteorder="big")
    return header + body

async def send_prompt_to_doubao(prompt: str, conv_id: str):
    """主动发送提问到豆包WS服务"""
    url = build_wss_url()
    headers = {
        "Origin": "https://www.doubao.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        async with websockets.connect(url, extra_headers=headers, ping_interval=20) as ws:
            frame = build_send_frame(prompt, conv_id)
            await ws.send(frame)
            # 保持连接直到流式输出完成
            while True:
                await asyncio.sleep(0.5)
    except Exception as e:
        print(f"WS连接异常: {e}")