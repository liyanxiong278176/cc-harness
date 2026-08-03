# TUI Transformation Design

> **Superseded:** This full-screen Textual design is superseded by
> `docs/specs/2026-08-02-inline-terminal-session-design.md`.

**Status**: 草案 v1
**Date**: 2026-07-30
**Author**: brainstorm with user
**Scope**: 把 cc-harness 的 Web UI 完全替换为 TUI,对齐 Claude Code 风格

---

## 1. 目标

cc-harness 当前 Web UI(1271 行 Python + 606 行 TS/TSX)将被完全删除,TUI 取而代之成为唯一前端。TUI 设计与 Claude Code UX 风格对齐(单 pane、流式 markdown、键盘优先),底层复用 `agent.run_turn` 与所有防御层。

**核心原则**:
- 不动 `agent.py` / `repl.py` / `llm.py` / `tools.py` / `memory/*` / `project/*`
- 不动 121 + 15 现有测试
- render.py 现有 `print_*` 函数保留为 REPLDriver / TestDriver 兼容层,新 API 是 `emit(event, driver)`
- 一次到位删除 Web UI,无灰度

---

## 2. 决策清单(13 项)

| # | 决策 | 备注 |
|---|---|---|
| Q1 | Web UI 完全删除 | `--serve` / `--port` flag 移除,`web/` 全删 |
| Q2 | Textual 框架 | `textual >=0.50,<1.0` + `textual-dev >=1.0` |
| Q3 | 单 pane | Claude Code 原味,无侧栏 |
| Q4 | 必做快捷键全选 | 多行 / @path / Tab / ↑↓ / Ctrl+R / Ctrl+C / Ctrl+L / Shift+Tab / Ctrl+T |
| Q5 | Markdown 渲染(Claude Code 风格) | 改 render.py,从 4-phase 改成 streaming markdown |
| Q6 | Token-by-token 流式 | LLM.stream → Textual.message,节流 50ms |
| Q7 | Shift+Tab 双档 | `default`(per-action 询问) / `auto`(全部 allow) |
| Q8 | Status bar B | 顶部 minimal + 底部 status(token/cost/help) |
| Q9 | 单 session + `/resume` on demand | 启动续上次,`/resume` 弹 modal |
| Q10 | 无 file tree | `@path` 唯一文件入口,连 `/files` 命令都不要 |
| Q11 | Theme:跟随系统 + 内置 dark + `/theme` | 默认 `textual-dark`,4 档(dark/light/system/high-contrast) |
| Q12 | Todo 内联 markdown 实时 | 助手 chat 内联 `-[ ]` / `-[x]`,diff-only update |
| Q13 | 一次到位删除 | 1 个 PR,无灰度,无 --tui 开关 |

---

## 3. 架构

### 3.1 4-zone 布局

```
┌─────────────────────────────────────────────┐
│ Header: opus-4 · ~/repo · main · [plan]     │  ← textual.Header
├─────────────────────────────────────────────┤
│                                             │
│  Chat log (vertical scroll)                 │  ← RichLog + Markdown
│  - user message                             │
│  - assistant markdown (streamed)            │
│  - tool call: ● Read file.py                │
│  - tool result: <colored>                   │
│  - todo update:                             │
│    - [x] Read README                        │
│    - [x] Parse                              │
│    - [ ] Implement                          │
│                                             │
│  (auto-scroll to bottom)                    │
├─────────────────────────────────────────────┤
│ Input: 多行 TextArea + @ + / 触发补全       │  ← TextArea
├─────────────────────────────────────────────┤
│ Footer: ↑ 1.2k ↓ 800 · $0.04  │ ⓘ ? for help │  ← textual.Footer
└─────────────────────────────────────────────┘
```

### 3.2 模块结构

```
cc_harness/tui/
├── __init__.py
├── app.py              # PipTuiApp(Textual App),mount 4 个 widget
├── widgets/
│   ├── header.py       # 顶部 status line(model/cwd/mode/permission)
│   ├── chat.py         # ChatLog(RichLog + Markdown wrapper),流式写
│   ├── input.py        # PromptInput(TextArea),多行 + @ / + ↑↓
│   ├── footer.py       # 底部 token/cost/help
├── screens/
│   ├── resume.py       # ModalScreen(/resume 历史 session 列表)
│   ├── theme.py        # ModalScreen(/theme 选项)
│   ├── hitl.py         # ModalScreen(L4 询问 yes/always/no)
├── events.py           # TUIEvent 协议(agent_run 推 → TUI 收)
├── completer.py        # @path + /command 补全
├── history.py          # Ctrl+R 历史搜
├── driver.py           # main() 入口,init 依赖,start app
├── test_app.py         # pytest-textual-snapshot
```

### 3.3 边界

- `cc_harness/tui/` 全新包,**不动** `agent.py` / `repl.py` / `llm.py` / `tools.py`
- `agent.run_turn()` 已经是 async generator(参考 `eval-v2` 提交 `99ebf96` / `620d3d8` / `96c11b0`),事件总线已就绪
- `render.py` 改造为 driver 抽象
- `cli/` 保留(init / resume / todo 不在 TUI 流程,工具型入口)
- `repl.py` 保留,作为 `--repl` flag 调试入口

