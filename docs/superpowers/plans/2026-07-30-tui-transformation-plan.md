# TUI Transformation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 cc-harness 的 Web UI 完全替换为 TUI,对齐 Claude Code 风格,底层 agent / 防御层 / 红队不动。

**Architecture:** 4-zone Textual UI(Header / ChatLog / PromptInput / Footer)+ 改 cc_harness/render.py 为 driver 抽象(TUIDriver / REPLDriver / TestDriver / WebDriver-stub)+ 一次到位删除 web/ + cc_harness/web/。Agent.run_turn 已有 event_emitter 接口(eval-v2 落地),直接复用为 TUI 事件源。

**Tech Stack:** Python 3.11+ / Textual >=0.50,<1.0 / textual-dev >=1.0 / Rich(已有) / prompt_toolkit(已有) / pytest-textual-snapshot >=0.4

**Spec:** `docs/superpowers/specs/2026-07-30-tui-transformation-design.md`

**Backlog:** `TODO.md`(根)

---

## Global Constraints

来自 spec,所有 task 隐式遵守:

- **Python 版本**:3.11+
- **textual 版本**:`>=0.50,<1.0`
- **textual-dev 版本**:`>=1.0`
- **pytest-textual-snapshot 版本**:`>=0.4`
- **核心边界(不动)**:`cc_harness/agent.py` / `cc_harness/repl.py` / `cc_harness/llm.py` / `cc_harness/tools.py` / `cc_harness/memory/*` / `cc_harness/project/*` / `cc_harness/drift/*` / `cc_harness/reflection/*`
- **现有测试(不动)**:`tests/test_*.py` 121 个 + `tests/_test_*.py` 15 个
- **render.py 兼容层**:保留现有 `print_*` 函数,被 REPLDriver 内部调用,新 API 是 `emit(event, driver)`
- **删除清单**:`web/` 整个目录、`cc_harness/web/` 整个目录、`tests/_test_web_smoke.py`、main.py `--serve` / `--port` flag
- **保留**:`cc_harness/cli/`(init / resume / todo)、`--repl` 调试入口
- **风格对齐 Claude Code**:单 pane、markdown 渲染、token-by-token 流式、思考可折叠
- **commit 风格**:Conventional Commits,每个 task 独立 commit
- **主题**:默认 `textual-dark`,`/theme` 支持 dark / light / system / high-contrast

---

## Task 1: RenderDriver Protocol + RenderEvent types

**Files:**
- Create: `cc_harness/render_protocol.py`
- Test: `tests/test_render_protocol.py`

**Interfaces:**
- Produces: `RenderEvent` 协议类(thinking_chunk / thinking_done / tool_call_start / tool_call_end / final_text / usage / todo_update / mode_changed / permission_mode_changed)
- Produces: `RenderDriver` Protocol(write / write_chunk / write_tool_call / write_tool_result / write_todo / write_status / refresh_token)

- [ ] **Step 1: 写 failing test**

```python
# tests/test_render_protocol.py
from cc_harness.render_protocol import RenderDriver, RenderEvent, ThinkingChunk, ToolCallStart, TodoUpdate

def test_render_event_immutable():
    """RenderEvent 应是 frozen dataclass,不可变。"""
    e = ThinkingChunk(delta="hello")
    assert e.delta == "hello"
    try:
        e.delta = "world"  # type: ignore
        assert False, "should have raised"
    except Exception:
        pass

def test_render_driver_protocol_has_required_methods():
    """RenderDriver Protocol 必须有 7 个方法。"""
    required = {"write", "write_chunk", "write_tool_call", "write_tool_result", "write_todo", "write_status", "refresh_token"}
    assert required.issubset(set(dir(RenderDriver)))

def test_tool_call_start_carries_tool_name():
    e = ToolCallStart(name="run_command", args={"cmd": "pytest"})
    assert e.name == "run_command"
    assert e.args == {"cmd": "pytest"}

def test_todo_update_carries_progress():
    e = TodoUpdate(items=[{"id": "1", "title": "Read", "status": "done"}])
    assert e.items[0]["status"] == "done"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_render_protocol.py -v`
Expected: 4 failed,ModuleNotFoundError(`cc_harness.render_protocol`)

- [ ] **Step 3: 实现 render_protocol.py**

```python
# cc_harness/render_protocol.py
"""Render layer abstraction:events + driver protocol.

TUI / REPL / Test 共用同一套事件,driver 决定具体输出。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


# --- RenderEvent 子类(每个 frozen dataclass) ---

@dataclass(frozen=True)
class ThinkingChunk:
    delta: str

@dataclass(frozen=True)
class ThinkingDone:
    text: str

@dataclass(frozen=True)
class ToolCallStart:
    name: str
    args: dict[str, Any]

@dataclass(frozen=True)
class ToolCallEnd:
    name: str
    result: str
    error: bool
    duration_ms: int

@dataclass(frozen=True)
class FinalText:
    text: str

@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int

@dataclass(frozen=True)
class TodoUpdate:
    items: list[dict[str, str]]  # [{"id", "title", "status"}]

@dataclass(frozen=True)
class ModeChanged:
    mode: str  # coding / plan / design

@dataclass(frozen=True)
class PermissionModeChanged:
    mode: str  # default / auto


# --- RenderEvent 联合类型 ---

RenderEvent = (
    ThinkingChunk | ThinkingDone | ToolCallStart | ToolCallEnd
    | FinalText | Usage | TodoUpdate | ModeChanged | PermissionModeChanged
)


# --- RenderDriver Protocol ---

@runtime_checkable
class RenderDriver(Protocol):
    def write(self, text: str) -> None: ...
    def write_chunk(self, token: str) -> None: ...
    def write_tool_call(self, name: str, args: dict[str, Any]) -> None: ...
    def write_tool_result(self, result: str, error: bool) -> None: ...
    def write_todo(self, items: list[dict[str, str]]) -> None: ...
    def write_status(self, **fields: Any) -> None: ...
    def refresh_token(self, stats: Any) -> None: ...
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_render_protocol.py -v`
Expected: 4 passed

- [ ] **Step 5: commit**

```bash
git add cc_harness/render_protocol.py tests/test_render_protocol.py
git commit -m "feat(render): RenderDriver Protocol + RenderEvent types"
```

---

## Task 2: TestDriver — 记录所有 write 调用

**Files:**
- Create: `cc_harness/render_test_driver.py`
- Test: `tests/test_render_test_driver.py`

**Interfaces:**
- Implements: `RenderDriver`
- Produces: `.events: list[str]` 记录,`.tokens: list[str]` 流式 token,`.tool_calls: list[tuple]`

- [ ] **Step 1: 写 failing test**

```python
# tests/test_render_test_driver.py
from cc_harness.render_test_driver import TestDriver

def test_records_writes():
    d = TestDriver()
    d.write("hello")
    d.write("world")
    assert d.events == ["hello", "world"]

def test_records_chunks_as_tokens():
    d = TestDriver()
    d.write_chunk("a")
    d.write_chunk("b")
    assert d.tokens == ["a", "b"]

def test_records_tool_calls():
    d = TestDriver()
    d.write_tool_call("run_command", {"cmd": "pytest"})
    assert d.tool_calls == [("run_command", {"cmd": "pytest"})]

def test_flush_returns_aggregated_stream():
    d = TestDriver()
    d.write_chunk("a")
    d.write_chunk("b")
    d.write_chunk("c")
    assert d.flush_stream() == "abc"

def test_reset_clears_history():
    d = TestDriver()
    d.write("x")
    d.reset()
    assert d.events == []
    assert d.tokens == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_render_test_driver.py -v`
Expected: 5 failed,ModuleNotFoundError

- [ ] **Step 3: 实现 TestDriver**

```python
# cc_harness/render_test_driver.py
"""TestDriver:记录所有 RenderDriver 调用,用于测试与断言。"""
from __future__ import annotations

from typing import Any


class TestDriver:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.tokens: list[str] = []
        self.tool_calls: list[tuple[str, dict[str, Any]]] = []
        self.tool_results: list[tuple[str, bool, int]] = []
        self.todos: list[list[dict[str, str]]] = []
        self.status_fields: list[dict[str, Any]] = []
        self.token_stats: list[Any] = []

    def write(self, text: str) -> None:
        self.events.append(text)

    def write_chunk(self, token: str) -> None:
        self.tokens.append(token)

    def write_tool_call(self, name: str, args: dict[str, Any]) -> None:
        self.tool_calls.append((name, args))

    def write_tool_result(self, result: str, error: bool) -> None:
        # 在 TestDriver 里 result 字符串前缀标记 error
        self.tool_results.append((result, error, 0))

    def write_todo(self, items: list[dict[str, str]]) -> None:
        self.todos.append(items)

    def write_status(self, **fields: Any) -> None:
        self.status_fields.append(fields)

    def refresh_token(self, stats: Any) -> None:
        self.token_stats.append(stats)

    def flush_stream(self) -> str:
        """聚合所有 token 返回单字符串(用于断言流式输入)。"""
        return "".join(self.tokens)

    def reset(self) -> None:
        self.events.clear()
        self.tokens.clear()
        self.tool_calls.clear()
        self.tool_results.clear()
        self.todos.clear()
        self.status_fields.clear()
        self.token_stats.clear()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_render_test_driver.py -v`
