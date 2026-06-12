# free-claude

## mitm 
mitmweb -s mitm_addon.py -p 8080 --ssl-insecure \
  --ignore-hosts 'apple\.com|push\.apple\.com|icloud\.com'

豆包 WebSocket 中转 + Claude Code OpenAI 兼容 API。

## 一、快速开始（自动获取凭证，推荐）

```bash
cd free-claude
chmod +x run.sh
./run.sh
```

或手动：

```bash
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # 仅首次需要
python trans_api.py
```

> 不要用系统自带的 `python trans_api.py`，必须用 `venv` 里的 Python。

**每次启动 `trans_api.py` 时**会自动：
1. 删除旧的 `doubao_ws_params.json`
2. 用 Playwright 登录豆包并保存 `sessionid`
3. 对话通过浏览器内 `/chat/completion` SSE 完成（自动注入 a_bogus，无需抓 WebSocket）
4. 登录状态保存在 `.doubao_browser_profile/`

也可手动刷新凭证：

```bash
python doubao_auth.py
```

测试：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-claude","messages":[{"role":"user","content":"1+1="}],"stream":false}'
```

遇到 **401** 时会自动重新抓取参数；无需手抄 `sessionid`。

---

## 二、备选：手动抓包

若 Playwright 不可用，可手动从 Chrome DevTools 复制 WS URL + `sessionid` 到 `doubao_ws_params.json`。参考 `doubao_ws_params.example.json`。

### mitmproxy 抓包

```bash
mitmweb -s mitm_addon.py -p 8080 --ssl-insecure
```

---

## 三、接入 Claude Code

```
OPENAI_API_BASE=http://127.0.0.1:8000/v1
OPENAI_API_KEY=sk-any
```

---

## 四、常见问题

| 现象 | 处理 |
|------|------|
| 弹出浏览器要求登录 | 首次正常，登录后自动继续 |
| `playwright install chromium` | 首次安装浏览器内核 |
| API 返回 401 | 会自动刷新；失败则运行 `python doubao_auth.py` |
| `certificate unknown` (手机) | 与 API 无关；手机抓包见 mitmproxy 方案 |