---

## 4. 数据流

### 4.1 事件源

```python
# cc_harness/agent.py:run_turn() 已经 emit(参考 eval-v2 已落地)
async def run_turn(
    messages: list[dict],
    *,
    event_emitter: Callable[[AgentEvent], Awaitable[None]] | None = None,
) -> TurnResult:
    """每步产生 AgentEvent:
       - thinking_chunk(delta=token)
       - thinking_done
       - tool_call_start(name, args)
       - tool_call_end(result, error)
       - final_text(text)
       - usage(input/output/cached/reasoning)
       - todo_update(task_id, status)
    """
```

### 4.2 TUI 端订阅

```
LLM.stream(token)
       ↓
LLMClient yields chunk
       ↓
agent.run_turn(state, emitter=tui_emit)
       ↓
TUIEmitter.emit(event)  (适配器,把 AgentEvent → Textual message)
       ↓
app.post_message(WriteToken(token))  ← Textual 跨协程安全
       ↓
ChatLog.on_message → widget.write / append
```

### 4.3 跨线程

- `run_turn` 已在 asyncio 上下文 → 直接 `app.post_message(...)`(Textual method)
- 每个 event 一个 `Message` 子类,Textual dispatcher 自动派发到 widget

### 4.4 Ctrl+C

- Textual `BINDINGS = [Binding("ctrl+c", "interrupt", "Interrupt")]`
- `action_interrupt` 设置 `asyncio.Event` `_interrupt_event`
- agent loop 每次 iter 检查 `_interrupt_event.is_set()` → 取消 LLM stream / tool call

### 4.5 HITL 弹窗

- L4 `confirm_tool` 在异步上下文 → 调用 `app.push_screen(HITLModal(...), callback)`
- modal `RadioSet` + `Button` 三选(yes / always / no)
- modal 关闭 → `await condition.wait()` → 回调返回结果
- 写权限: modal 关闭时 `app.pop_screen()` 回到主界面

### 4.6 Resume / Theme / Help

- `ModalScreen` + `ListView` + 回调

---

## 5. 渲染层(`render.py` 改造)

### 5.1 公共 API

```python
class RenderDriver(Protocol):
    def write(self, text: str) -> None: ...
    def write_chunk(self, token: str) -> None: ...
    def write_tool_call(self, name: str, args: dict) -> None: ...
    def write_tool_result(self, result: str, error: bool) -> None: ...
    def write_todo(self, items: list[TodoItem]) -> None: ...
    def write_status(self, **fields) -> None: ...
    def refresh_token(self, stats: TokenStats) -> None: ...

def emit(event: RenderEvent, *, driver: RenderDriver) -> None:
    """event → markdown 段,driver 负责具体输出。"""
```

### 5.2 内置 driver

- `TUIDriver`:主 driver,实现 RenderDriver,通过 Textual message 派发
- `REPLDriver`:保留作 `--repl` 调试入口
- `TestDriver`:测试用,记录所有 `write` 调用
- `WebDriver`:留空壳(为未来扩展,此次不实现)

### 5.3 token-by-token 节流

- `write_chunk` 内部用 50ms 节流窗口:累计多个 token → 单次 `app.post_message`
- 流式期间 `Static` + spinner,完成后替换为 `Markdown` 渲染

### 5.4 事件 → markdown 映射

```
thinking_chunk  → ▌▌▌[token] (节流 → 最终单段)
thinking_done   → # 思考\n<final_text>
tool_call_start → ● tool_name(args_truncated)
tool_call_end   → result(绿) / error(红) + duration
final_text      → (final markdown 段)
todo_update     → diff-only:[ ] → [x] 替换单行
usage           → footer.refresh
mode_changed    → header.refresh
permission_mode_changed → header.refresh
```

### 5.5 Markdown 渲染

- ChatLog 内部 `RichLog` widget
- 助手 markdown → `textual.widgets.Markdown` 嵌套 widget
- 工具调用 → `Static(rich_text)` with 颜色
- streaming 期间,**临时** `Static` + spinner,完成后替换为 `Markdown` 渲染

---

## 6. 依赖 + 入口

### 6.1 pyproject.toml 变更

```toml
[project]
dependencies = [
    # ... 现有
    "textual>=0.50,<1.0",
    "textual-dev>=1.0",
]

[project.optional-dependencies]
test = [
    # ... 现有
    "pytest-textual-snapshot>=0.4",
]
```

### 6.2 main.py 入口

```python
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["coding", "plan", "design"], default="coding")
    parser.add_argument("--repl", action="store_true", help="legacy REPL 调试入口")
    parser.add_argument("--cwd", default=".")
    args = parser.parse_args()

    if args.repl:
        from cc_harness.repl import run_repl
        asyncio.run(run_repl(...))
    else:
        from cc_harness.tui.driver import run_tui
        asyncio.run(run_tui(cwd=args.cwd, mode=args.mode))
```