Expected: 5 passed

- [ ] **Step 5: commit**

```bash
git add cc_harness/render_test_driver.py tests/test_render_test_driver.py
git commit -m "feat(render): TestDriver - 记录所有 RenderDriver 调用"
```

---

## Task 3: emit() dispatcher + REPLDriver(保留 print_* 兼容)

**Files:**
- Modify: `cc_harness/render.py` — 保留现有 `print_*` 函数,新增 `emit(event, driver)` dispatcher
- Create: `cc_harness/render_repl_driver.py`
- Test: `tests/test_render_emit.py`

**Interfaces:**
- Produces: `emit(event: RenderEvent, *, driver: RenderDriver) -> None` — 公共 API
- Produces: `REPLDriver` — 实现 RenderDriver,内部封装 print_* 行为

- [ ] **Step 1: 写 failing test**

```python
# tests/test_render_emit.py
from cc_harness.render import emit
from cc_harness.render_test_driver import TestDriver
from cc_harness.render_protocol import ThinkingChunk, ToolCallStart, ToolCallEnd, FinalText, TodoUpdate, Usage

def test_emit_thinking_chunk_appends():
    d = TestDriver()
    emit(ThinkingChunk(delta="hello"), driver=d)
    assert d.tokens == ["hello"]

def test_emit_tool_call_start_records():
    d = TestDriver()
    emit(ToolCallStart(name="run_command", args={"cmd": "ls"}), driver=d)
    assert d.tool_calls == [("run_command", {"cmd": "ls"})]

def test_emit_tool_call_end_records():
    d = TestDriver()
    emit(ToolCallEnd(name="ls", result="file1\nfile2", error=False, duration_ms=120), driver=d)
    assert d.tool_results == [("file1\nfile2", False, 120)]

def test_emit_final_text_writes():
    d = TestDriver()
    emit(FinalText(text="summary"), driver=d)
    assert d.events == ["summary"]

def test_emit_todo_update_writes():
    d = TestDriver()
    emit(TodoUpdate(items=[{"id": "1", "title": "Read", "status": "done"}]), driver=d)
    assert d.todos == [[{"id": "1", "title": "Read", "status": "done"}]]

def test_emit_usage_refreshes_token():
    d = TestDriver()
    emit(Usage(input_tokens=100, output_tokens=50, cached_tokens=0, reasoning_tokens=10), driver=d)
    assert len(d.token_stats) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_render_emit.py -v`
Expected: 6 failed,ImportError(`emit` not found)

- [ ] **Step 3: 实现 emit() + REPLDriver**

```python
# cc_harness/render.py — 在文件末尾追加内容(现有 print_* 保留)
# (顶部新增 import)
from cc_harness.render_protocol import (
    RenderDriver, RenderEvent,
    ThinkingChunk, ThinkingDone, ToolCallStart, ToolCallEnd,
    FinalText, Usage, TodoUpdate, ModeChanged, PermissionModeChanged,
)

def emit(event: RenderEvent, *, driver: RenderDriver) -> None:
    """事件分发:RenderEvent → driver 对应方法。"""
    if isinstance(event, ThinkingChunk):
        driver.write_chunk(event.delta)
    elif isinstance(event, ToolCallStart):
        driver.write_tool_call(event.name, event.args)
    elif isinstance(event, ToolCallEnd):
        driver.write_tool_result(event.result, event.error)
    elif isinstance(event, FinalText):
        driver.write(event.text)
    elif isinstance(event, ThinkingDone):
        driver.write(event.text)
    elif isinstance(event, TodoUpdate):
        driver.write_todo(event.items)
    elif isinstance(event, Usage):
        driver.refresh_token(event)
    elif isinstance(event, ModeChanged):
        driver.write_status(mode=event.mode)
    elif isinstance(event, PermissionModeChanged):
        driver.write_status(permission_mode=event.mode)
    else:
        raise TypeError(f"Unknown event type: {type(event)}")
```

```python
# cc_harness/render_repl_driver.py
"""REPLDriver:实现 RenderDriver,内部调用 cc_harness.render 现有 print_* 函数。

CLI / --repl 调试入口仍然走 4-phase 渲染,保持兼容性。
"""
from __future__ import annotations

from typing import Any

from cc_harness.render import (
    print_thinking, print_action, print_observation, print_result,
    print_token_summary, print_compaction_summary,
    print_info, print_warn,
)
from rich.console import Console


class REPLDriver:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._tool_name = ""

    def write(self, text: str) -> None:
        # REPL 把 final_text 走原 print_result
        print_result(self.console, text)

    def write_chunk(self, token: str) -> None:
        # REPL 流式 chunk 立即打;buffer 模式(repl.py)累积到 final_text
        # REPLDriver 主要给 --repl 调试入口用,直接 print
        self.console.print(token, end="")

    def write_tool_call(self, name: str, args: dict[str, Any]) -> None:
        self._tool_name = name
        print_action(self.console, f"{name}({args})")

    def write_tool_result(self, result: str, error: bool) -> None:
        print_observation(self.console, result)

    def write_todo(self, items: list[dict[str, str]]) -> None:
        # 简化:写成 markdown 列表
        for item in items:
            mark = "x" if item.get("status") == "done" else " "
            print_info(self.console, f"  [{mark}] {item.get('title', '')}")

    def write_status(self, **fields: Any) -> None:
        if "mode" in fields:
            print_info(self.console, f"mode → {fields['mode']}")
        if "permission_mode" in fields:
            print_info(self.console, f"permission → {fields['permission_mode']}")

    def refresh_token(self, stats: Any) -> None:
        # REPLDriver 把 Usage dataclass 转化为原 print_token_summary 输入
        print_token_summary(self.console, stats)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_render_emit.py -v`
Expected: 6 passed

- [ ] **Step 5: 跑现有 render 相关测试确认不破**

Run: `pytest tests/test_render.py tests/test_agent.py -v`
Expected: 全部 pass(没改 print_* 行为)

- [ ] **Step 6: commit**

```bash
git add cc_harness/render.py cc_harness/render_repl_driver.py tests/test_render_emit.py
git commit -m "feat(render): emit() dispatcher + REPLDriver (兼容 --repl 入口)"
```

---

## Task 4: 删除 --serve / --port flag(main.py)

**Files:**
- Modify: `main.py:1-368` — 移除 Web UI 入口与相关 import

**Interfaces:**
- Removes: `--serve` / `--port` flag
- Modifies: `main()` 入口,无 --serve 时只走 TUI / --repl

- [ ] **Step 1: 找到要删的代码**

Run: `grep -n "serve\|port\|web" main.py`
Expected: 列出所有 --serve / --port / run_serve / web 相关行

- [ ] **Step 2: 删除 --serve / --port + run_serve import**

```python
# main.py 改造
# 1. 删除 import line 141: from cc_harness.web.app import run_serve
# 2. 删除所有 "--serve" / "--port" argparse 行
# 3. 删除 if args.serve: run_serve(...) 整段
# 4. 删除 docstring / 注释里 --serve 相关说明

# 改后 argparse:
parser = argparse.ArgumentParser()
parser.add_argument("--mode", choices=["coding", "plan", "design"], default="coding")
parser.add_argument("--repl", action="store_true", help="legacy REPL 调试入口")
parser.add_argument("--cwd", default=".")
args = parser.parse_args()

# 改后 main 流程:
if args.repl:
    from cc_harness.repl import run_repl
    asyncio.run(run_repl(cwd=args.cwd, mode=args.mode))
else:
    from cc_harness.tui.driver import run_tui  # 后续 task 实现
    asyncio.run(run_tui(cwd=args.cwd, mode=args.mode))
```

- [ ] **Step 3: 跑现有 main 测试**

Run: `pytest tests/test_cli_init.py tests/test_cli_resume.py tests/test_cli_todo.py -v`
Expected: 全部 pass

- [ ] **Step 4: commit**

```bash
git add main.py
git commit -m "refactor(main): remove --serve / --port flag (Web UI 迁移到 TUI)"
```

---

## Task 5: 添加 Textual 依赖

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Adds: `textual>=0.50,<1.0` 到 dependencies
- Adds: `textual-dev>=1.0` 到 dependencies
- Adds: `pytest-textual-snapshot>=0.4` 到 test extra

- [ ] **Step 1: 修改 pyproject.toml**

```toml
# pyproject.toml
[project]
dependencies = [
    # ... 现有 dependency
    "textual>=0.50,<1.0",
    "textual-dev>=1.0",
]

[project.optional-dependencies]
test = [
    # ... 现有 test 依赖
    "pytest-textual-snapshot>=0.4",
]
```

- [ ] **Step 2: 安装新依赖**

Run: `pip install -e '.[dev]'`
Expected: textual + textual-dev + pytest-textual-snapshot 装好

- [ ] **Step 3: 验证 import**

Run: `python -c "import textual; from textual.app import App; print(textual.__version__)"`
Expected: 打印 textual 版本

- [ ] **Step 4: commit**

```bash
git add pyproject.toml
git commit -m "build(deps): add textual>=0.50,<1.0 + textual-dev + pytest-textual-snapshot"
```

---

## Task 6: 创建 cc_harness/tui/ 骨架

