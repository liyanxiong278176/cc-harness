# cc-harness

Terminal coding agent with MCP tools. ReAct loop driven by an OpenAI-compatible
LLM (DeepSeek by default), 4-tier context compaction, and rich tool support
via the Model Context Protocol.

> **Status (2026-08-02):** TUI 已是默认入口(`python main.py` 直接走 TUI)。
> 21 个 `cc_harness/tui/test_*.py` 测试全 pass,legacy REPL 入口用 `--repl` flag 切回。
> Boot 流程:`main.py` 加载 config → 构造 `LLMClient` + `MCPClient` → 注入 `PipTuiApp`
> (同 legacy REPL 的 boot 路径)。

## 启动

### TUI(默认)

```bash
python main.py
```

启动 TUI,默认 Tokyo Night 主题,4 区布局(Header / ChatLog / PromptInput / Footer)。

Boot 流程:`main.py` 加载 `.env` + `mcp.json` → 构造 `LLMClient(api_key, model, base_url)`
→ 构造 `MCPClient(cfg.mcp_servers)` → `await mcp.start()` → 把 `llm` + `mcp` 注入
`PipTuiApp`。Config 错误 fail-fast(在 TUI 启动前报错,不进 silent no-op app)。

### REPL(legacy,调试用)

```bash
python main.py --repl
```

切回 legacy REPL 入口(原 `cc_harness/repl.py:run_repl`,用于对比调试
TUI 路径)。Boot 流程与 TUI 一致 + 额外构造 memory / reflection / drift /
sandbox pre-warm。

### 快捷键

| 快捷键 | 行为 |
|---|---|
| Enter | 提交输入 |
| Shift+Enter | 换行 |
| Tab | 补全 / 或 @ |
| ↑ / ↓ | 历史 |
| Ctrl+R | 反向搜历史 |
| Ctrl+C | 中断 LLM 流 |
| Ctrl+L | 清屏 |
| Shift+Tab | 切换权限模式 |
| Ctrl+T | 切换 todo 显示 |

实现: `cc_harness/tui/app.py` (`BINDINGS` + `action_*` 派发)。

### Slash 命令

`/help` `/theme` `/resume` `/model` `/clear` `/exit` `/memory` `/usage` `/policy` `/audit` `/config` `/tools` `/mcp` `/hitl` `/plan` `/team` `/snapshot` `/restore` `/search` `/index` `/skill` `/context`

完整命令见 `/help` 弹窗。补全源: `cc_harness/tui/completer.py:SLASH_COMMANDS`
(24 个,含上列 22 + `/reset` + `/version`)。

## Architecture (data flow)

```
main.py                                  # 默认 → TUI; --repl → legacy REPL
  ├── [default] cc_harness/tui/driver.py:run_tui(cwd, mode)
  │     └── PipTuiApp (Textual App)
  │           ├── HeaderBar        # model / cwd / branch / mode / permission
  │           ├── ChatLog          # RichLog + markup, 4-phase 思考/行动/观察/结果
  │           ├── PromptInput      # TextArea, Tab 补全 / 提交 Submitted 消息
  │           └── FooterBar        # input/output tokens + cost
  │                 ↑
  │           TUIDriver (RenderDriver) → post_message 派发到 widget
  │                 ↑
  │           cc_harness.agent.run_turn event_emitter
  │
  └── [--repl] cc_harness/repl.py:run_repl()       # sticky mode (coding/plan/design)
        └── run_turn()  [agent.py]                  # ReAct loop
              ├── context.py:maybe_compact          # 4-tier cascade
              ├── llm.py / mcp_client.py            # providers
              └── tokens.py:TokenCounter            # 6-bucket token tracking
```

Boot 路径共享:`main.py` 加载 config → 构造 `LLMClient` + `MCPClient` → 注入
对应入口(`PipTuiApp` 或 `run_repl`)。

## Test summary (2026-08-02)

- `tests/`: **1453 passed, 1 skipped**(PTY POSIX)
- `cc_harness/tui/test_*.py`: **21 passed**
- **Total: 1474 passed, 1 skipped, 0 failed**

运行:

```bash
PYTHONIOENCODING=utf-8 python -m pytest tests/ cc_harness/tui/test_*.py \
  -W ignore::pytest.PytestUnhandledThreadExceptionWarning
```

## Out of scope (don't add unless asked)

- Multi-LLM backend switching (locked to OpenAI-compatible)
- Kernel sandbox (gVisor / Firecracker) — out of scope
- Wiring `cc_harness/memory/` into the live agent — package exists but
  not yet imported by the ReAct loop
- Concurrent tool calls (serial only)
- SubAgent / Agent Team (PDF 阶段 4-5, not started)
