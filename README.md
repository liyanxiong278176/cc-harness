# cc-harness

Terminal coding agent with MCP tools. ReAct loop driven by an OpenAI-compatible
LLM (DeepSeek by default), 4-tier context compaction, and rich tool support
via the Model Context Protocol.

> **Status (2026-08-02):** TUI 实现已合入 `cc_harness/tui/`(21 测试全 pass),
> `main.py` 入口派发到 REPL 仍为默认值。TUI 走 `cc_harness.tui.driver.run_tui()`。

## 启动

### TUI(默认)

```bash
python main.py
```

启动 TUI,默认 Tokyo Night 主题,4 区布局(Header / ChatLog / PromptInput / Footer)。

> **当前实际入口:** `python main.py` 默认走 `run_repl`(legacy REPL 调试入口,
> 见下)。TUI 组件已就绪,主入口派发尚未切到 `run_tui` —— 计划为
> `python -m cc_harness.tui` 或 Task 18 后续的 `main.py` 派发调整。
> 组件验证:21 个 `cc_harness/tui/test_*.py` 测试全 pass(见末尾测试摘要)。

### REPL(调试)

```bash
python main.py --repl
```

走 legacy REPL 调试入口。

> **当前状态:** `--repl` flag 尚未在 `main.py` argparse 注册(`unrecognized arguments: --repl`)。
> legacy REPL 入口目前是默认行为,无需 flag。

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

`/help` `/theme` `/resume` `/model` `/clear` `/exit` `/task` `/save` `/memory` `/usage` `/policy` `/audit` `/config` `/tools` `/mcp` `/hitl` `/plan` `/team` `/snapshot` `/restore` `/search` `/index` `/skill` `/context`

完整命令见 `/help` 弹窗。补全源: `cc_harness/tui/completer.py:SLASH_COMMANDS`
(26 个,含上列 24 + `/reset` + `/version`)。

## Architecture (data flow)

```
main.py
  └── repl.py:run_repl()                  # sticky mode (coding/plan/design)
        └── run_turn()  [agent.py]        # ReAct loop
              ├── context.py:maybe_compact  # 4-tier cascade
              ├── llm.py / mcp_client.py   # providers
              └── tokens.py:TokenCounter    # 6-bucket token tracking
```

TUI 平行入口(组件已就绪,主入口派发未切):

```
cc_harness/tui/driver.py:run_tui(cwd, mode)
  └── PipTuiApp (Textual App)
        ├── HeaderBar        # model / cwd / branch / mode / permission
        ├── ChatLog          # RichLog + markup, 4-phase 思考/行动/观察/结果
        ├── PromptInput      # TextArea, Tab 补全 / 提交 Submitted 消息
        └── FooterBar        # input/output tokens + cost
              ↑
        TUIDriver (RenderDriver) → post_message 派发到 widget
              ↑
        cc_harness.agent.run_turn event_emitter
```

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