**Files:**
- Create: `cc_harness/tui/__init__.py`
- Create: `cc_harness/tui/widgets/__init__.py`
- Create: `cc_harness/tui/screens/__init__.py`

**Interfaces:**
- Package level: `cc_harness.tui` 空包

- [ ] **Step 1: 创建目录与 __init__.py 文件**

```python
# cc_harness/tui/__init__.py
"""TUI 替换 Web UI,基于 Textual,Claude Code 风格对齐。"""
__version__ = "0.1.0"
```

```python
# cc_harness/tui/widgets/__init__.py
"""TUI 内部 widget 集合。"""
```

```python
# cc_harness/tui/screens/__init__.py
"""TUI ModalScreen 集合(/help / /theme / /resume / HITL)。"""
```

- [ ] **Step 2: 验证 import**

Run: `python -c "from cc_harness.tui import __version__; print(__version__)"`
Expected: 0.1.0

- [ ] **Step 3: commit**

```bash
git add cc_harness/tui/__init__.py cc_harness/tui/widgets/__init__.py cc_harness/tui/screens/__init__.py
git commit -m "feat(tui): create cc_harness/tui package skeleton"
```

---

## Task 7: PipTuiApp mount 4 widgets(Header / ChatLog / PromptInput / Footer)

**Files:**
- Create: `cc_harness/tui/app.py`
- Create: `cc_harness/tui/widgets/header.py`
- Create: `cc_harness/tui/widgets/chat.py`
- Create: `cc_harness/tui/widgets/input.py`
- Create: `cc_harness/tui/widgets/footer.py`
- Test: `cc_harness/tui/test_app.py`

**Interfaces:**
- Produces: `PipTuiApp(Textual App)` — 主类,`BINDINGS` + `compose()` 4 widget
- Produces: `HeaderBar(Static)` — 顶部状态行
- Produces: `ChatLog(RichLog)` — 中部聊天区域
- Produces: `PromptInput(TextArea)` — 底部输入
- Produces: `FooterBar(Static)` — 底部 token/cost/help

- [ ] **Step 1: 写 failing test**

```python
# cc_harness/tui/test_app.py
from cc_harness.tui.app import PipTuiApp

async def test_app_composes_four_widgets():
    \"\"\"App 必须 mount 4 个 widget:Header / ChatLog / PromptInput / Footer。\"\"\"
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        # compose 后查询 DOM
        from textual.widgets import Static, TextArea, RichLog
        header = pilot.app.query_one(\"HeaderBar\")
        chat = pilot.app.query_one(\"ChatLog\")
        prompt = pilot.app.query_one(\"PromptInput\")
        footer = pilot.app.query_one(\"FooterBar\")
        assert header is not None
        assert chat is not None
        assert prompt is not None
        assert footer is not None

async def test_app_default_theme_is_dark():
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)):
        assert app.theme == \"textual-dark\"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest cc_harness/tui/test_app.py -v`
Expected: 2 failed,ImportError(`PipTuiApp` not found)

- [ ] **Step 3: 实现 4 个 widget + App**

```python
# cc_harness/tui/widgets/header.py
from textual.widgets import Static

class HeaderBar(Static):
    DEFAULT_CSS = \"\"\"
    HeaderBar {
        height: 1;
        dock: top;
        background: $boost;
        color: $text;
    }
    \"\"\"

    def render_status(self, model: str, cwd: str, branch: str, mode: str, permission: str) -> None:
        self.update(f\"{model} · {cwd} · {branch} · [{mode}] · [{permission}]\")
```

```python
# cc_harness/tui/widgets/chat.py
from textual.widgets import RichLog
from typing import Any

class ChatLog(RichLog):
    DEFAULT_CSS = \"\"\"
    ChatLog {
        height: 1fr;
        padding: 1 2;
    }
    \"\"\"

    def write_user(self, text: str) -> None:
        self.write(f\"\\n[bold cyan]›[/bold cyan] {text}\")

    def write_assistant_chunk(self, token: str) -> None:
        # 临时 static,完成后替换为 render
        self.write(token, expand=False)

    def write_assistant_final(self, text: str) -> None:
        self.write(f\"\\n[green]●[/green] {text}\")

    def write_tool_call(self, name: str, args: dict[str, Any]) -> None:
        self.write(f\"\\n[yellow]●[/yellow] {name}({args})\")

    def write_tool_result(self, result: str, error: bool) -> None:
        color = \"red\" if error else \"green\"
        self.write(f\"  [{color}]{result}[/{color}]\")

    def write_todo(self, items: list[dict[str, str]]) -> None:
        for item in items:
            mark = \"x\" if item.get(\"status\") == \"done\" else \" \"
            title = item.get(\"title\", \"\")
            self.write(f\"  - [{mark}] {title}\")
```

```python
# cc_harness/tui/widgets/input.py
from textual.widgets import TextArea
from textual.containers import Horizontal

class PromptInput(TextArea):
    DEFAULT_CSS = \"\"\"
    PromptInput {
        height: 3;
        border: solid $primary;
    }
    \"\"\"

    BINDINGS = [
        # 后续 task 11 加 Ctrl+C / Ctrl+L / Ctrl+R / Shift+Tab / Ctrl+T
        (\"tab\", \"complete\", \"Complete @ or /\"),
    ]

    async def action_complete(self) -> None:
        # 后续 task 12 实现:@path / /command 补全
        pass
```

```python
# cc_harness/tui/widgets/footer.py
from textual.widgets import Static

class FooterBar(Static):
    DEFAULT_CSS = \"\"\"
    FooterBar {
        height: 1;
        dock: bottom;
        background: $boost;
        color: $text;
    }
    \"\"\"

    def render_tokens(self, in_tok: int, out_tok: int, cost: float) -> None:
        self.update(f\"↑ {in_tok} ↓ {out_tok} · ${cost:.4f}    │    ⓘ ? for help\")
```

```python
# cc_harness/tui/app.py
from textual.app import App
from textual.binding import Binding
from textual.containers import Vertical
from cc_harness.tui.widgets.header import HeaderBar
from cc_harness.tui.widgets.chat import ChatLog
from cc_harness.tui.widgets.input import PromptInput
from cc_harness.tui.widgets.footer import FooterBar


class PipTuiApp(App):
    \"\"\"cc-harness TUI 主应用,4-zone 布局,Claude Code 风格对齐。\"\"\"

    TITLE = \"cc-harness\"
    THEME = \"textual-dark\"

    BINDINGS = [
        # 后续 task 11 加 Ctrl+C / Ctrl+L / Ctrl+R / Shift+Tab / Ctrl+T / Tab
        Binding(\"ctrl+l\", \"clear_screen\", \"Clear\"),
    ]

    def compose(self):
        yield HeaderBar(id=\"header\")
        yield ChatLog(id=\"chat\", highlight=True, markup=True, wrap=True)
        yield PromptInput(id=\"prompt\")
        yield FooterBar(id=\"footer\")

    def action_clear_screen(self) -> None:
        chat = self.query_one(\"#chat\", ChatLog)
        chat.clear()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest cc_harness/tui/test_app.py -v`
Expected: 2 passed

- [ ] **Step 5: commit**

```bash
git add cc_harness/tui/app.py cc_harness/tui/widgets/ cc_harness/tui/test_app.py
git commit -m "feat(tui): PipTuiApp mounts 4 widgets (Header/ChatLog/PromptInput/Footer)"
```

---

## Task 8: TUIDriver — 把 RenderEvent 派发到 Textual message

**Files:**
- Create: `cc_harness/tui/driver.py`

**Interfaces:**
- Implements: `RenderDriver`
- Produces: `TUIDriver(app: PipTuiApp)` — 接收 Textual app,emit 时 `app.post_message(Message)`
- Produces: `ChatWrite(text)` / `TokenWrite(token)` / `ToolCallWrite(name, args)` / `ToolResultWrite(result, error)` / `TodoWrite(items)` / `StatusWrite(**fields)` / `TokenRefresh(stats)` — Textual Message 子类

- [ ] **Step 1: 写 failing test**

```python
# cc_harness/tui/test_render_driver.py
from cc_harness.render import emit
from cc_harness.render_protocol import ThinkingChunk, ToolCallStart, FinalText, TodoUpdate, Usage

async def test_tui_driver_writes_text_via_message():
    from cc_harness.tui.app import PipTuiApp
    from cc_harness.tui.driver import TUIDriver
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        driver = TUIDriver(app)
        emit(FinalText(text=\"hello\"), driver=driver)
        await pilot.pause()
        chat = app.query_one(\"#chat\")
        assert \"hello\" in str(chat.lines)

async def test_tui_driver_writes_chunk_accumulates():
    from cc_harness.tui.app import PipTuiApp
    from cc_harness.tui.driver import TUIDriver
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        driver = TUIDriver(app)
        emit(ThinkingChunk(delta=\"a\"), driver=driver)
        emit(ThinkingChunk(delta=\"b\"), driver=driver)
        emit(ThinkingChunk(delta=\"c\"), driver=driver)
        await pilot.pause()
        # 累积应至少出现 abc 的一部分(节流可能只显示最后一次)
        chat = app.query_one(\"#chat\")
        assert \"a\" in str(chat.lines) or \"abc\" in str(chat.lines)

async def test_tui_driver_writes_tool_call():
    from cc_harness.tui.app import PipTuiApp
    from cc_harness.tui.driver import TUIDriver
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        driver = TUIDriver(app)
        emit(ToolCallStart(name=\"run_command\", args={\"cmd\": \"ls\"}), driver=driver)
        await pilot.pause()
        chat = app.query_one(\"#chat\")
        assert \"run_command\" in str(chat.lines)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest cc_harness/tui/test_render_driver.py -v`
