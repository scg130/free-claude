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
cp .env.example .env   # 可选，按需修改配置
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

```bash
cd /path/to/your-project          # 项目目录由这里决定，Claude Code 会传给代理
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

### 配置（.env）

所有服务端配置集中在项目根目录 **`.env`** 文件（从 `.env.example` 复制）：

```bash
cp .env.example .env
```

`run.sh` 与 `trans_api.py` 启动时自动加载。主要项：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `API_HOST` / `API_PORT` | `127.0.0.1:8000` | 代理监听地址 |
| `CONTEXT` | `1` | 是否注入项目上下文 |
| `CONTEXT_MODE` | `lite` | `lite` / `tree` / `full` |
| `CREDENTIAL_CHECK_INTERVAL` | `3600` | 凭证后台检查间隔（秒） |
| `RETRY_MAX` | `3` | 请求失败重试次数 |
| `RATE_LIMIT_RPM` | `30` | 每 provider 每分钟请求上限 |

完整列表见 [`.env.example`](.env.example)，每项均有中文注释。

### 项目上下文

**谁提供项目目录？** Claude Code（客户端）。你在哪个目录运行 `claude`，它就会在每次请求的 system prompt 里带上 **working directory**；服务端收到请求后**自动识别**并扫描该目录。**run.sh 不知道、也不需要知道你在哪个项目。**

**服务端只管注入策略**（在 `.env` 中配置）——开不开、注入多少：

用法：

```bash
# 终端 1：启动代理（与项目无关，任意目录均可）
./run.sh

# 终端 2：在你的项目目录运行 claude
cd /path/to/your-project
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_API_KEY=sk-any
claude --model deepseek-coder --permission-mode bypassPermissions
```

需要完整源码时，编辑 `.env` 后重启：

```bash
# .env 中设置
CONTEXT_MODE=full
CONTEXT_MAX_CHARS=30000
./run.sh
```

关闭注入（最快，但不结合项目代码）：`.env` 中设 `CONTEXT=0`

| 配置 | 设置位置 | 说明 |
|------|----------|------|
| 项目目录 | **Claude Code 客户端** | `cd your-project && claude`，自动从 system 传入 |
| `CONTEXT` / `CONTEXT_MODE` / `CONTEXT_MAX_CHARS` | **`.env` 服务端** | 只控制是否注入、注入多少 |
| `CONTEXT_ALWAYS` | `.env` 服务端 | 每轮都重新扫描（默认仅首轮） |

日志示例（项目路径来自请求，不是 run.sh 配置的）：

```
[context] coding 已注入项目上下文: /path/to/your-project mode=lite (19506 chars)
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

## 凭证与可靠性

| 能力 | 说明 |
|------|------|
| 凭证备份 | `session.json.bak`，主文件损坏时自动恢复 |
| 自动续期 | 后台每 `CREDENTIAL_CHECK_INTERVAL` 秒（默认 3600）无头校验，失效则自动刷新 |
| 统一刷新 | `python auth.py [doubao\|deepseek\|all]` |
| 健康检查 | `GET /health` 查看各 provider 凭证状态 |
| 手动续期 | `POST /health/refresh` |
| 请求重试 | 网络波动自动重试 `RETRY_MAX` 次（默认 3） |
| 速率限制 | 默认 `RATE_LIMIT_RPM=30` 防封禁 |
| PoW 并行 | DeepSeek PoW 在锁外线程池求解，减少阻塞 |

```bash
curl http://127.0.0.1:8000/health
python auth.py all          # 刷新全部凭证
```

## 常见问题

| 现象 | 处理 |
|------|------|
| 弹出浏览器要求登录 | 首次正常，登录后自动继续 |
| `未知模型` | 查看 `GET /v1/models` 支持的 model 列表 |
| API 返回 502 | `python auth.py all` 或 `curl -X POST http://127.0.0.1:8000/health/refresh` |
| `Not logged in · Please run /login` | 设置 `ANTHROPIC_API_KEY=sk-any` |
| 只聊天不写文件 | 重启 `./run.sh` 加载 Tool 桥接；见上文 Agent 章节 |
| 说了要写但文件没出现 | 重启 `./run.sh` 加载最新 bridge；DeepSeek 若输出 `[调用 Write]` 现已自动解析为 `tool_use`；写文件需授权或使用 `claude --dangerously-skip-permissions` |
| DeepSeek 浏览器被占用（WSL/Linux） | 重新运行 `./run.sh`；系统依赖有问题时：`./run.sh --reinstall-system-deps` |
| 回复很慢（1 分钟+） | DeepSeek 每轮走网页 API + PoW；可 `CONTEXT=0` 关闭注入提速；看日志 `[deepseek] prompt=... total=...s` |
| 交互模式只说不做 / Cogitated 后无回答 | 问答类自动走纯聊天并注入项目上下文；重启 `./run.sh` 后重试 |

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