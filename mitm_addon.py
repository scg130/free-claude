import inspect
import json
from urllib.parse import parse_qs, urlparse

from mitmproxy import ctx, tls
from mitmproxy.net import tls as net_tls
from OpenSSL import SSL

from paths import ensure_provider_dir, provider_param_file

PARAM_FILE = provider_param_file("doubao", "ws_params.json")

# 全局缓存
class GlobalCache:
    ws_params = {}
    user_prompt = ""
    model_reply = ""
    stream_chunks = []
    is_streaming = False
    conv_id = ""
    last_error = ""

cache = GlobalCache()

# 方式 B：对指定域名关闭上游证书校验（mitmproxy 11 API）
PASSTHROUGH_HOSTS = ("doubao.com", "kugou.com")


def _match_passthrough_host(data: tls.TlsData) -> bool:
    host = data.conn.address[0] if data.conn.address else ""
    sni = getattr(data.conn, "sni", None) or ""
    return any(h in host or h in sni for h in PASSTHROUGH_HOSTS)


def _create_insecure_server_context():
    """兼容不同 mitmproxy 版本的 create_proxy_server_context 参数。"""
    kwargs = {
        "method": net_tls.Method.TLS_CLIENT_METHOD,
        "min_version": net_tls.Version[ctx.options.tls_version_server_min],
        "max_version": net_tls.Version[ctx.options.tls_version_server_max],
        "cipher_list": None,
        "ecdh_curve": getattr(ctx.options, "tls_ecdh_curve_server", None),
        "verify": net_tls.Verify.VERIFY_NONE,
        "ca_path": getattr(ctx.options, "ssl_verify_upstream_trusted_confdir", None),
        "ca_pemfile": getattr(ctx.options, "ssl_verify_upstream_trusted_ca", None),
        "client_cert": None,
        "legacy_server_connect": True,
    }
    params = inspect.signature(net_tls.create_proxy_server_context).parameters
    return net_tls.create_proxy_server_context(
        **{k: v for k, v in kwargs.items() if k in params}
    )


class DontVerify:
    """对豆包、酷狗等域名跳过上游 TLS 证书校验（方式 B）。"""

    def tls_start_server(self, data: tls.TlsData) -> None:
        if data.ssl_conn is not None or not _match_passthrough_host(data):
            return

        try:
            ssl_ctx = _create_insecure_server_context()
        except Exception as e:
            ctx.log.warn(f"[DontVerify] 创建 SSL 上下文失败，回退内置处理: {e}")
            return

        data.ssl_conn = SSL.Connection(ssl_ctx)
        server = data.conn
        if server.sni:
            data.ssl_conn.set_tlsext_host_name(server.sni.encode("idna"))
        if server.alpn_offers:
            data.ssl_conn.set_alpn_protos(server.alpn_offers)
        data.ssl_conn.set_connect_state()


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
            ensure_provider_dir("doubao")
            with open(PARAM_FILE, "w", encoding="utf-8") as f:
                json.dump(cache.ws_params, f, ensure_ascii=False, indent=2)
            ctx.log.info("[✅ 已保存豆包WS连接参数]")

    # mitmproxy 11：websocket_message 仅接收 flow，消息在 flow.websocket.messages[-1]
    def websocket_message(self, flow):
        if flow.websocket is None or not flow.websocket.messages:
            return

        frame = flow.websocket.messages[-1]
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
            payload = parse_ws_frame(frame.content)
            content = payload.get("content", "")
            if content and cache.is_streaming:
                cache.stream_chunks.append(content)
                cache.model_reply += content
            if payload.get("is_end", False):
                cache.is_streaming = False
                ctx.log.info("[✅ 回答结束]")

    def websocket_end(self, flow):
        if flow.websocket is None:
            return
        code = flow.websocket.close_code
        if code and code not in (1000, 1001, 1005):
            ctx.log.warn(
                f"[⚠️ WS异常断开] code={code} reason={flow.websocket.close_reason}"
            )


addons = [DontVerify(), Addon()]