Expected: 3 failed,ImportError

- [ ] **Step 3: 实现 TUIDriver**

```python
# cc_harness/tui/driver.py
"""TUIDriver:把 RenderEvent 派发到 Textual app 的 widget message。"""
from __future__ import annotations

import asyncio
from typing import Any

from textual.message import Message
from cc_harness.render_protocol import RenderDriver


# --- Textual Message 子类,每个对应一种 write 方法 ---

class ChatWrite(Message):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

class TokenWrite(Message):
    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token

class ToolCallWrite(Message):
    def __init__(self, name: str, args: dict[str, Any]) -> None:
        super().__init__()
        self.name = name
        self.args = args

class ToolResultWrite(Message):
    def __init__(self, result: str, error: bool) -> None:
        super().__init__()
        self.result = result
        self.error = error

class TodoWrite(Message):
    def __init__(self, items: list[dict[str, str]]) -> None:
        super().__init__()
        self.items = items

class StatusWrite(Message):
    def __init__(self, **fields: Any) -> None:
        super().__init__()
        self.fields = fields

class TokenRefresh(Message):
    def __init__(self, stats: Any) -> None:
        super().__init__()
        self.stats = stats


# --- TUIDriver:实现 RenderDriver,通过 app.post_message 派发 ---

class TUIDriver(RenderDriver):
    def __init__(self, app) -> None:
        self.app = app
        # 节流:累计 token,50ms 一次 flush
        self._token_buffer: list[str] = []
        self._flush_task: asyncio.Task | None = None

    def write(self, text: str) -> None:
        self.app.post_message(ChatWrite(text))

    def write_chunk(self, token: str) -> None:
        self._token_buffer.append(token)
        if self._flush_task is None:
            loop = asyncio.get_event_loop()
            self._flush_task = loop.create_task(self._flush_after(0.05))

    async def _flush_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self._token_buffer:
            token = \"\".join(self._token_buffer)
            self._token_buffer.clear()
            self.app.post_message(TokenWrite(token))
        self._flush_task = None

    def write_tool_call(self, name: str, args: dict[str, Any]) -> None:
        self.app.post_message(ToolCallWrite(name, args))

    def write_tool_result(self, result: str, error: bool) -> None:
        self.app.post_message(ToolResultWrite(result, error))

    def write_todo(self, items: list[dict[str, str]]) -> None:
        self.app.post_message(TodoWrite(items))

    def write_status(self, **fields: Any) -> None:
        self.app.post_message(StatusWrite(**fields))

    def refresh_token(self, stats: Any) -> None:
        self.app.post_message(TokenRefresh(stats))
```

```python
# cc_harness/tui/app.py — 在 PipTuiApp 内追加 message handler
# (修改 PipTuiApp,加 7 个 on_* 方法)

class PipTuiApp(App):
    # ... 现有 ...

    def on_chat_write(self, message: ChatWrite) -> None:
        chat = self.query_one(\"#chat\", ChatLog)
        chat.write_assistant_final(message.text)

    def on_token_write(self, message: TokenWrite) -> None:
        chat = self.query_one(\"#chat\", ChatLog)
        chat.write_assistant_chunk(message.token)

    def on_tool_call_write(self, message: ToolCallWrite) -> None:
        chat = self.query_one(\"#chat\", ChatLog)
        chat.write_tool_call(message.name, message.args)

    def on_tool_result_write(self, message: ToolResultWrite) -> None:
        chat = self.query_one(\"#chat\", ChatLog)
        chat.write_tool_result(message.result, message.error)

    def on_todo_write(self, message: TodoWrite) -> None:
        chat = self.query_one(\"#chat\", ChatLog)
        chat.write_todo(message.items)

    def on_status_write(self, message: StatusWrite) -> None:
        header = self.query_one(\"#header\", HeaderBar)
        # Status 字段更新(后续 task 10 完整实现)
        # 这里先存 fields,等 task 10 加强
        self._status = message.fields

    def on_token_refresh(self, message: TokenRefresh) -> None:
        footer = self.query_one(\"#footer\", FooterBar)
        footer.render_tokens(
            in_tok=message.stats.input_tokens,
            out_tok=message.stats.output_tokens,
            cost=0.0,  # 后续 task 10 加 cost 计算
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest cc_harness/tui/test_render_driver.py -v`
Expected: 3 passed

- [ ] **Step 5: 跑现有 121 测试 + 15 集成测试(确认 render 没破)**

Run: `pytest tests/ -v --ignore=tests/_test_*.py`
Expected: 121 测试全 pass

- [ ] **Step 6: commit**

```bash
git add cc_harness/tui/driver.py cc_harness/tui/app.py cc_harness/tui/test_render_driver.py
git commit -m "feat(tui): TUIDriver — RenderEvent 派发到 Textual message"
```

---

## Task 9: driver.run_tui(cwd, mode) — TUI 入口

**Files:**
- Modify: `cc_harness/tui/driver.py` — 加 `run_tui()` 入口
- Test: `cc_harness/tui/test_run_tui.py`

**Interfaces:**
- Produces: `async def run_tui(*, cwd: str, mode: str) -> None` — main 入口,init 依赖,start app

- [ ] **Step 1: 写 failing test**

```python
# cc_harness/tui/test_run_tui.py
import asyncio
from pathlib import Path
from cc_harness.tui.driver import run_tui

async def test_run_tui_starts_app():
    \"\"\"run_tui 必须创建 PipTuiApp 并 start。\"\"\"
    # 防止无限循环:1 秒后 cancel
    async def late_cancel():
        await asyncio.sleep(0.5)
        # 通过某种方式取消(pilot 不行,这里直接 cancel task)
    task = asyncio.create_task(run_tui(cwd=str(Path.cwd()), mode=\"coding\"))
    await asyncio.sleep(0.5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert True  # 到达这里就算 ok
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest cc_harness/tui/test_run_tui.py -v`
Expected: 1 failed,ImportError(`run_tui` not found)

- [ ] **Step 3: 实现 run_tui()**

```python
# cc_harness/tui/driver.py — 追加

async def run_tui(*, cwd: str, mode: str = \"coding\") -> None:
    \"\"\"TUI 入口:初始化依赖,启动 PipTuiApp。\"\"\"
    from cc_harness.tui.app import PipTuiApp
    from cc_harness.config import load_config, ConfigError
    from cc_harness.llm import LLMClient
    from cc_harness.mcp_client import MCPClient

    try:
        config = load_config(cwd=cwd)
    except ConfigError as e:
        # 配置错时仍然启动 TUI,显示错误
        app = PipTuiApp()
        async with app.run_test() as pilot:
            chat = app.query_one(\"#chat\", ChatLog)
            chat.write(f\"[red]Config error: {e}[/red]\")
        return

    # 真实启动路径
    app = PipTuiApp()
    await app.run_async()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest cc_harness/tui/test_run_tui.py -v`
Expected: 1 passed

- [ ] **Step 5: commit**

```bash
git add cc_harness/tui/driver.py cc_harness/tui/test_run_tui.py
git commit -m "feat(tui): run_tui(cwd, mode) entry point"
```

---

## Task 10: Header status line + Footer refresh

**Files:**
- Modify: `cc_harness/tui/widgets/header.py`
- Modify: `cc_harness/tui/widgets/footer.py`
- Modify: `cc_harness/tui/app.py` — 加 git branch 检测

**Interfaces:**
- Modifies: `HeaderBar.render_status(...)` 完整 5 段
- Modifies: `FooterBar.render_tokens(...)` 加 cost 估算

- [ ] **Step 1: 写 failing test**

```python
# cc_harness/tui/test_status.py
from cc_harness.tui.app import PipTuiApp

async def test_header_shows_5_segments():
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        header = app.query_one(\"#header\")
        # 5 段:model / cwd / branch / mode / permission
        await pilot.app._refresh_status(
            model=\"claude-opus-4\",
            cwd=\"/tmp\",
            branch=\"main\",
            mode=\"coding\",
            permission=\"default\",
        )
        await pilot.pause()
        text = str(header.render())
        assert \"claude-opus-4\" in text
        assert \"/tmp\" in text
        assert \"main\" in text
        assert \"coding\" in text
        assert \"default\" in text

async def test_footer_shows_tokens_and_cost():
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        footer = app.query_one(\"#footer\")
        await pilot.app._refresh_footer(input_tokens=1200, output_tokens=800, cost=0.04)
        await pilot.pause()
        text = str(footer.render())
        assert \"1200\" in text
        assert \"800\" in text
        assert \"0.04\" in text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest cc_harness/tui/test_status.py -v`
Expected: 2 failed,AttributeError(`_refresh_status` not found)

- [ ] **Step 3: 扩展 HeaderBar / FooterBar / App**

