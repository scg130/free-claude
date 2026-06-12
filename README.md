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
  deepseek/          # DeepSeek 网页 API（Playwright 登录 + PoW）
    browser.py
    client.py
    pow.py
    provider.py
params/
  doubao/session.json
  deepseek/session.json   # ds_session_id + authorization
.profiles/
  doubao/                # 豆包浏览器登录态
  deepseek/              # DeepSeek 浏览器登录态
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

## DeepSeek（推荐 Agent 写代码）

与豆包相同：**浏览器登录 chat.deepseek.com**，自动保存 session 凭证，通过网页 API 转发，**无需申请官方 API Key**。

1. 首次启动会弹出浏览器，登录 [chat.deepseek.com](https://chat.deepseek.com) 即可（凭证保存至 `params/deepseek/session.json`）。

```bash
./run.sh
# 或手动刷新登录态：
python deepseek_auth.py
```

2. 测试 DeepSeek（Anthropic Messages API，Claude Code 同款）：

```bash
curl -X POST http://127.0.0.1:8000/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","max_tokens":1024,"messages":[{"role":"user","content":"1+1=?"}]}'

curl -s -X POST http://127.0.0.1:8000/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model":"deepseek-coder",
    "max_tokens":4096,
    "tools":[{"name":"Write","description":"Write file","input_schema":{"type":"object","properties":{"file_path":{"type":"string"},"content":{"type":"string"}},"required":["file_path","content"]}}],
    "messages":[{"role":"user","content":"写一个快速排序到 test.py"}]
  }'  
```

3. 让 Claude Code 使用 DeepSeek：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_API_KEY=sk-any
claude --model deepseek-chat -p "写一个快速排序到 test.py"
```

| 对比 | 豆包 | DeepSeek |
|------|------|----------|
| 凭证 | 浏览器登录 | 浏览器登录 |
| 转发方式 | Playwright + SSE | Playwright + 网页 API + PoW |
| Agent tools | prompt 模拟 JSON | prompt 模拟 JSON |
| 需要 API Key | 否 | 否 |

## 接入 Claude Code（豆包）

### 纯聊天

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_API_KEY=sk-any
claude -p "解释一下快速排序"
```

### Agent 写代码（实验性，已实现 Tool 桥接）

Claude Code 写文件依赖 API 返回 **`tool_use`**。现已通过 `providers/anthropic_bridge.py` 把 tools 转成 prompt，让豆包输出工具 JSON，再包装成 Anthropic 格式：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_API_KEY=sk-any
claude -p "写一个快速排序到 test.py"
```

```
Claude Code（tools + 对话历史）
    → anthropic_bridge 序列化 prompt
    → 豆包返回文字 + {"tool_uses":[...]}
    → 解析为 tool_use → Claude Code 本地执行 Write/Read/Bash
```

**局限：** 豆包不原生支持 Anthropic tools，靠 prompt 模拟，成功率低于官方 Claude；复杂多轮 Agent 可能失败。

配置：`ANTHROPIC_*`（不是 `OPENAI_*`），`ANTHROPIC_BASE_URL` 不加 `/v1`，改代码后需重启 `./run.sh`。

## 备选：mitmproxy 抓包

```bash
mitmweb -s mitm_addon.py -p 8080 --ssl-insecure
```

- 豆包 WS 参数 → `params/doubao/ws_params.json`
- DeepSeek 会话 → `params/deepseek/session.json`（抓包 `/api/v0/` 请求时自动保存）

## 常见问题

| 现象 | 处理 |
|------|------|
| 弹出浏览器要求登录 | 首次正常，登录后自动继续 |
| `未知模型` | 查看 `GET /v1/models` 支持的 model 列表 |
| API 返回 502 | 运行 `python doubao_auth.py` 或 `python deepseek_auth.py` 重新登录 |
| `Not logged in · Please run /login` | 设置 `ANTHROPIC_API_KEY=sk-any` |
| 只聊天不写文件 | 重启 `./run.sh` 加载 Tool 桥接；见上文 Agent 章节 |
| 说了要写但文件没出现 | 重启 `./run.sh` 加载最新 bridge；DeepSeek 若输出 `[调用 Write]` 现已自动解析为 `tool_use`；写文件需授权或使用 `claude --dangerously-skip-permissions` |