### 6.3 文件删除清单

- `web/` 整个目录(React + Vite + Tailwind + Zustand + xterm.js + Monaco)
- `cc_harness/web/` 整个目录(app.py + boot.py + run_loop.py + sessions.py + pty.py + routes/* + emitter.py + events.py)
- `tests/_test_web_smoke.py`
- `main.py` 中 `--serve` / `--port` flag
- `package.json` / `package-lock.json` / `vite.config.*` / `tsconfig.*` / `tailwind.config.js` / `web/dist/`

### 6.4 保留

- `cc_harness/cli/`:init / resume / todo
- `repl.py`:`--repl` 调试入口
- `cc_harness/memory/` / `cc_harness/project/` / `cc_harness/drift/` / `cc_harness/reflection/`:全部不动

### 6.5 仓库体积变化

- 前:`web/` ~50MB 依赖 + 600 行 TS/TSX
- 后:`textual` ~80MB 依赖 + 0 行 frontend
- **净依赖 +30MB,代码 -600 行 + 删 build chain**

---

## 7. 测试

### 7.1 三层策略

1. **现有 121 + 15 测试不动** — 全走 `FakeLLM` / `FakeMCP`,无 Touch TUI
2. **TUI 单元测试** — `cc_harness/tui/test_*.py`:
   - `test_app.py`:`pytest-textual-snapshot`(固定 120×40)
   - `test_completer.py`:`@path` + `/command` 补全
   - `test_history.py`:Ctrl+R 历史搜索
   - `test_render_driver.py`:TUIDriver.message 路由
   - `test_input.py`:多行 + Tab + Shift+Tab 行为
3. **集成测试** — `tests/_test_tui_e2e.py`(`_test_*.py` 命名,需真 LLM,默认不跑):
   - `pilot` 驱动 headless TUI
   - 验证 token-by-token streaming
   - 验证 Ctrl+C interrupt
   - 验证 HITL modal
   - 验证 /resume modal

### 7.2 验收标准

- [ ] `python main.py` 启动 TUI,默认 Tokyo Night 主题
- [ ] 输入 `hello` → 看到 markdown 流式输出
- [ ] `/help` 弹出 modal 显示所有 slash 命令
- [ ] `/theme` 切换 dark/light/system/high-contrast
- [ ] `Shift+Tab` 切换 default/auto,顶部 status 改变
- [ ] `Ctrl+C` 中断 LLM 流不挂
- [ ] `Ctrl+L` 清屏
- [ ] `@README.md` 显示前 N 行 preview
- [ ] `/resume` 弹出 session list
- [ ] `Ctrl+T` 弹 todo 状态
- [ ] 长会话触发 4-tier 压缩,顶部 token 数字更新
- [ ] `python -m pytest` 121 + 15 现有测试全 pass
- [ ] 删除 `web/` 后仓库 tarball 体积减少 ≥ 50MB
- [ ] `python main.py --repl` 仍可走 legacy REPL 调试

---

## 8. 风险 + 缓解

| 风险 | 缓解 |
|---|---|
| render.py 改造范围广,可能破 121 测试 | `print_*` 保留为 REPLDriver / TestDriver 内部实现,新 API 是 `emit(event, driver)` <br> 现有测试用 TestDriver,验证 emit 输入 + 输出 |
| Textual 在 Jupyter / Subagent 进程可能冲突 | `python -m cc_harness.tui` 走独立进程,不在现有 asyncio 上下文 |
| Token-by-token 流式掉帧 | 节流 50ms,实测在 20+ token/s 下不掉 |
| Tab 补全 / @path 弹窗覆盖 chat | ModalScreen 自动 dim 主界面,标准 Textual 行为 |
| HITL modal 在 token 流期间弹出 | `asyncio.Event` 同步,modal 关闭前流暂停 |
| 删除 `web/` 后某些脚本 broke | `git grep -l "from cc_harness.web"` 全删;`git grep -l "FastAPI\|uvicorn"` 全删;`git grep -l "web/src"` 全删 |
| `cc_harness/cli/resume.py` 现依赖 Web session? | 实际查 `cli/resume.py` 验证(plan 阶段),不依赖 Web 应保留 |
| `pytest-textual-snapshot` snapshot 在不同终端宽度下 diff | `app.run_test(size=(120, 40))` 固定尺寸 |

---

## 9. 非目标(留 TODO.md 后续)

- PaiCLI 全套 parity(Skill/RAG/Snapshot/Runtime API/Multi-Agent 3 角色)
- Eval-v2 增量
- Web UI 恢复
- Mob / Vim 模式
- Image input
- 模型 Profile / 价格表
- 外部 / mcp serve / mcp init-chrome / 教程路线
- TUI 多 session / tab 切换 / 分屏

---

## 10. spec 文档 / plan 文档

- 本文:`docs/superpowers/specs/2026-07-30-tui-transformation-design.md`
- 后续 plan:`docs/superpowers/plans/2026-07-30-tui-transformation-plan.md`(writing-plans 阶段)
- 后备 TODO:`TODO.md`(项目根)