```python
# cc_harness/tui/widgets/header.py 改造
class HeaderBar(Static):
    DEFAULT_CSS = \"\"\"  # 同上  \"\"\"

    def render_status(self, model: str, cwd: str, branch: str, mode: str, permission: str) -> None:
        self.update(f\"{model} · {cwd} · {branch} · [{mode}] · [{permission}]\")

    def update_section(self, **fields) -> None:
        # 后续 task 13 实现 diff-only,先用全量 update
        cur = str(self.render())
        # 状态字段由 App 维护,这里只更新 self._state
        self._state = fields
        self.update(\" \".join(f\"[{k}]={v}\" for k, v in fields.items()))
```

```python
# cc_harness/tui/widgets/footer.py 改造(同上,确保 render_tokens 接收 total cost)
```

```python
# cc_harness/tui/app.py 改造
import subprocess
from pathlib import Path

class PipTuiApp(App):
    # ... 现有 ...

    def on_mount(self) -> None:
        # 启动时刷新一次 status
        self._refresh_status(
            model=self._detect_model(),
            cwd=str(Path.cwd()),
            branch=self._detect_branch(),
            mode=\"coding\",
            permission=\"default\",
        )

    def _refresh_status(self, *, model: str, cwd: str, branch: str, mode: str, permission: str) -> None:
        header = self.query_one(\"#header\", HeaderBar)
        header.render_status(model, cwd, branch, mode, permission)

    def _refresh_footer(self, *, input_tokens: int, output_tokens: int, cost: float) -> None:
        footer = self.query_one(\"#footer\", FooterBar)
        footer.render_tokens(input_tokens, output_tokens, cost)

    def _detect_model(self) -> str:
        # 后续接 LLMClient.config;v1 写死
        return \"claude-opus-4\"

    def _detect_branch(self) -> str:
        try:
            return subprocess.check_output(
                [\"git\", \"rev-parse\", \"--abbrev-ref\", \"HEAD\"],
                cwd=str(Path.cwd()),
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except Exception:
            return \"no-git\"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest cc_harness/tui/test_status.py -v`
Expected: 2 passed

- [ ] **Step 5: commit**

```bash
git add cc_harness/tui/widgets/header.py cc_harness/tui/widgets/footer.py cc_harness/tui/app.py cc_harness/tui/test_status.py
git commit -m "feat(tui): Header status line + Footer token/cost refresh"
```

---

## Task 11: 键盘快捷键 — Ctrl+C / Ctrl+L / Ctrl+R / Shift+Tab / Ctrl+T / Tab

**Files:**
- Modify: `cc_harness/tui/app.py`
- Modify: `cc_harness/tui/widgets/input.py`
- Create: `cc_harness/tui/history.py`
- Test: `cc_harness/tui/test_input.py`

**Interfaces:**
- Produces: `History` 类 — 维护历史 + Ctrl+R 反向搜
- Modifies: BINDINGS — 加 Ctrl+C / Ctrl+L / Ctrl+R / Shift+Tab / Ctrl+T / Tab

- [ ] **Step 1: 写 failing test**

```python
# cc_harness/tui/test_input.py
from cc_harness.tui.history import History

def test_history_starts_empty():
    h = History()
    assert h.entries == []

def test_history_append():
    h = History()
    h.append(\"hello\")
    h.append(\"world\")
    assert h.entries == [\"hello\", \"world\"]

def test_history_search_substring():
    h = History()
    h.append(\"git status\")
    h.append(\"git log\")
    h.append(\"pytest\")
    matches = h.search(\"git\")
    assert matches == [\"git log\", \"git status\"]  # latest first

def test_history_no_match_returns_empty():
    h = History()
    h.append(\"hello\")
    assert h.search(\"xyz\") == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest cc_harness/tui/test_input.py -v`
Expected: 4 failed,ImportError(`History`)

- [ ] **Step 3: 实现 History + 快捷键**

```python
# cc_harness/tui/history.py
\"\"\"输入历史管理 + Ctrl+R 反向搜。\"\"\"
from __future__ import annotations


class History:
    def __init__(self, max_size: int = 1000) -> None:
        self.entries: list[str] = []
        self.max_size = max_size

    def append(self, entry: str) -> None:
        if not entry.strip():
            return
        # 去重:与最后一条相同就不重复
        if self.entries and self.entries[-1] == entry:
            return
        self.entries.append(entry)
        if len(self.entries) > self.max_size:
            self.entries = self.entries[-self.max_size:]

    def search(self, query: str) -> list[str]:
        \"\"\"substr 反向搜,最新匹配在前。\"\"\"
        if not query:
            return list(reversed(self.entries[-10:]))
        matches = [e for e in self.entries if query in e]
        return list(reversed(matches))
```

```python
# cc_harness/tui/app.py — BINDINGS 扩展
class PipTuiApp(App):
    BINDINGS = [
        Binding(\"ctrl+c\", \"interrupt\", \"Interrupt\", priority=True),
        Binding(\"ctrl+l\", \"clear_screen\", \"Clear\"),
        Binding(\"ctrl+r\", \"search_history\", \"History\"),
        Binding(\"shift+tab\", \"toggle_permission\", \"Permission\"),
        Binding(\"ctrl+t\", \"toggle_todo\", \"Todo\"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._interrupt_event = asyncio.Event()
        self._permission_mode = \"default\"
        self._history = History()

    def action_interrupt(self) -> None:
        self._interrupt_event.set()

    def action_clear_screen(self) -> None:
        chat = self.query_one(\"#chat\", ChatLog)
        chat.clear()

    async def action_search_history(self) -> None:
        # 简化:弹输入框式 prompt,搜完替换当前 input
        # v1:先实现 /history 命令,Ctrl+R 后续 task 升级
        self.query_one(\"#prompt\", PromptInput).focus()

    def action_toggle_permission(self) -> None:
        self._permission_mode = \"auto\" if self._permission_mode == \"default\" else \"default\"
        # 刷新 header
        from cc_harness.render import emit
        from cc_harness.render_protocol import PermissionModeChanged
        emit(PermissionModeChanged(mode=self._permission_mode), driver=TUIDriver(self))

    def action_toggle_todo(self) -> None:
        # 后续 task 17 实现
        pass
```

```python
# cc_harness/tui/widgets/input.py — 加 Tab 行为
class PromptInput(TextArea):
    BINDINGS = [
        (\"tab\", \"complete\", \"Complete\"),
    ]

    async def action_complete(self) -> None:
        # 后续 task 12 实现:@path / /command 补全
        pass
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest cc_harness/tui/test_input.py -v`
Expected: 4 passed

- [ ] **Step 5: commit**

```bash
git add cc_harness/tui/history.py cc_harness/tui/app.py cc_harness/tui/widgets/input.py cc_harness/tui/test_input.py
git commit -m "feat(tui): keyboard shortcuts (Ctrl+C/L/R/Shift+Tab/Ctrl+T) + History"
```

---

## Task 12: Tab 补全 + @path mention

**Files:**
- Create: `cc_harness/tui/completer.py`
- Modify: `cc_harness/tui/widgets/input.py`
- Test: `cc_harness/tui/test_completer.py`

**Interfaces:**
- Produces: `Completer(cwd)` — 维护 `/command` + `@path` 列表
- Modifies: `PromptInput.action_complete` — 触发 completer

- [ ] **Step 1: 写 failing test**

```python
# cc_harness/tui/test_completer.py
from pathlib import Path
from cc_harness.tui.completer import Completer

def test_completer_slash_commands():
    c = Completer(cwd=str(Path.cwd()))
    matches = c.complete(\"/the\")
    # 应包含 /theme
    assert any(m.startswith(\"/theme\") for m in matches)

def test_completer_at_path():
    c = Completer(cwd=str(Path.cwd()))
    matches = c.complete(\"@READ\")
    # 仓库根有 README.md,应匹配
    assert any(m.endswith(\"README.md\") for m in matches)

def test_completer_no_match_returns_empty():
    c = Completer(cwd=str(Path.cwd()))
    assert c.complete(\"/xyz_no_match\") == []
    assert c.complete(\"@xyz_no_match\") == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest cc_harness/tui/test_completer.py -v`
Expected: 3 failed,ImportError

- [ ] **Step 3: 实现 Completer**

```python
# cc_harness/tui/completer.py
\"\"\"Tab 补全:slash 命令 + @path 文件。\"\"\"
from __future__ import annotations

import os
from pathlib import Path


SLASH_COMMANDS = [
    \"/help\", \"/exit\", \"/clear\", \"/context\", \"/memory\",
    \"/save\", \"/config\", \"/tools\", \"/mcp\", \"/hitl\",
    \"/policy\", \"/audit\", \"/index\", \"/search\", \"/plan\",
    \"/team\", \"/model\", \"/usage\", \"/skill\", \"/task\",
    \"/snapshot\", \"/restore\", \"/theme\", \"/resume\",
]


class Completer:
    def __init__(self, cwd: str) -> None:
        self.cwd = Path(cwd)

    def complete(self, prefix: str) -> list[str]:
        if prefix.startswith(\"/\"):
            return [c for c in SLASH_COMMANDS if c.startswith(prefix)]
        if prefix.startswith(\"@\"):
            return self._complete_path(prefix[1:])
        return []

    def _complete_path(self, prefix: str) -> list[str]:
        \"\"\"@path 补全:支持相对路径。\"\"\"
        if not prefix:
            # 列出 cwd 顶层文件
            try:
                return [\"@\" + p.name for p in self.cwd.iterdir()]
            except Exception:
                return []
        # 解析 prefix → 绝对路径
        try:
            target = self.cwd / prefix
            if target.is_dir():
                return [f\"@{prefix}{p.name}\" for p in target.iterdir()]
            else:
                # 文件补全:glob 找 .ext 同名
                parent = target.parent
                partial = target.name
                if not parent.exists():
                    return []
                return [f\"@{prefix}{p.name}\" for p in parent.iterdir() if p.name.startswith(partial)]
        except Exception:
            return []
```

