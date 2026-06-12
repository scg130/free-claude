# free-claude

多 AI 平台 OpenAI 兼容中转 API（当前支持豆包，可扩展 DeepSeek / ChatGPT 等）。

## 架构

```
providers/
  base.py          # ChatProvider 抽象接口
  registry.py      # 注册与路由（按 model 名选择提供商）
  doubao/          # 豆包实现
    browser.py     # Playwright + SSE
    provider.py
  deepseek/        # 后续扩展
  openai/          # 后续扩展
params/
  doubao/session.json    # 豆包凭证（自动生成）
.profiles/
  doubao/                # 豆包浏览器登录态
```

新增平台：实现 `ChatProvider`，在 `registry.py` 中 `register_provider` 即可。

## 快速开始

```bash
chmod +x run.sh && ./run.sh
```

测试：

```bash
curl http://127.0.0.1:8000/v1/models

curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-claude","messages":[{"role":"user","content":"1+1="}],"stream":false}'
```

手动刷新豆包凭证：

```bash
python doubao_auth.py
```

## 接入 Claude Code

Claude Code 走 **Anthropic API**，不是 OpenAI：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_API_KEY=sk-any
claude -p "你好豆包 写一个快速排序到test.py"
```

- 用 `ANTHROPIC_*`，不要用 `OPENAI_*`
- `ANTHROPIC_BASE_URL` **不要加** `/v1`
- `ANTHROPIC_API_KEY` 任意非空字符串即可（本地不校验），用于跳过 `Not logged in · Please run /login`
- 修改代码后需 **重启** `./run.sh`；可用 `curl -I http://127.0.0.1:8000/` 确认服务已更新（应返回 200）

## 备选：mitmproxy 抓包

```bash
mitmweb -s mitm_addon.py -p 8080 --ssl-insecure
```

WS 参数保存至 `params/doubao/ws_params.json`。

## 常见问题

| 现象 | 处理 |
|------|------|
| 弹出浏览器要求登录 | 首次正常，登录后自动继续 |
| `未知模型` | 查看 `GET /v1/models` 支持的 model 列表 |
| API 返回 502 | 运行 `python doubao_auth.py` 重新登录 |
| `Not logged in · Please run /login` | 设置 `ANTHROPIC_API_KEY=sk-any`，不要用 `OPENAI_API_KEY` |
