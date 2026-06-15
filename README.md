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

3. Agent 写文件（需授权 + 重启 `./run.sh` 加载最新 bridge）：

**项目上下文（自动）：** 在**你的项目目录**下运行 `claude`（`cd your-project && claude`），会从 Claude Code 的 system 里读取 **working directory**，自动注入该目录信息（**不是** free-claude 安装目录）。

```bash
cd /path/to/your-project          # 你的项目
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_API_KEY=sk-any
claude --model deepseek-coder --permission-mode bypassPermissions -p "写一个快速排序到 test.py"
```

### 重启代理

改代码或更新配置后，需重启中转服务：

```bash
cd /path/to/free-claude
./run.sh
# 等到: Uvicorn running on http://127.0.0.1:8000
```

### 加速与项目上下文（推荐）

DeepSeek 走网页 API + PoW，**每轮工具调用都要 30s～90s**。`run.sh` 已内置默认配置（**无需在 Claude 窗口设置**）：

```bash
# run.sh 内默认值（最快）
FREE_CLAUDE_CONTEXT=0
FREE_CLAUDE_CONTEXT_MODE=tree
```

直接 `./run.sh` 即可。需要完整源码时，**在 run.sh 同终端**临时覆盖后启动：

```bash
FREE_CLAUDE_CONTEXT=1 FREE_CLAUDE_CONTEXT_MODE=full FREE_CLAUDE_CONTEXT_MAX_CHARS=30000 ./run.sh
```

或先 export 再启动：

```bash
export FREE_CLAUDE_CONTEXT=1
export FREE_CLAUDE_CONTEXT_MODE=full
export FREE_CLAUDE_CONTEXT_MAX_CHARS=30000
./run.sh
```

| 环境变量 | 说明 |
|----------|------|
| 设置位置 | **`./run.sh` 终端**（服务端）；`ANTHROPIC_*` 在 Claude Code 终端 |
| `FREE_CLAUDE_PROJECT_ROOT` | 可选，手动指定项目根（默认从 Claude Code 工作目录自动识别） |
| `FREE_CLAUDE_CONTEXT=0` | 关闭项目上下文注入（`run.sh` 默认，最快） |
| `FREE_CLAUDE_CONTEXT=1` | 开启注入 |
| `FREE_CLAUDE_CONTEXT_MODE` | `tree`（`run.sh` 默认）/ `lite` / `full` |
| `FREE_CLAUDE_CONTEXT_MAX_CHARS` | 上下文总字符上限；`full` 模式建议 30000 |
| `FREE_CLAUDE_CONTEXT_ALWAYS=1` | 每轮 API 都重新扫描项目（默认仅会话首轮） |

重启 `./run.sh` 后，终端会打印耗时日志，便于排查慢在哪一步：

```
[deepseek] prompt=1234 chars | session+pow=2.1s solve=0.3s completion=45.2s total=47.6s
[context] 已注入项目上下文: /path/to/project (786 chars)
```

`-p` 会执行工具，但必须加 `--permission-mode bypassPermissions`（或 `--dangerously-skip-permissions`），否则 Write 不会落盘。

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
claude --model deepseek-coder --permission-mode bypassPermissions -p "写一个冒泡排序到 test.py"
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
| DeepSeek 浏览器被占用（WSL/Linux） | 重新运行 `./run.sh`；系统依赖有问题时：`./run.sh --reinstall-system-deps` |
| 回复很慢（1 分钟+） | 正常：每轮工具调用都要走 DeepSeek 网页 API + PoW；已默认 `FREE_CLAUDE_CONTEXT_MODE=tree` 减小 prompt；重启 `./run.sh` 后看日志 `[deepseek] prompt=... total=...s` |

## Claude Code 配置示例

`~/.claude/settings.json`：

```json
{
  "model": "deepseek-coder",
  "skipDangerousModePermissionPrompt": true,
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8000",
    "ANTHROPIC_API_KEY": "sk-any"
  },
  "permissions": {
    "allow": [
      "mcp__codegraph__codegraph_explore",
      "mcp__codegraph__codegraph_search",
      "mcp__codegraph__codegraph_node",
      "mcp__codegraph__codegraph_callers",
      "mcp__codegraph__codegraph_callees",
      "mcp__codegraph__codegraph_impact",
      "mcp__codegraph__codegraph_files",
      "mcp__codegraph__codegraph_status"
    ]
  }
}
```