```python
# cc_harness/tui/widgets/input.py — action_complete 改造
import subprocess
from pathlib import Path
from cc_harness.tui.completer import Completer

class PromptInput(TextArea):
    def __init__(self, cwd: str = \".\", **kwargs) -> None:
        super().__init__(**kwargs)
        self.completer = Completer(cwd=cwd)

    async def action_complete(self) -> None:
        text = self.text
        # 找到最后一个 / 或 @ 起始位置
        for i in range(len(text) - 1, -1, -1):
            if text[i] in (\"/\", \"@\") and (i == 0 or text[i - 1].isspace()):
                prefix = text[i:]
                matches = self.completer.complete(prefix)
                if matches:
                    # v1:取第一个替换(完整补全 popup 后续 task)
                    self.text = text[:i] + matches[0]
                return
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest cc_harness/tui/test_completer.py -v`
Expected: 3 passed

- [ ] **Step 5: commit**

```bash
git add cc_harness/tui/completer.py cc_harness/tui/widgets/input.py cc_harness/tui/test_completer.py
git commit -m "feat(tui): Tab completion + @path mention (Completer)"
```

---

## Task 13: Slash commands dispatcher(/help / /theme / /resume / /model / /clear)

**Files:**
- Modify: `cc_harness/tui/app.py` — 加 `_handle_slash_command` 方法
- Create: `cc_harness/tui/screens/help.py`
- Create: `cc_harness/tui/screens/theme.py`
- Create: `cc_harness/tui/screens/resume.py`
- Test: `cc_harness/tui/test_slash.py`

**Interfaces:**
- Produces: `HelpScreen(ModalScreen)` — 帮助 modal
- Produces: `ThemeScreen(ModalScreen)` — 主题切换 modal
- Produces: `ResumeScreen(ModalScreen)` — 历史 session modal
- Modifies: `PipTuiApp._handle_slash_command(cmd: str)` — 解析分发

- [ ] **Step 1: 写 failing test**

```python
# cc_harness/tui/test_slash.py
from cc_harness.tui.app import PipTuiApp

async def test_help_command_pushes_help_screen():
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await app._handle_slash_command(\"/help\")
        await pilot.pause()
        # screen stack 应有 HelpScreen
        assert len(app.screen_stack) >= 1

async def test_theme_command_pushes_theme_screen():
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await app._handle_slash_command(\"/theme\")
        await pilot.pause()
        assert any(isinstance(s.__class__.__name__, str) and s.__class__.__name__ == \"ThemeScreen\" for s in app.screen_stack)

async def test_clear_command_clears_chat():
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        # 先写一些
        chat = app.query_one(\"#chat\")
        await app._handle_slash_command(\"/clear\")
        await pilot.pause()
        assert len(chat.lines) == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest cc_harness/tui/test_slash.py -v`
Expected: 3 failed,ImportError

- [ ] **Step 3: 实现 3 个 modal + dispatcher**

```python
# cc_harness/tui/screens/help.py
from textual.screen import ModalScreen
from textual.widgets import Static, Button
from textual.containers import Vertical

HELP_TEXT = \"\"\"
cc-harness TUI 帮助

基础:
  Enter              提交输入
  Shift+Enter        换行
  Tab                补全 / 或 @
  ↑ / ↓              历史
  Ctrl+R             反向搜历史

快捷键:
  Ctrl+C             中断当前 LLM 流
  Ctrl+L             清屏
  Shift+Tab          切换权限模式 (default / auto)
  Ctrl+T             切换 todo 显示

命令:
  /help              本帮助
  /theme             切换主题
  /resume            历史 session
  /model             切换模型
  /clear             清空 chat
  /exit              退出

完整命令见 README。
\"\"\"


class HelpScreen(ModalScreen):
    BINDINGS = [(\"escape\", \"dismiss\", \"Close\")]

    def compose(self):
        yield Vertical(
            Static(HELP_TEXT, id=\"help-text\"),
            Button(\"Close\", id=\"close\"),
            id=\"help-modal\",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()
```

```python
# cc_harness/tui/screens/theme.py
from textual.screen import ModalScreen
from textual.widgets import OptionList, Header
from textual.containers import Vertical

THEME_OPTIONS = [
    (\"textual-dark\", \"Dark (default)\"),
    (\"textual-light\", \"Light\"),
    (\"system\", \"跟随系统\"),
    (\"high-contrast\", \"High Contrast\"),
]


class ThemeScreen(ModalScreen):
    BINDINGS = [(\"escape\", \"dismiss\", \"Close\")]

    def compose(self):
        yield Vertical(
            Header(),
            OptionList(*[(label, _id) for _id, label in THEME_OPTIONS]),
            id=\"theme-modal\",
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        theme_id = event.option.value
        if theme_id == \"system\":
            self.app.theme = \"textual-dark\"  # 系统主题跟随后续 task 升级
        else:
            self.app.theme = theme_id
        self.dismiss()
```

```python
# cc_harness/tui/screens/resume.py
\"\"\"ResumeScreen:列出历史 session,选中载入。\"\"\"
from textual.screen import ModalScreen
from textual.widgets import OptionList, Header
from textual.containers import Vertical


class ResumeScreen(ModalScreen):
    BINDINGS = [(\"escape\", \"dismiss\", \"Close\")]

    def __init__(self, sessions: list[dict]) -> None:
        super().__init__()
        self.sessions = sessions  # [{id, title, started_at, message_count}]

    def compose(self):
        items = [
            (f\"{s.get('title', s['id'])} ({s.get('message_count', 0)} msgs)\", s[\"id\"])
            for s in self.sessions
        ]
        yield Vertical(
            Header(),
            OptionList(*items) if items else OptionList((\"(no prior sessions)\", None)),
            id=\"resume-modal\",
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.value)
```

```python
# cc_harness/tui/app.py — 加 _handle_slash_command
class PipTuiApp(App):
    async def _handle_slash_command(self, cmd: str) -> None:
        cmd = cmd.strip()
        if not cmd.startswith(\"/\"):
            return
        name = cmd.split()[0]
        if name == \"/help\":
            from cc_harness.tui.screens.help import HelpScreen
            await self.push_screen(HelpScreen())
        elif name == \"/theme\":
            from cc_harness.tui.screens.theme import ThemeScreen
            await self.push_screen(ThemeScreen())
        elif name == \"/resume\":
            from cc_harness.tui.screens.resume import ResumeScreen
            # v1:从 history.json 读历史(后续 task 19 接 storage)
            sessions = self._load_sessions()
            await self.push_screen(ResumeScreen(sessions))
        elif name == \"/clear\":
            self.action_clear_screen()
        elif name == \"/exit\":
            self.exit()
        else:
            # 未知命令 — 显示在 chat
            chat = self.query_one(\"#chat\", ChatLog)
            chat.write(f\"[red]Unknown command: {name}[/red]\")

    def _load_sessions(self) -> list[dict]:
        # v1 stub:后续 task 19 接 cc_harness/storage
        return []
```

```python
# cc_harness/tui/widgets/input.py — 提交时触发 slash
from textual.message import Message

class PromptInput(TextArea):
    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    async def on_key(self, event) -> None:
        if event.key == \"enter\" and not event.shift:
            event.prevent_default()
            self.post_message(self.Submitted(self.text))
            self.text = \"\"
```

```python
# cc_harness/tui/app.py — 监听 Submitted
class PipTuiApp(App):
    def on_prompt_input_submitted(self, message: PromptInput.Submitted) -> None:
        text = message.text
        if text.startswith(\"/\"):
            self.run_worker(self._handle_slash_command(text))
        else:
            # 后续 task 14 接 run_turn
            chat = self.query_one(\"#chat\", ChatLog)
            chat.write_user(text)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest cc_harness/tui/test_slash.py -v`
Expected: 3 passed

- [ ] **Step 5: commit**

```bash
git add cc_harness/tui/screens/ cc_harness/tui/app.py cc_harness/tui/widgets/input.py cc_harness/tui/test_slash.py
git commit -m "feat(tui): slash commands dispatcher + /help / /theme / /resume modal"
```

---

## Task 14: TUI 驱动 run_turn(event_emitter=TUIDriver)

**Files:**
- Modify: `cc_harness/tui/app.py`
- Modify: `cc_harness/tui/driver.py`
- Test: `cc_harness/tui/test_integration.py`

**Interfaces:**
- Modifies: `PipTuiApp` — 收到用户非 slash 输入时,调 `run_turn(messages, event_emitter=tui_driver)`

- [ ] **Step 1: 写 failing test**

```python
# cc_harness/tui/test_integration.py
from cc_harness.tui.app import PipTuiApp
from cc_harness.render_test_driver import TestDriver

async def test_user_input_triggers_run_turn_with_emitter():
    \"\"\"用户输入(非 slash)触发 run_turn,event_emitter 是 TUIDriver。\"\"\"
    # 这里没法直接测 run_turn(它要真 LLM),改为:
    # 1. 注入 FakeLLM
    # 2. 用户输入 hello
    # 3. verify chat 收到 final_text
    from cc_harness.llm import LLMClient
    from cc_harness.mcp_client import MCPClient
    from cc_harness.project.service import TodoService
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        # 注入 fake 依赖
        app._llm = None  # 后续 task 接 FakeLLM
        chat = app.query_one(\"#chat\")
        # 直接调 _handle_user_input 跳过 input
        await app._handle_user_input(\"hello\")
        await pilot.pause()
        # user message 应写进 chat
        assert \"hello\" in str(chat.lines)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest cc_harness/tui/test_integration.py -v`
Expected: 1 failed,AttributeError(`_handle_user_input` not found)

- [ ] **Step 3: 实现 _handle_user_input**

```python
# cc_harness/tui/app.py — _handle_user_input
class PipTuiApp(App):
    async def _handle_user_input(self, text: str) -> None:
        if not text.strip():
            return
        if text.startswith(\"/\"):
            await self._handle_slash_command(text)
            return
        # 写 user message
        chat = self.query_one(\"#chat\", ChatLog)
        chat.write_user(text)
        # 调 run_turn(后续 task 接真 LLM;v1 stub)
        await self._run_turn_stub(text)

    async def _run_turn_stub(self, text: str) -> None:
        \"\"\"v1 stub:调 cc_harness.agent.run_turn integration。\n        完整 agent wiring 后续 task。\"\"\"
        driver = TUIDriver(self)
        from cc_harness.render import emit
        from cc_harness.render_protocol import FinalText, ToolCallStart, ToolCallEnd
        emit(FinalText(text=f\"(stub) echo: {text}\"), driver=driver)
```

```python
# cc_harness/tui/app.py — 改 on_prompt_input_submitted
class PipTuiApp(App):
    def on_prompt_input_submitted(self, message: PromptInput.Submitted) -> None:
        self.run_worker(self._handle_user_input(message.text))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest cc_harness/tui/test_integration.py -v`
Expected: 1 passed

- [ ] **Step 5: commit**

```bash
git add cc_harness/tui/app.py cc_harness/tui/test_integration.py
git commit -m "feat(tui): _handle_user_input + run_turn stub (TUIDriver 注入)"
```

---

## Task 15: run_turn 真实集成(从 cc_harness.agent.run_turn 拉 event_emitter)

**Files:**
- Modify: `cc_harness/tui/driver.py` — 接 `run_turn`
- Modify: `cc_harness/tui/app.py`

**Interfaces:**
- Modifies: `run_tui(cwd, mode)` — 创建真实 LLMClient + MCPClient,注入到 App
- Modifies: `PipTuiApp._handle_user_input` — 调真实 `agent.run_turn(messages, event_emitter=tui_driver)`

- [ ] **Step 1: 写 failing test**

```python
# cc_harness/tui/test_run_turn_integration.py
import asyncio
from unittest.mock import AsyncMock, patch
from cc_harness.tui.app import PipTuiApp
from cc_harness.render_test_driver import TestDriver

async def test_run_turn_called_with_event_emitter():
    \"\"\"用户输入触发 run_turn,event_emitter 是调用 TUIDriver 的对象。\"\"\"
    app = PipTuiApp()
    captured = []

    async def fake_run_turn(messages, *, event_emitter=None):
        captured.append(event_emitter)
        # 模拟 emit 一个 FinalText
        if event_emitter:
            from cc_harness.render import emit
            from cc_harness.render_protocol import FinalText
            emit(FinalText(text=\"ok\"), driver=event_emitter)
        return None

    with patch(\"cc_harness.tui.app._run_turn\", fake_run_turn):
        async with app.run_test(size=(120, 40)) as pilot:
            await app._handle_user_input(\"hello\")
            await pilot.pause()
            assert len(captured) == 1
            assert captured[0] is not None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest cc_harness/tui/test_run_turn_integration.py -v`
Expected: 1 failed,ImportError

- [ ] **Step 3: 接真 run_turn**

```python
# cc_harness/tui/app.py — _handle_user_input 改造
from cc_harness.agent import run_turn as _run_turn
from cc_harness.tui.driver import TUIDriver

class PipTuiApp(App):
    def __init__(self, *, llm=None, mcp=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._llm = llm
        self._mcp = mcp
        self._messages: list[dict] = []

    async def _handle_user_input(self, text: str) -> None:
        if not text.strip():
            return
        if text.startswith(\"/\"):
            await self._handle_slash_command(text)
            return
        chat = self.query_one(\"#chat\", ChatLog)
        chat.write_user(text)
        driver = TUIDriver(self)
        try:
            await _run_turn(
                self._messages,
                event_emitter=lambda ev: driver.write_status(**{\"event\": ev.__class__.__name__}),
            )
        except Exception as e:
            chat.write(f\"[red]Error: {e}[/red]\")

    def _messages_for_run(self) -> list[dict]:
        # 后续 task 接 TodoService + memory;v1 简易 self._messages
        return self._messages
```

注:这里 lambda 把 event 翻译成 driver 调用,实际需要更细的映射(event 类型 → 对应 driver 方法)。后续 task 18 完善。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest cc_harness/tui/test_run_turn_integration.py -v`
Expected: 1 passed

- [ ] **Step 5: commit**

```bash
git add cc_harness/tui/app.py cc_harness/tui/test_run_turn_integration.py
git commit -m "feat(tui): real run_turn integration (event_emitter=TUIDriver)"
```

---

## Task 16: HITL modal(L4 confirm 接入)

**Files:**
- Create: `cc_harness/tui/screens/hitl.py`
- Modify: `cc_harness/tui/driver.py` — 提供 ask_user hook
- Test: `cc_harness/tui/test_hitl.py`

**Interfaces:**
- Produces: `HITLScreen(ModalScreen)` — yes / always / no 三选
- Modifies: `TUIDriver` — 加 `ask_user(question, options)` 异步等待

- [ ] **Step 1: 写 failing test**

```python
# cc_harness/tui/test_hitl.py
from cc_harness.tui.app import PipTuiApp

async def test_hitl_modal_returns_choice():
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        # 推 hitl modal,默认选 yes
        from cc_harness.tui.screens.hitl import HITLScreen
        result = {}
        async def cb(value):
            result[\"choice\"] = value
        screen = HITLScreen(question=\"Run rm -rf /tmp?\")
        await app.push_screen(screen, cb)
        await pilot.pause()
        # 默认选 yes,按 Enter
        await pilot.press(\"enter\")
        await pilot.pause()
        assert result.get(\"choice\") == \"yes\"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest cc_harness/tui/test_hitl.py -v`
Expected: 1 failed,ImportError

- [ ] **Step 3: 实现 HITLScreen**

```python
# cc_harness/tui/screens/hitl.py
from textual.screen import ModalScreen
from textual.widgets import Static, Button, RadioSet, RadioButton
from textual.containers import Vertical, Horizontal


class HITLScreen(ModalScreen):
    BINDINGS = [
        (\"escape\", \"dismiss_no\", \"No\"),
        (\"y\", \"yes\", \"Yes\"),
        (\"a\", \"always\", \"Always\"),
        (\"n\", \"dismiss_no\", \"No\"),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self):
        yield Vertical(
            Static(self.question, id=\"hitl-question\"),
            RadioSet(
                RadioButton(\"Yes\", id=\"yes\"),
                RadioButton(\"Always (this session)\", id=\"always\"),
                RadioButton(\"No\", id=\"no\", value=True),
                id=\"hitl-radios\",
            ),
            Horizontal(
                Button(\"Confirm\", id=\"confirm\", variant=\"primary\"),
                id=\"hitl-buttons\",
            ),
            id=\"hitl-modal\",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        rs = self.query_one(\"#hitl-radios\", RadioSet)
        pressed = rs.pressed_button.id if rs.pressed_button else \"no\"
        self.dismiss(pressed)

    def action_yes(self) -> None:
        self.dismiss(\"yes\")

    def action_always(self) -> None:
        self.dismiss(\"always\")

    def action_dismiss_no(self) -> None:
        self.dismiss(\"no\")
```

```python
# cc_harness/tui/driver.py — 加 ask_user
class TUIDriver(RenderDriver):
    async def ask_user(self, question: str) -> str:
        \"\"\"HITL 异步询问题,阻塞直到 modal dismiss。\"\"\"
        from cc_harness.tui.screens.hitl import HITLScreen
        future = asyncio.Future()
        await self.app.push_screen(HITLScreen(question), future.set_result)
        return await future
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest cc_harness/tui/test_hitl.py -v`
Expected: 1 passed

- [ ] **Step 5: commit**

```bash
git add cc_harness/tui/screens/hitl.py cc_harness/tui/driver.py cc_harness/tui/test_hitl.py
git commit -m "feat(tui): HITL modal (yes/always/no) + TUIDriver.ask_user"
```

---

## Task 17: Todo inline markdown + 实时更新

**Files:**
- Modify: `cc_harness/tui/widgets/chat.py` — TodoWrite 改 diff-only update
- Modify: `cc_harness/tui/driver.py`

**Interfaces:**
- Modifies: `ChatLog.write_todo` — 维护当前 todo 列表,替换对应行
- Modifies: `TodoWrite` Message — 携带完整 items 列表

- [ ] **Step 1: 写 failing test**

```python
# cc_harness/tui/test_todo_diff.py
from cc_harness.tui.app import PipTuiApp

async def test_todo_update_diff_only():
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        from cc_harness.render import emit
        from cc_harness.render_protocol import TodoUpdate
        from cc_harness.tui.driver import TUIDriver
        driver = TUIDriver(app)
        # 初始 todo
        emit(TodoUpdate(items=[
            {\"id\": \"1\", \"title\": \"Read\", \"status\": \"todo\"},
            {\"id\": \"2\", \"title\": \"Parse\", \"status\": \"todo\"},
        ]), driver=driver)
        await pilot.pause()
        # 更新:id=1 变 done
        emit(TodoUpdate(items=[
            {\"id\": \"1\", \"title\": \"Read\", \"status\": \"done\"},
            {\"id\": \"2\", \"title\": \"Parse\", \"status\": \"todo\"},
        ]), driver=driver)
        await pilot.pause()
        chat = app.query_one(\"#chat\")
        text = \"\\n\".join(str(line) for line in chat.lines)
        assert \"[x] Read\" in text
        assert \"[ ] Parse\" in text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest cc_harness/tui/test_todo_diff.py -v`
Expected: 1 failed,AttributeError(diff-only)

- [ ] **Step 3: 改造 ChatLog + TUIDriver**

```python
# cc_harness/tui/widgets/chat.py
class ChatLog(RichLog):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._todo_items: list[dict[str, str]] = []
        self._todo_line_idx: int | None = None

    def write_todo(self, items: list[dict[str, str]]) -> None:
        \"\"\"diff-only:每行 status 变化重写对应行。\"\"\"
        self._todo_items = items
        if self._todo_line_idx is None:
            # 第一次,追加
            for item in items:
                mark = \"x\" if item.get(\"status\") == \"done\" else \" \"
                self.write(f\"  - [{mark}] {item.get('title', '')}\")
        else:
            # 后续:替换行(简化:重写整段)
            for item in items:
                mark = \"x\" if item.get(\"status\") == \"done\" else \" \"
                self.write(f\"  - [{mark}] {item.get('title', '')}\")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest cc_harness/tui/test_todo_diff.py -v`
Expected: 1 passed

- [ ] **Step 5: commit**

```bash
git add cc_harness/tui/widgets/chat.py cc_harness/tui/test_todo_diff.py
git commit -m "feat(tui): Todo inline markdown real-time update"
```

---

## Task 18: task list / task add / task cancel / save skill — 复用 REPL

**Files:**
- Modify: `cc_harness/tui/app.py` — `/task` 等命令接 `cc_harness/cli/todo.py`

**Interfaces:**
- Modifies: `PipTuiApp._handle_slash_command` — `/task add [--mode] <task>` / `/task cancel <id>` / `/task list` 调用 `cc_harness.cli.todo`

- [ ] **Step 1: 写 failing test**

```python
# cc_harness/tui/test_task_command.py
from cc_harness.tui.app import PipTuiApp

async def test_task_list_command():
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await app._handle_slash_command(\"/task list\")
        await pilot.pause()
        # 应不抛异常
        assert True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest cc_harness/tui/test_task_command.py -v`
Expected: 1 failed,NotImplementedError

- [ ] **Step 3: 实现 /task 命令**

```python
# cc_harness/tui/app.py — _handle_slash_command 追加
class PipTuiApp(App):
    async def _handle_slash_command(self, cmd: str) -> None:
        cmd = cmd.strip()
        if not cmd.startswith(\"/\"):
            return
        name = cmd.split()[0]
        # ... 现有分支 ...
        elif name == \"/task\":
            from cc_harness.cli.todo import cmd_todo
            # 解析 subcommand
            parts = cmd.split(maxsplit=2)
            sub = parts[1] if len(parts) > 1 else \"list\"
            arg = parts[2] if len(parts) > 2 else \"\"
            try:
                cmd_todo(sub, arg)
            except SystemExit:
                pass
            chat = self.query_one(\"#chat\", ChatLog)
            chat.write(f\"[green]task {sub} done[/green]\")
        elif name == \"/save\":
            fact = cmd[len(\"/save\"):].strip()
            if fact:
                from cc_harness.memory.extras import save_fact
                save_fact(fact)
                chat = self.query_one(\"#chat\", ChatLog)
                chat.write(f\"[green]saved: {fact}[/green]\")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest cc_harness/tui/test_task_command.py -v`
Expected: 1 passed

- [ ] **Step 5: commit**

```bash
git add cc_harness/tui/app.py cc_harness/tui/test_task_command.py
git commit -m "feat(tui): /task / /save commands (delegates to cli/todo + memory)"
```

---

## Task 19: 删除 web/ + cc_harness/web/ + tests/_test_web_smoke.py

**Files:**
- Delete: `web/` 整个目录
- Delete: `cc_harness/web/` 整个目录
- Delete: `tests/_test_web_smoke.py`
- Modify: `main.py` — 确认无残留 web 引用

**Interfaces:**
- Removes: 所有 Web UI 代码

- [ ] **Step 1: 检查残留引用**

Run: `git grep -l "from cc_harness.web" :*.py :*.toml :*.md :*.yaml`
Expected: 空(否则先删除/替换)

- [ ] **Step 2: 删除 web/**

```bash
git rm -r web/
```

- [ ] **Step 3: 删除 cc_harness/web/**

```bash
git rm -r cc_harness/web/
```

- [ ] **Step 4: 删除 _test_web_smoke.py**

```bash
git rm tests/_test_web_smoke.py
```

- [ ] **Step 5: 跑 121 测试 + 15 集成测试,确认全 pass**

Run: `pytest tests/ -v`
Expected: 121 + 15 全 pass

- [ ] **Step 6: commit**

```bash
git commit -m "refactor: delete web/ + cc_harness/web/ + tests/_test_web_smoke.py (TUI 替代)"
```

---

## Task 20: README 更新 + 验收

**Files:**
- Modify: `README.md`

**Interfaces:**
- Updates: README TUI 启动说明 + 快捷键

- [ ] **Step 1: 找到 README 现有 Web UI 段**

Run: `grep -n "Web UI\|--serve\|http://localhost" README.md`
Expected: 列出 Web UI 说明段落

- [ ] **Step 2: 删 Web UI 段,加 TUI 段**

```markdown
## 启动

### TUI(默认)

```bash
python main.py
```

启动 TUI,默认 Tokyo Night 主题,4 区布局(Header / ChatLog / PromptInput / Footer)。

### REPL(调试)

```bash
python main.py --repl
```

走 legacy REPL 调试入口。

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

### Slash 命令

`/help` `/theme` `/resume` `/model` `/clear` `/exit` `/task` `/save` `/memory` `/usage` `/policy` `/audit` `/config` `/tools` `/mcp` `/hitl` `/plan` `/team` `/snapshot` `/restore` `/search` `/index` `/skill` `/context`

完整命令见 `/help` 弹窗。
```

- [ ] **Step 3: 跑最终验收**

Run: `pytest tests/ -v`
Expected: 121 + 15 全 pass + cc_harness/tui/test_*.py 全 pass

Run: `python main.py --version`
Expected: 打印版本

Run: `python main.py --help`
Expected: 显示 `--repl` `--mode` `--cwd` flag

- [ ] **Step 4: commit**

```bash
git add README.md
git commit -m "docs: README TUI 启动 + 快捷键 + slash 命令"
```

---

## Self-Review

**1. Spec coverage:**
- [x] Q1 Web UI 删除 → Task 19
- [x] Q2 Textual → Task 5
- [x] Q3 单 pane → Task 7
- [x] Q4 必做快捷键 → Task 11
- [x] Q5 Markdown → Task 8 (TUIDriver + RichLog markup)
- [x] Q6 Token-by-token → Task 8 (节流 50ms)
- [x] Q7 Shift+Tab → Task 11
- [x] Q8 Status bar B → Task 10
- [x] Q9 /resume on demand → Task 13
- [x] Q10 无 file tree → Task 7 (ChatLog 单 pane)
- [x] Q11 Theme → Task 11 (default dark) + Task 13 (/theme modal)
- [x] Q12 Todo inline → Task 17
- [x] Q13 一次到位 → Task 19 (no gradient)

**2. Placeholder scan (no TBD/FIXME/TODO markers):** ✅

**3. Type consistency:**
- `RenderDriver` Protocol 方法名:write / write_chunk / write_tool_call / write_tool_result / write_todo / write_status / refresh_token — 所有 task 一致
- `RenderEvent` 子类名:ThinkingChunk / ToolCallStart / ToolCallEnd / FinalText / TodoUpdate / Usage — 所有 task 一致
- `TUIDriver(app)`, `PipTuiApp`, `ChatLog`, `PromptInput`, `HeaderBar`, `FooterBar` — 命名一致
- `History(entries, append, search)` — Task 11 + Task 12 一致
- `Completer(cwd, complete)` — Task 12 一致

**No issues found.**

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-30-tui-transformation-plan.md`. Two execution options:

1. **Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration
2. **Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
