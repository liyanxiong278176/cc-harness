# cc-harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a terminal-based coding agent (`cc-harness`) that wraps an OpenAI-compatible LLM with a ReAct loop, routes native `tool_calls` to MCP servers (stdio/SSE/streamable-HTTP), and streams Rich-colored output to the console.

**Architecture:** 8 focused Python modules (`config`, `prompts`, `render`, `tools`, `llm`, `mcp_client`, `agent`, `repl`) plus a `main.py` entry point. All tools come from MCP servers declared in `mcp.json`; no built-in tool implementations. ReAct is implemented as a single `finish_reason`/`pending` routing in `agent.py` — no text-regex parsing. Async everywhere; sync `input()` is bridged via `asyncio.to_thread` so the event loop stays responsive.

**Tech Stack:** Python 3.11+, `openai>=1.40` (async client), `mcp[cli]>=1.0` (official MCP SDK), `rich>=13.7`, `pydantic>=2.6`, `python-dotenv>=1.0`, `pytest` + `pytest-asyncio` for tests.

**Spec:** `docs/superpowers/specs/2026-06-10-cc-harness-design.md`

---

## File Structure

| File | Lines (target) | Responsibility |
|------|---------------:|----------------|
| `pyproject.toml` | 30 | Deps + ruff + pytest config |
| `.env.example` | 5 | Template for OPENAI_API_KEY / BASE_URL / MODEL |
| `mcp.json` | 12 | MCP server config (filesystem stdio + sse/http examples) |
| `tests/fake_mcp_server.py` | 40 | Subprocess-spawned test MCP server with one echo tool |
| `tests/conftest.py` | 30 | Shared pytest fixtures |
| `cc_harness/__init__.py` | 1 | Package marker |
| `cc_harness/config.py` | 70 | Pydantic `AppConfig`/`MCPServerConfig` + `load_config()` |
| `cc_harness/prompts.py` | 15 | `SYSTEM_PROMPT` constant |
| `cc_harness/render.py` | 55 | Rich color wrappers: thought/tool_call/result/final/warn |
| `cc_harness/tools.py` | 45 | `DANGEROUS_PATTERNS`, `is_dangerous()`, `confirm()` |
| `cc_harness/llm.py` | 110 | `PendingToolCall`, `LLMClient.chat()` async stream + delta accumulation |
| `cc_harness/mcp_client.py` | 130 | Three transports (stdio/SSE/HTTP), `list_tools()` → ToolSpec, `call_tool()` → `ToolResult` |
| `cc_harness/agent.py` | 90 | `run_turn()` ReAct loop, finish_reason routing, danger intercept, max_iter guard |
| `cc_harness/repl.py` | 60 | `run_repl()` with `asyncio.to_thread` input, 5-step shutdown |
| `main.py` | 25 | Entry: load config → start MCP → run REPL |
| `tests/test_config.py` | 60 | Config parsing |
| `tests/test_render.py` | 40 | Color ANSI assertions |
| `tests/test_tools.py` | 30 | Danger pattern matching |
| `tests/test_llm.py` | 110 | Mock-stream delta accumulation + json.loads failure |
| `tests/test_mcp_client.py` | 90 | Subprocess fake server end-to-end |
| `tests/test_agent.py` | 130 | All routing cases including danger-then-change |

Total: ~20 files, ~1200 lines including tests.

**Execution order** (matches user request — bottom-up by dependency):
1. Setup
2. config
3. render
4. prompts
5. tools
6. llm
7. mcp_client
8. agent
9. repl
10. main
11. Smoke test (manual)

---

## Task 0: Project setup

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `mcp.json`
- Create: `cc_harness/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "cc-harness"
version = "0.1.0"
description = "Terminal coding agent with MCP tools"
requires-python = ">=3.11"
dependencies = [
  "openai>=1.40",
  "mcp[cli]>=1.0",
  "rich>=13.7",
  "python-dotenv>=1.0",
  "pydantic>=2.6",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "pytest-cov>=5.0", "ruff>=0.6"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["cc_harness"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
addopts = "-q"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py311"
```

- [ ] **Step 2: Write `.env.example`**

```bash
# Required
OPENAI_API_KEY=sk-...

# Optional (have defaults)
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
```

- [ ] **Step 3: Write `mcp.json`**

```json
{
  "mcpServers": {
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/agent_learning/cc-harness"]
    },
    "remote-sse-example": {
      "type": "sse",
      "url": "http://localhost:8000/sse"
    },
    "remote-http-example": {
      "type": "http",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

- [ ] **Step 4: Create empty package files**

```bash
touch cc_harness/__init__.py
touch tests/__init__.py
```

- [ ] **Step 5: Write `tests/conftest.py`**

```python
import pytest
from rich.console import Console

@pytest.fixture
def console() -> Console:
    """Console writing to a string buffer, for ANSI color assertions."""
    return Console(file=None, force_terminal=True, color_system="truecolor", width=120)
```

- [ ] **Step 6: Install dev deps and verify pytest runs**

```bash
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest --collect-only
```

Expected: pytest collects 0 tests (no test files yet), exits 5.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .env.example mcp.json cc_harness/ tests/
git commit -m "Task 0: project setup (pyproject, mcp.json, .env.example, package skeleton)"
```

---

## Task 1: config.py

**Files:**
- Create: `cc_harness/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing test for `MCPServerConfig`**

```python
# tests/test_config.py
from cc_harness.config import MCPServerConfig

def test_stdio_server_config():
    cfg = MCPServerConfig(type="stdio", command="npx", args=["-y", "foo"])
    assert cfg.type == "stdio"
    assert cfg.command == "npx"
    assert cfg.args == ["-y", "foo"]
    assert cfg.transport_type == "stdio"

def test_sse_server_config():
    cfg = MCPServerConfig(type="sse", url="http://x/sse")
    assert cfg.transport_type == "sse"

def test_http_server_config():
    cfg = MCPServerConfig(type="http", url="http://x/mcp")
    assert cfg.transport_type == "http"

def test_http_alias_accepted():
    """streamable-http should also map to http transport."""
    cfg = MCPServerConfig(type="streamable-http", url="http://x/mcp")
    assert cfg.transport_type == "http"
```

- [ ] **Step 2: Run, verify FAIL**

```bash
.venv/Scripts/python -m pytest tests/test_config.py -v
```
Expected: `ModuleNotFoundError: No module named 'cc_harness.config'`

- [ ] **Step 3: Implement `MCPServerConfig`**

```python
# cc_harness/config.py
from typing import Literal
from pydantic import BaseModel

class MCPServerConfig(BaseModel):
    type: Literal["stdio", "sse", "http", "streamable-http"] = "stdio"
    command: str | None = None
    args: list[str] = []
    url: str | None = None
    env: dict[str, str] = {}

    @property
    def transport_type(self) -> Literal["stdio", "sse", "http"]:
        if self.type in ("http", "streamable-http"):
            return "http"
        return self.type  # type: ignore[return-value]
```

- [ ] **Step 4: Run, verify PASS**

```bash
.venv/Scripts/python -m pytest tests/test_config.py::test_stdio_server_config tests/test_config.py::test_sse_server_config tests/test_config.py::test_http_server_config tests/test_config.py::test_http_alias_accepted -v
```
Expected: 4 passed.

- [ ] **Step 5: Add failing test for `AppConfig` and `load_config()`**

Append to `tests/test_config.py`:

```python
import json
import os
from pathlib import Path
import pytest
from cc_harness.config import AppConfig, load_config, ConfigError

def test_appconfig_defaults():
    cfg = AppConfig(openai_api_key="sk-test", mcp_servers={})
    assert cfg.openai_base_url == "https://api.openai.com/v1"
    assert cfg.openai_model == "gpt-4o-mini"

def test_load_config_missing_key_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {}}))
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        load_config(env_path=tmp_path / ".env", mcp_json_path=mcp_json)

def test_load_config_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({
        "mcpServers": {
            "fs": {"type": "stdio", "command": "npx", "args": ["-y", "fs"]},
            "remote": {"type": "sse", "url": "http://x/sse"},
        }
    }))
    cfg = load_config(env_path=tmp_path / ".env", mcp_json_path=mcp_json)
    assert cfg.openai_api_key == "sk-x"
    assert cfg.openai_model == "gpt-4o"
    assert set(cfg.mcp_servers) == {"fs", "remote"}
    assert cfg.mcp_servers["fs"].transport_type == "stdio"
    assert cfg.mcp_servers["remote"].transport_type == "sse"

def test_load_config_missing_mcp_json_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    with pytest.raises(ConfigError, match="mcp.json"):
        load_config(env_path=tmp_path / ".env", mcp_json_path=tmp_path / "missing.json")
```

- [ ] **Step 6: Run, verify FAIL**

```bash
.venv/Scripts/python -m pytest tests/test_config.py::test_appconfig_defaults -v
```
Expected: `ImportError: cannot import name 'AppConfig'`

- [ ] **Step 7: Implement `AppConfig` and `load_config()`**

Append to `cc_harness/config.py`:

```python
import json
import os
from pathlib import Path
from dotenv import load_dotenv

class ConfigError(Exception):
    pass

class AppConfig(BaseModel):
    openai_api_key: str
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    mcp_servers: dict[str, MCPServerConfig]

    model_config = {"extra": "ignore"}

def load_config(env_path: Path, mcp_json_path: Path) -> AppConfig:
    """Load .env (no-op if missing) + mcp.json + required OPENAI_API_KEY."""
    if env_path.exists():
        load_dotenv(env_path, override=False)

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ConfigError("OPENAI_API_KEY is required (set in .env or env var)")

    if not mcp_json_path.exists():
        raise ConfigError(f"mcp.json not found at {mcp_json_path}")

    raw = json.loads(mcp_json_path.read_text(encoding="utf-8"))
    servers_raw = raw.get("mcpServers", {})
    servers = {name: MCPServerConfig(**cfg) for name, cfg in servers_raw.items()}

    return AppConfig(
        openai_api_key=api_key,
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        mcp_servers=servers,
    )
```

- [ ] **Step 8: Run all config tests, verify PASS**

```bash
.venv/Scripts/python -m pytest tests/test_config.py -v
```
Expected: 8 passed.

- [ ] **Step 9: Commit**

```bash
git add cc_harness/config.py tests/test_config.py
git commit -m "Task 1: config.py with AppConfig, MCPServerConfig, load_config"
```

---

## Task 2: prompts.py

**Files:**
- Create: `cc_harness/prompts.py`

- [ ] **Step 1: Write `SYSTEM_PROMPT` constant**

```python
# cc_harness/prompts.py
SYSTEM_PROMPT = """你是一个运行在终端里的编程助手,可以访问一组 MCP 工具(文件、shell 等)。
当前工作目录: {cwd}

# 规则
1. 当你需要执行操作时,请先输出你的思考过程(以"思考:"开头或自然段落皆可),然后通过工具调用来执行。
2. 工具调用由系统处理,你不需要在文本中输出 JSON 格式的 Action 块;
   **不要在文本中输出 `Action: {{...}}` 或模拟工具调用格式**,所有工具调用由系统通过结构化字段处理。
3. 如果不需要工具就能回答用户问题,直接回答,不要硬塞工具调用。
4. 如果工具执行失败,根据错误信息调整参数或换工具,不要重复同样的失败调用。
5. 危险操作(rm -rf、删库、format 等)即使工具允许,也请先在思考中向用户说明并请求确认。
6. 不要编造文件内容,没读过就说没读过。
7. 简洁优先,不要写无谓的客套话。
8. 如果一个任务需要超过 10 步工具调用,请在思考中向用户说明进度,并考虑是否可以简化或拆分任务
   (系统硬护栏是 20 步,超过会被强制终止)。
"""

def build_system_prompt(cwd: str) -> str:
    return SYSTEM_PROMPT.format(cwd=cwd)
```

- [ ] **Step 2: Smoke test**

```bash
.venv/Scripts/python -c "from cc_harness.prompts import build_system_prompt; print(build_system_prompt('/tmp'))"
```
Expected: prints the prompt with `/tmp` filled in.

- [ ] **Step 3: Commit**

```bash
git add cc_harness/prompts.py
git commit -m "Task 2: prompts.py with SYSTEM_PROMPT and build_system_prompt"
```

---

## Task 3: render.py

**Files:**
- Create: `cc_harness/render.py`
- Test: `tests/test_render.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_render.py
import re
from cc_harness.render import (
    print_thought, print_tool_call, print_tool_result, print_final,
    print_warn, print_error, print_info,
)

ANSI_BLUE = re.compile(r"\x1b\[.*?34m")
ANSI_YELLOW = re.compile(r"\x1b\[.*?33m")
ANSI_GREEN = re.compile(r"\x1b\[.*?32m")
ANSI_RED = re.compile(r"\x1b\[.*?31m")
ANSI_WHITE = re.compile(r"\x1b\[.*?37m")

def test_print_thought_blue(console):
    console.print = lambda *a, **kw: print_thought(console, "thinking")
    # Use Console.capture path
    from io import StringIO
    from rich.console import Console as C
    buf = StringIO()
    c = C(file=buf, force_terminal=True, color_system="truecolor", width=120)
    print_thought(c, "hello thought")
    text = buf.getvalue()
    assert ANSI_BLUE.search(text), f"expected blue ANSI in: {text!r}"
    assert "hello thought" in text

def test_print_tool_call_yellow(console):
    from io import StringIO
    from rich.console import Console as C
    buf = StringIO()
    c = C(file=buf, force_terminal=True, color_system="truecolor", width=120)
    print_tool_call(c, "mcp__fs__read_file", {"path": "main.py"})
    text = buf.getvalue()
    assert ANSI_YELLOW.search(text)
    assert "mcp__fs__read_file" in text
    assert "main.py" in text

def test_print_tool_result_success_green():
    from io import StringIO
    from rich.console import Console as C
    buf = StringIO()
    c = C(file=buf, force_terminal=True, color_system="truecolor", width=120)
    print_tool_result(c, "file contents here", is_error=False)
    text = buf.getvalue()
    assert ANSI_GREEN.search(text)
    assert "file contents here" in text

def test_print_tool_result_error_red():
    from io import StringIO
    from rich.console import Console as C
    buf = StringIO()
    c = C(file=buf, force_terminal=True, color_system="truecolor", width=120)
    print_tool_result(c, "ENOENT", is_error=True)
    text = buf.getvalue()
    assert ANSI_RED.search(text)

def test_print_final_white():
    from io import StringIO
    from rich.console import Console as C
    buf = StringIO()
    c = C(file=buf, force_terminal=True, color_system="truecolor", width=120)
    print_final(c, "the answer is 42")
    text = buf.getvalue()
    assert ANSI_WHITE.search(text)
    assert "the answer is 42" in text

def test_print_warn_yellow():
    from io import StringIO
    from rich.console import Console as C
    buf = StringIO()
    c = C(file=buf, force_terminal=True, color_system="truecolor", width=120)
    print_warn(c, "careful")
    text = buf.getvalue()
    assert ANSI_YELLOW.search(text)

def test_print_error_red():
    from io import StringIO
    from rich.console import Console as C
    buf = StringIO()
    c = C(file=buf, force_terminal=True, color_system="truecolor", width=120)
    print_error(c, "boom")
    text = buf.getvalue()
    assert ANSI_RED.search(text)
```

- [ ] **Step 2: Run, verify FAIL**

```bash
.venv/Scripts/python -m pytest tests/test_render.py -v
```
Expected: `ModuleNotFoundError: No module named 'cc_harness.render'`

- [ ] **Step 3: Implement `render.py`**

```python
# cc_harness/render.py
"""Rich color wrappers. All public functions take a Console instance."""
from __future__ import annotations
import json
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

def print_thought(console: Console, text: str) -> None:
    """Blue: stream LLM 'thinking' tokens as they arrive."""
    console.print(text, style="blue", end="", highlight=False)

def print_tool_call(console: Console, name: str, arguments: dict) -> None:
    """Yellow: emit a tool invocation summary."""
    args_str = json.dumps(arguments, ensure_ascii=False)
    console.print(f"\n→ {name} {args_str}", style="yellow")

def print_tool_result(console: Console, text: str, is_error: bool = False) -> None:
    """Green on success, red on error."""
    style = "red" if is_error else "green"
    label = "✗ tool result" if is_error else "✓ tool result"
    console.print(Panel(text, title=label, border_style=style, expand=False))

def print_final(console: Console, text: str) -> None:
    """White: the LLM's final answer, rendered as Markdown so code blocks highlight."""
    console.print()
    console.print(Markdown(text), style="white")

def print_warn(console: Console, text: str) -> None:
    console.print(f"[yellow]⚠ {text}[/yellow]")

def print_error(console: Console, text: str) -> None:
    console.print(f"[red]✗ {text}[/red]")

def print_info(console: Console, text: str) -> None:
    console.print(f"[cyan]{text}[/cyan]")
```

- [ ] **Step 4: Run, verify PASS**

```bash
.venv/Scripts/python -m pytest tests/test_render.py -v
```
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add cc_harness/render.py tests/test_render.py
git commit -m "Task 3: render.py with Rich color wrappers (thought/call/result/final/warn/error)"
```

---

## Task 4: tools.py

**Files:**
- Create: `cc_harness/tools.py`
- Test: `tests/test_tools.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_tools.py
from cc_harness.tools import is_dangerous

def test_unsafe_bash_tool_matches_rm_rf():
    assert is_dangerous("mcp__bash__run", {"command": "rm -rf /tmp/x"})

def test_safe_bash_tool_does_not_match_rm_r():
    """MVP: only -rf is flagged; plain -r is fine for daily dev."""
    assert not is_dangerous("mcp__bash__run", {"command": "rm -r /tmp/build"})

def test_safe_bash_tool_does_not_match_ls():
    assert not is_dangerous("mcp__bash__run", {"command": "ls -la"})

def test_write_file_content_not_scanned():
    """Per spec: write_file content is NEVER scanned (false positives)."""
    assert not is_dangerous(
        "mcp__filesystem__write_file",
        {"path": "docs.md", "content": "How to back up before rm -rf ..."},
    )

def test_non_shell_tool_with_command_field_still_flagged():
    """If a non-shell tool happens to have a 'command' field, scan it."""
    assert is_dangerous("mcp__custom__do_thing", {"command": "drop table users"})

def test_drop_database_caught():
    assert is_dangerous("mcp__db__exec", {"command": "drop database prod"})

def test_format_drive_caught():
    assert is_dangerous("mcp__os__run", {"command": "format C:"})

def test_shutdown_caught():
    assert is_dangerous("mcp__os__run", {"command": "shutdown now"})

def test_fork_bomb_caught():
    assert is_dangerous("mcp__os__run", {"command": ":(){ :|:&};:"})
```

- [ ] **Step 2: Run, verify FAIL**

```bash
.venv/Scripts/python -m pytest tests/test_tools.py -v
```
Expected: `ImportError: cannot import name 'is_dangerous'`

- [ ] **Step 3: Implement `tools.py`**

```python
# cc_harness/tools.py
"""Dangerous-command detection + user confirmation prompt."""
from __future__ import annotations
import re

# 体验级安全 — 不是安全边界。真正安全要靠沙箱和权限控制,这里只是防误操作的提示。
# MVP: 只匹配最危险的 rm -rf(避免 rm -r 这种日常用法频繁误报)。
DANGEROUS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+-rf\b"),
    re.compile(r"\brm\s+--\s"),
    re.compile(r"\brm\s+.*--no-preserve-root\b"),
    re.compile(r"\bdel\s+/[sqf]\b"),
    re.compile(r"\bformat\s+[a-zA-Z]:"),
    re.compile(r"\bdrop\s+(database|table|schema)\b"),
    re.compile(r"\btruncate\s+table\b"),
    re.compile(r":\(\)\{\s*:\|:&\s*\};:"),
    re.compile(r"\bdd\s+if=.*of=/dev/"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\breboot\b"),
]

_SHELL_TOOL_SUFFIX_RE = re.compile(r"__(bash|run_command|shell|execute)$")

def is_dangerous(tool_name: str, arguments: dict) -> bool:
    """Return True if this tool call is dangerous and needs user confirmation.

    Scans only the 'command' field of shell-class tools. write_file content is
    never scanned (see spec § 危险命令匹配).
    """
    is_shell = bool(_SHELL_TOOL_SUFFIX_RE.search(tool_name))
    has_command_field = "command" in arguments
    if not (is_shell or has_command_field):
        return False

    command = arguments.get("command", "")
    if not isinstance(command, str):
        return False

    return any(p.search(command) for p in DANGEROUS_PATTERNS)


def confirm(prompt: str) -> bool:
    """Interactive y/N prompt. Default N (Enter = No)."""
    try:
        answer = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer == "y"
```

- [ ] **Step 4: Run, verify PASS**

```bash
.venv/Scripts/python -m pytest tests/test_tools.py -v
```
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add cc_harness/tools.py tests/test_tools.py
git commit -m "Task 4: tools.py with is_dangerous (shell-only scan) and confirm"
```

---

## Task 5: llm.py

**Files:**
- Create: `cc_harness/llm.py`
- Test: `tests/test_llm.py`

This task has 4 sub-cycles for clarity (dataclasses, stream accumulator, LLMClient, JSON parse failure).

### Task 5a: Dataclasses + delta accumulator (no real API)

- [ ] **Step 1: Write failing test for `PendingToolCall` index handling**

```python
# tests/test_llm.py
import pytest
from cc_harness.llm import PendingToolCall, ToolResult, accumulate_delta

def test_pending_tool_call_index_optional():
    p = PendingToolCall()
    assert p.index is None
    assert p.id is None
    assert p.name is None
    assert p.arguments_json == ""

def test_accumulate_delta_aligns_by_index():
    pending: list[PendingToolCall] = []
    accumulate_delta(pending, index=2, id="c1", name="t1", arguments_json='{"a":')
    assert len(pending) == 3
    assert pending[2].id == "c1"
    assert pending[2].name == "t1"
    assert pending[2].arguments_json == '{"a":'

def test_accumulate_delta_appends_when_index_none():
    pending: list[PendingToolCall] = []
    accumulate_delta(pending, index=None, id="c1", name="t1", arguments_json='{')
    accumulate_delta(pending, index=None, id="c2", name="t2", arguments_json='{')
    assert len(pending) == 2
    assert pending[0].id == "c1"
    assert pending[1].id == "c2"

def test_accumulate_delta_concat_arguments():
    pending: list[PendingToolCall] = []
    accumulate_delta(pending, index=0, id="c1", name="t1", arguments_json='{"a":')
    accumulate_delta(pending, index=0, id=None, name=None, arguments_json=' 1}')
    assert pending[0].arguments_json == '{"a": 1}'
```

- [ ] **Step 2: Run, verify FAIL**

```bash
.venv/Scripts/python -m pytest tests/test_llm.py -v
```
Expected: `ImportError: cannot import name 'PendingToolCall' from 'cc_harness.llm'`

- [ ] **Step 3: Implement dataclasses + accumulator**

```python
# cc_harness/llm.py
"""OpenAI-compatible LLM client with native tool_calls streaming."""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal
from openai import AsyncOpenAI


# --- Data contracts ---

@dataclass
class PendingToolCall:
    """One tool_call accumulated from OpenAI's stream delta."""
    index: int | None = None
    id: str | None = None
    name: str | None = None
    arguments_json: str = ""


@dataclass
class StreamEvent:
    """One event yielded by LLMClient.chat()."""
    kind: Literal["content", "tool_call_delta", "done"]
    text: str = ""
    tool_call: PendingToolCall | None = None
    finish_reason: str | None = None
    pending: list[PendingToolCall] = field(default_factory=list)
    content: str = ""


# --- Delta accumulator ---

def accumulate_delta(
    pending: list[PendingToolCall],
    index: int | None,
    id: str | None,
    name: str | None,
    arguments_json: str,
) -> None:
    """Apply one delta.tool_calls[i] entry to the pending list.

    If index is given, align by index (growing the list as needed).
    If index is None, append to the end.
    """
    if index is None:
        slot = PendingToolCall()
        if id is not None:
            slot.id = id
        if name is not None:
            slot.name = name
        slot.arguments_json += arguments_json
        pending.append(slot)
        return

    while len(pending) <= index:
        pending.append(PendingToolCall())
    slot = pending[index]
    if id is not None:
        slot.id = id
    if name is not None:
        slot.name = name
    slot.arguments_json += arguments_json
```

- [ ] **Step 4: Run, verify PASS**

```bash
.venv/Scripts/python -m pytest tests/test_llm.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add cc_harness/llm.py tests/test_llm.py
git commit -m "Task 5a: PendingToolCall, StreamEvent, accumulate_delta"
```

### Task 5b: LLMClient.chat() stream + json.loads failure path

- [ ] **Step 1: Add failing test for `LLMClient.chat` happy path (mocked) and bad JSON path**

Append to `tests/test_llm.py`:

```python
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from cc_harness.llm import LLMClient

class _FakeChoiceDelta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

class _FakeChoice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason

class _FakeChunk:
    def __init__(self, delta, finish_reason=None):
        self.choices = [_FakeChoice(delta, finish_reason)]

def _tc(index, id_, name, arguments):
    """Build a fake tool_call delta. Use SimpleNamespace (NOT MagicMock) because
    `name` is the third positional/keyword arg of MagicMock.__init__ and gets
    consumed as the mock's repr-name, not an attribute. tc.name would then
    return a child MagicMock (truthy), not None."""
    return SimpleNamespace(
        index=index, id=id_, name=name,
        function=SimpleNamespace(arguments=arguments),
    )

def _make_client(stream_chunks):
    """Build an LLMClient whose underlying openai client yields stream_chunks."""
    client = LLMClient(api_key="sk-test", model="gpt-4o-mini", base_url=None)
    # Replace the internal async client
    mock = MagicMock()
    mock.chat.completions.create = AsyncMock(return_value=aiter(stream_chunks))
    client._client = mock
    return client

async def aiter(items):
    for x in items:
        yield x

@pytest.mark.asyncio
async def test_chat_streams_content_and_tool_calls():
    chunks = [
        _FakeChunk(_FakeChoiceDelta(content="I will ")),
        _FakeChunk(_FakeChoiceDelta(content="read the file")),
        _FakeChunk(_FakeChoiceDelta(tool_calls=[_tc(0, "c1", "t1", '{"pa')])),
        _FakeChunk(_FakeChoiceDelta(tool_calls=[_tc(0, None, None, 'th":1}')])),
        _FakeChunk(_FakeChoiceDelta(), finish_reason="tool_calls"),
    ]
    client = _make_client(chunks)
    events = []
    final = None
    async for ev in client.chat(messages=[{"role": "user", "content": "hi"}], tools=[]):
        events.append(ev)
        if ev.kind == "done":
            final = ev
    assert final is not None
    assert final.finish_reason == "tool_calls"
    assert final.content == "I will read the file"
    assert len(final.pending) == 1
    assert final.pending[0].name == "t1"
    assert final.pending[0].arguments_json == '{"pa"th":1}'  # concatenated
    # The accumulation: '{"pa' + 'th":1}' = '{"path":1}'

@pytest.mark.asyncio
async def test_chat_bad_json_finishes_with_raw_arguments():
    """If a tool_call's concatenated arguments_json doesn't parse, the pending
    entry is left as-is (no exception). Caller (agent.py) will detect this."""
    chunks = [
        _FakeChunk(_FakeChoiceDelta(tool_calls=[_tc(0, "c1", "t1", '{"a": oops')])),
        _FakeChunk(_FakeChoiceDelta(), finish_reason="tool_calls"),
    ]
    client = _make_client(chunks)
    final = None
    async for ev in client.chat(messages=[{"role": "user", "content": "x"}], tools=[]):
        if ev.kind == "done":
            final = ev
    assert final is not None
    assert final.pending[0].arguments_json == '{"a": oops'  # not parsed yet
```

- [ ] **Step 2: Run, verify FAIL**

```bash
.venv/Scripts/python -m pytest tests/test_llm.py::test_chat_streams_content_and_tool_calls -v
```
Expected: `AttributeError: type object 'LLMClient' has no attribute 'chat'`

- [ ] **Step 3: Implement `LLMClient`**

Append to `cc_harness/llm.py`:

```python
class LLMClient:
    """Thin async wrapper around AsyncOpenAI for streaming chat + tools.

    NB: `model` is per-call, NOT a constructor arg of AsyncOpenAI.
    """

    def __init__(self, api_key: str, model: str, base_url: str | None) -> None:
        self.model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Yield StreamEvents; the final 'done' event carries the full assistant
        message (content + pending tool_calls + finish_reason)."""
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "stream": True}
        if tools:
            kwargs["tools"] = tools

        pending: list[PendingToolCall] = []
        content_parts: list[str] = []
        finish_reason: str | None = None

        async for chunk in await self._client.chat.completions.create(**kwargs):
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            if delta.content:
                content_parts.append(delta.content)
                yield StreamEvent(kind="content", text=delta.content)

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    index = getattr(tc, "index", None)
                    tc_id = getattr(tc, "id", None)
                    tc_name = getattr(tc, "name", None) or getattr(tc, "function", None) and getattr(tc.function, "name", None)
                    tc_args = ""
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        tc_args = getattr(fn, "arguments", "") or ""
                    accumulate_delta(pending, index, tc_id, tc_name, tc_args)
                    yield StreamEvent(
                        kind="tool_call_delta",
                        tool_call=pending[index if index is not None else len(pending) - 1],
                    )

            if choice.finish_reason:
                finish_reason = choice.finish_reason

        yield StreamEvent(
            kind="done",
            finish_reason=finish_reason,
            pending=pending,
            content="".join(content_parts),
        )
```

- [ ] **Step 4: Run, verify PASS**

```bash
.venv/Scripts/python -m pytest tests/test_llm.py -v
```
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add cc_harness/llm.py tests/test_llm.py
git commit -m "Task 5b: LLMClient.chat() streams content + tool_call deltas"
```

---

## Task 6: mcp_client.py

**Files:**
- Create: `cc_harness/mcp_client.py`
- Test: `tests/fake_mcp_server.py`
- Test: `tests/test_mcp_client.py`

### Task 6a: Fake MCP server (subprocess fixture)

- [ ] **Step 1: Write `tests/fake_mcp_server.py`**

```python
# tests/fake_mcp_server.py
"""A minimal MCP stdio server for tests.

Speaks JSON-RPC over stdio. Exposes:
  - one tool 'echo' that returns its input string
  - one tool 'fail' that returns isError=True with a fixed message
  - one tool 'slow' that sleeps 0.2s
"""
import asyncio
import json
import sys
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

server = Server("fake-mcp-server")

@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="echo",
            description="Echoes back the input string",
            inputSchema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        ),
        Tool(
            name="fail",
            description="Always fails with a fixed error",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="slow",
            description="Sleeps 0.2s and returns 'done'",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "echo":
        return [TextContent(type="text", text=arguments.get("text", ""))]
    if name == "fail":
        from mcp.types import CallToolResult
        return CallToolResult(content=[TextContent(type="text", text="intentional failure")], isError=True)
    if name == "slow":
        await asyncio.sleep(0.2)
        return [TextContent(type="text", text="done")]
    raise ValueError(f"unknown tool {name}")

async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Smoke test the fake server**

```bash
.venv/Scripts/python tests/fake_mcp_server.py < /dev/null
```
Expected: process starts, waits on stdin, exits cleanly when stdin closes.

- [ ] **Step 3: Commit**

```bash
git add tests/fake_mcp_server.py
git commit -m "Task 6a: fake_mcp_server.py test fixture (echo/fail/slow tools)"
```

### Task 6b: `MCPClient.list_tools` and `call_tool` (stdio)

- [ ] **Step 1: Write failing test**

```python
# tests/test_mcp_client.py
import asyncio
import sys
from pathlib import Path
import pytest
from cc_harness.config import MCPServerConfig
from cc_harness.mcp_client import MCPClient, ToolResult

FAKE_SERVER = str(Path(__file__).parent / "fake_mcp_server.py")

@pytest.mark.asyncio
async def test_list_tools_converts_to_openai_schema():
    cfg = MCPServerConfig(type="stdio", command=sys.executable, args=[FAKE_SERVER])
    client = MCPClient({"fake": cfg})
    await client.start()
    try:
        tools = client.list_tools()
        names = {t["function"]["name"] for t in tools}
        assert "mcp__fake__echo" in names
        assert "mcp__fake__fail" in names
        # Schema sanity
        echo = next(t for t in tools if t["function"]["name"] == "mcp__fake__echo")
        assert echo["type"] == "function"
        assert "text" in echo["function"]["parameters"]["properties"]
    finally:
        await client.shutdown()

@pytest.mark.asyncio
async def test_call_tool_echo_returns_success_result():
    cfg = MCPServerConfig(type="stdio", command=sys.executable, args=[FAKE_SERVER])
    client = MCPClient({"fake": cfg})
    await client.start()
    try:
        result = await client.call_tool("mcp__fake__echo", {"text": "hi"})
        assert isinstance(result, ToolResult)
        assert result.is_error is False
        assert "hi" in result.display_text
        assert "hi" in result.llm_text
    finally:
        await client.shutdown()

@pytest.mark.asyncio
async def test_call_tool_fail_returns_error_result():
    cfg = MCPServerConfig(type="stdio", command=sys.executable, args=[FAKE_SERVER])
    client = MCPClient({"fake": cfg})
    await client.start()
    try:
        result = await client.call_tool("mcp__fake__fail", {})
        assert result.is_error is True
        assert "intentional failure" in result.display_text
        assert result.llm_text.startswith("[Tool Error]")
    finally:
        await client.shutdown()
```

- [ ] **Step 2: Run, verify FAIL**

```bash
.venv/Scripts/python -m pytest tests/test_mcp_client.py -v
```
Expected: `ImportError: cannot import name 'MCPClient'`

- [ ] **Step 3: Implement `MCPClient` (stdio only first; sse/http stubbed)**

```python
# cc_harness/mcp_client.py
"""MCP client supporting stdio, SSE, and streamable-HTTP transports."""
from __future__ import annotations
import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from cc_harness.config import MCPServerConfig

INIT_TIMEOUT_S = 5.0
CALL_TIMEOUT_S = 30.0


@dataclass
class ToolResult:
    is_error: bool = False
    display_text: str = ""
    llm_text: str = ""

    @classmethod
    def success(cls, text: str) -> "ToolResult":
        return cls(is_error=False, display_text=text, llm_text=text)

    @classmethod
    def error(cls, display: str, llm: str) -> "ToolResult":
        return cls(is_error=True, display_text=display, llm_text=llm)


class MCPClient:
    """Manages multiple MCP servers and routes tool calls to them.

    Tool names are namespaced as 'mcp__{server}__{tool}'.
    """

    def __init__(self, servers: dict[str, MCPServerConfig]) -> None:
        self._servers = servers
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._tools: list[dict] = []

    async def start(self) -> None:
        """Connect to all servers, initialize sessions, list tools."""
        for name, cfg in self._servers.items():
            try:
                if cfg.transport_type == "stdio":
                    params = StdioServerParameters(
                        command=cfg.command,  # type: ignore[arg-type]
                        args=cfg.args,
                        env=cfg.env or None,
                    )
                    read, write = await self._stack.enter_async_context(
                        asyncio.wait_for(stdio_client(params), timeout=INIT_TIMEOUT_S)
                    )
                elif cfg.transport_type == "sse":
                    from mcp.client.sse import sse_client
                    url = cfg.url  # type: ignore[assignment]
                    read, write = await self._stack.enter_async_context(sse_client(url))
                else:  # http
                    from mcp.client.streamable_http import streamablehttp_client
                    url = cfg.url  # type: ignore[assignment]
                    read, write, _ = await self._stack.enter_async_context(streamablehttp_client(url))

                session = await self._stack.enter_async_context(ClientSession(read, write))
                await asyncio.wait_for(session.initialize(), timeout=INIT_TIMEOUT_S)
                self._sessions[name] = session

                listed = await session.list_tools()
                for tool in listed.tools:
                    self._tools.append({
                        "type": "function",
                        "function": {
                            "name": f"mcp__{name}__{tool.name}",
                            "description": f"[server: {name}] {tool.description or ''}".strip(),
                            "parameters": tool.inputSchema,
                        },
                    })
            except Exception as e:
                # Per spec: continue starting other servers, print red warning.
                from rich.console import Console
                Console().print(f"[red]server {name} failed to start: {e}[/red]")

    async def shutdown(self) -> None:
        try:
            await asyncio.wait_for(self._stack.aclose(), timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            pass

    def list_tools(self) -> list[dict]:
        return list(self._tools)

    def _route(self, namespaced_name: str) -> tuple[str, str]:
        """Split 'mcp__server__tool' into ('server', 'tool')."""
        parts = namespaced_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            raise ValueError(f"tool name must be mcp__{{server}}__{{tool}}, got {namespaced_name!r}")
        return parts[1], parts[2]

    async def call_tool(self, namespaced_name: str, arguments: dict) -> ToolResult:
        server_name, tool_name = self._route(namespaced_name)
        session = self._sessions.get(server_name)
        if session is None:
            return ToolResult.error(
                display=f"server '{server_name}' not connected",
                llm=f"[Tool Error] server '{server_name}' not connected",
            )
        try:
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments),
                timeout=CALL_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            return ToolResult.error(
                display=f"tool call timed out after {CALL_TIMEOUT_S}s",
                llm=f"[Tool Error] timeout after {CALL_TIMEOUT_S}s",
            )
        except Exception as e:
            return ToolResult.error(
                display=f"tool call raised: {e}",
                llm=f"[Tool Error] {type(e).__name__}: {e}",
            )

        if getattr(result, "isError", False):
            structured = json.dumps(
                [c.model_dump() for c in getattr(result, "content", [])],
                ensure_ascii=False,
            )
            return ToolResult.error(
                display=f"tool returned error: {structured[:200]}",
                llm=f"[Tool Error] {structured}",
            )

        texts = [c.text for c in result.content if hasattr(c, "text")]
        text = "\n".join(texts) if texts else json.dumps(
            [c.model_dump() for c in result.content], ensure_ascii=False
        )
        return ToolResult.success(text)
```

- [ ] **Step 4: Run, verify PASS**

```bash
.venv/Scripts/python -m pytest tests/test_mcp_client.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add cc_harness/mcp_client.py tests/test_mcp_client.py
git commit -m "Task 6b: MCPClient with stdio/sse/http transports + ToolResult"
```

---

## Task 7: agent.py

**Files:**
- Create: `cc_harness/agent.py`
- Test: `tests/test_agent.py`

- [ ] **Step 1: Write failing tests for `run_turn` covering all routing cases**

```python
# tests/test_agent.py
import pytest
from dataclasses import dataclass, field
from typing import Any
from cc_harness.llm import PendingToolCall
from cc_harness.tools import is_dangerous, confirm

# --- Test fixtures ---

@dataclass
class FakeMCP:
    """MCPClient replacement for tests. Pre-programmed tool results."""
    tools_spec: list[dict]
    results: dict[str, Any]  # namespaced_name -> ToolResult
    calls: list[tuple[str, dict]]

    def list_tools(self) -> list[dict]:
        return list(self.tools_spec)

    async def call_tool(self, name: str, args: dict):
        self.calls.append((name, args))
        return self.results[name]

@dataclass
class FakeStreamEvent:
    kind: str
    text: str = ""
    tool_call: PendingToolCall | None = None
    finish_reason: str | None = None
    pending: list[PendingToolCall] = field(default_factory=list)  # mutable default needs factory
    content: str = ""

@dataclass
class FakeLLM:
    """Returns pre-programmed lists of StreamEvents on chat()."""
    responses: list  # list of list[StreamEvent] — one per turn
    call_count: int = 0
    model: str = "fake"

    async def chat(self, messages, tools):
        idx = self.call_count
        self.call_count += 1
        for ev in self.responses[idx]:
            yield ev

# --- Routing tests ---

@pytest.mark.asyncio
async def test_routes_normal_tool_call_executes_and_backfills(monkeypatch):
    from cc_harness import agent as agent_mod
    from cc_harness.mcp_client import ToolResult

    fs_tool = {
        "type": "function", "function": {
            "name": "mcp__fs__read", "description": "r",
            "parameters": {"type": "object", "properties": {"p": {"type": "string"}}},
        }
    }
    pending = [PendingToolCall(index=0, id="c1", name="mcp__fs__read", arguments_json='{"p":"a.py"}')]
    events = [
        FakeStreamEvent(kind="content", text="reading "),
        FakeStreamEvent(kind="content", text="file"),
        FakeStreamEvent(kind="done", content="reading file", pending=pending, finish_reason="tool_calls"),
    ]
    llm = FakeLLM(responses=[events, [
        FakeStreamEvent(kind="content", text="summary"),
        FakeStreamEvent(kind="done", content="summary", pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(
        tools_spec=[fs_tool],
        results={"mcp__fs__read": ToolResult.success("file contents")},
        calls=[],
    )
    # Don't actually prompt for confirmation
    monkeypatch.setattr(agent_mod, "confirm", lambda prompt: True)

    messages = [{"role": "user", "content": "read a.py"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=5)

    # Expect: user, assistant(tool_call), tool, assistant(summary)
    assert len(messages) == 4
    assert messages[1]["role"] == "assistant"
    assert "tool_calls" in messages[1]
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "c1"
    assert "file contents" in messages[2]["content"]
    assert messages[3]["role"] == "assistant"
    assert messages[3]["content"] == "summary"
    assert mcp.calls == [("mcp__fs__read", {"p": "a.py"})]


@pytest.mark.asyncio
async def test_routes_final_answer_when_no_tool_calls(monkeypatch):
    from cc_harness import agent as agent_mod
    llm = FakeLLM(responses=[[
        FakeStreamEvent(kind="content", text="answer is 42"),
        FakeStreamEvent(kind="done", content="answer is 42", pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    messages = [{"role": "user", "content": "what is 6*7?"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=5)
    assert len(messages) == 2
    assert messages[1] == {"role": "assistant", "content": "answer is 42"}


@pytest.mark.asyncio
async def test_routes_empty_turn_yellow_warn(monkeypatch, capfd):
    from cc_harness import agent as agent_mod
    llm = FakeLLM(responses=[[
        FakeStreamEvent(kind="done", content="", pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    messages = [{"role": "user", "content": "x"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=5)
    # No new assistant message added
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_finish_reason_tool_calls_with_empty_pending_degrades_to_stop(monkeypatch, capfd):
    from cc_harness import agent as agent_mod
    llm = FakeLLM(responses=[[
        FakeStreamEvent(kind="content", text="hi"),
        FakeStreamEvent(kind="done", content="hi", pending=[], finish_reason="tool_calls"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    messages = [{"role": "user", "content": "x"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=5)
    # Treated as final answer (not empty)
    assert len(messages) == 2
    assert messages[1] == {"role": "assistant", "content": "hi"}


@pytest.mark.asyncio
async def test_pending_tool_call_name_missing_backfills_error(monkeypatch, capfd):
    from cc_harness import agent as agent_mod
    from cc_harness.mcp_client import ToolResult

    pending = [PendingToolCall(index=0, id=None, name=None, arguments_json='{}')]
    llm = FakeLLM(responses=[
        # First turn: name-missing tool call
        [FakeStreamEvent(kind="done", content="", pending=pending, finish_reason="tool_calls")],
        # Second turn: stop with content
        [FakeStreamEvent(kind="done", content="ok", pending=[], finish_reason="stop")],
    ])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    messages = [{"role": "user", "content": "x"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=5)
    # Expect: user, assistant(tool_call), tool(error), assistant(ok)
    assert len(messages) == 4
    assert messages[1]["tool_calls"][0]["function"]["name"] == ""
    assert "unknown_0" in messages[2]["tool_call_id"]
    assert messages[2]["content"].startswith("[Tool Error]")


@pytest.mark.asyncio
async def test_max_iter_reached_with_pending_drops_tool_calls(monkeypatch):
    from cc_harness import agent as agent_mod
    from cc_harness.mcp_client import ToolResult

    fs_tool = {"type": "function", "function": {
        "name": "mcp__fs__read", "description": "r",
        "parameters": {"type": "object"},
    }}
    # Always returns a tool call — drives the loop to max_iter
    responses = []
    for _ in range(25):
        pending = [PendingToolCall(index=0, id=f"c{i}", name="mcp__fs__read", arguments_json="{}")]
        responses.append([
            FakeStreamEvent(kind="done", content=f"thought {len(responses)}",
                            pending=pending, finish_reason="tool_calls"),
        ])
    llm = FakeLLM(responses=responses)
    mcp = FakeMCP(tools_spec=[fs_tool],
                  results={"mcp__fs__read": ToolResult.success("x")},
                  calls=[])
    monkeypatch.setattr(agent_mod, "confirm", lambda prompt: True)

    messages = [{"role": "user", "content": "loop"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=20)
    # Spec: on iter==20 with has_tool_calls=True, the agent MUST:
    #   (1) drop pending tool_calls (no tool_calls on the final assistant message)
    #   (2) NOT append any role:tool backfill after the final assistant
    #   (3) emit a gentle fallback text instead
    final = messages[-1]
    assert final["role"] == "assistant"
    assert "tool_calls" not in final, "final assistant must not have tool_calls"
    assert final["content"]  # either a thought or the fallback text

    # Walk backwards: find the LAST assistant message; nothing after it should be role:tool
    final_assistant_idx = max(
        i for i, m in enumerate(messages) if m["role"] == "assistant"
    )
    assert not any(
        m["role"] == "tool" for m in messages[final_assistant_idx + 1:]
    ), "no role:tool backfill after the final assistant message"

    # The total number of assistant-with-tool_calls messages should be < 20
    # (one fewer than max_iter because the final turn drops them)
    tool_call_msgs = [m for m in messages if m.get("role") == "assistant" and "tool_calls" in m]
    assert len(tool_call_msgs) < 20


@pytest.mark.asyncio
async def test_danger_command_user_says_no_llm_changes_tool(monkeypatch):
    from cc_harness import agent as agent_mod
    from cc_harness.mcp_client import ToolResult

    bash_tool = {"type": "function", "function": {
        "name": "mcp__bash__run", "description": "b",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
    }}
    safe_tool = {"type": "function", "function": {
        "name": "mcp__safe__read", "description": "s",
        "parameters": {"type": "object"},
    }}
    # Turn 1: LLM tries to call bash with rm -rf; user says N.
    # Turn 2: LLM tries safe tool; executes.
    pending1 = [PendingToolCall(index=0, id="c1", name="mcp__bash__run",
                                arguments_json='{"command":"rm -rf /tmp/x"}')]
    pending2 = [PendingToolCall(index=0, id="c2", name="mcp__safe__read", arguments_json="{}")]
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content="", pending=pending1, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="", pending=pending2, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="done", pending=[], finish_reason="stop")],
    ])
    mcp = FakeMCP(
        tools_spec=[bash_tool, safe_tool],
        results={"mcp__safe__read": ToolResult.success("ok")},
        calls=[],
    )
    confirm_calls: list[str] = []
    def fake_confirm(prompt: str) -> bool:
        confirm_calls.append(prompt)
        return False  # user rejects rm -rf
    monkeypatch.setattr(agent_mod, "confirm", fake_confirm)

    messages = [{"role": "user", "content": "clean up"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=5)
    assert confirm_calls == ["Confirm execution?"]
    # Bash tool was NOT called
    assert all(name != "mcp__bash__run" for name, _ in mcp.calls)
    # Safe tool WAS called
    assert ("mcp__safe__read", {}) in mcp.calls
```

- [ ] **Step 2: Run, verify FAIL**

```bash
.venv/Scripts/python -m pytest tests/test_agent.py -v
```
Expected: `ImportError: cannot import name 'run_turn'`

- [ ] **Step 3: Implement `agent.py`**

```python
# cc_harness/agent.py
"""ReAct loop: streams one LLM turn, routes finish_reason, dispatches tools."""
from __future__ import annotations
import json
from rich.console import Console
from cc_harness.render import (
    print_thought, print_tool_call, print_tool_result, print_final,
    print_warn, print_error,
)
from cc_harness.tools import is_dangerous, confirm


def run_turn(
    messages: list[dict],
    llm,                    # any object with async chat(messages, tools) -> AsyncIterator[StreamEvent]
    mcp,                    # any object with list_tools() and async call_tool(name, args) -> ToolResult
    *,
    max_iter: int = 20,
) -> None:
    """Run one user turn (may involve multiple LLM ↔ tool rounds). Mutates messages in place."""
    import asyncio
    asyncio.run(_run_turn_async(messages, llm, mcp, max_iter=max_iter))


async def _run_turn_async(messages, llm, mcp, *, max_iter: int) -> None:
    console = Console()
    tool_specs = mcp.list_tools()
    iter_count = 0

    while iter_count < max_iter:
        iter_count += 1

        # 1. Stream one LLM turn
        content_parts: list[str] = []
        pending: list = []
        finish_reason: str | None = None
        try:
            async for ev in llm.chat(messages, tool_specs):
                if ev.kind == "content":
                    print_thought(console, ev.text)
                    content_parts.append(ev.text)
                elif ev.kind == "tool_call_delta":
                    pass  # accumulation handled inside llm.chat
                elif ev.kind == "done":
                    finish_reason = ev.finish_reason
                    pending = ev.pending
                    content_parts = [ev.content] if ev.content else content_parts
        except Exception as e:
            print_error(console, f"LLM stream failed: {e}")
            return

        content = "".join(content_parts)

        # 2. Compute routing
        has_tool_calls = (finish_reason == "tool_calls") and bool(pending)

        if has_tool_calls:
            # 6. Max-iter guard: if this is the last allowed iteration and the
            # LLM still wants to call tools, DROP the tool_calls entirely.
            # We must check here (not after the loop) so we don't append a
            # 20th tool_call message that the test would then count.
            if iter_count >= max_iter:
                print_warn(console, "max iterations reached with pending tool calls, forcing stop")
                if content:
                    messages.append({"role": "assistant", "content": content})
                    print_final(console, content)
                else:
                    fallback = "达到最大迭代次数,任务未完成。"
                    messages.append({"role": "assistant", "content": fallback})
                    print_final(console, fallback)
                return

            # 3. Build assistant message (with tool_calls; content may be None)
            assistant_msg: dict = {
                "role": "assistant",
                "content": content if content else None,
                "tool_calls": [_pending_to_openai_tc(p) for p in pending],
            }
            messages.append(assistant_msg)

            # 4. Execute each tool (or backfill error)
            for i, p in enumerate(pending):
                if p.name is None:
                    placeholder_id = f"unknown_{i}"
                    print_warn(console, f"tool_call name missing; feeding back error")
                    error_llm_text = (
                        f"[Tool Error] tool_call name missing, raw: "
                        f"{json.dumps({'id': p.id, 'arguments_json': p.arguments_json})}"
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": placeholder_id,
                        "content": error_llm_text,
                    })
                    continue

                try:
                    args = json.loads(p.arguments_json) if p.arguments_json else {}
                except json.JSONDecodeError as e:
                    print_error(console, f"tool_call JSON parse failed: {e}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": p.id or f"unknown_{i}",
                        "content": f"[Tool Error] JSON parse failed: {p.arguments_json}",
                    })
                    continue

                # Danger check
                if is_dangerous(p.name, args):
                    print_warn(console, f"dangerous command detected: {p.name} {args}")
                    if not confirm("Confirm execution?"):
                        messages.append({
                            "role": "tool",
                            "tool_call_id": p.id or f"unknown_{i}",
                            "content": f"[Tool Error] user rejected dangerous command: {p.name}",
                        })
                        continue

                print_tool_call(console, p.name, args)
                result = await mcp.call_tool(p.name, args)
                print_tool_result(console, result.display_text, is_error=result.is_error)
                messages.append({
                    "role": "tool",
                    "tool_call_id": p.id or f"unknown_{i}",
                    "content": result.llm_text,
                })

            # 5. Continue the loop — feed tool results back to LLM
            continue

        # has_tool_calls == False
        if finish_reason == "tool_calls" and not pending:
            print_warn(console, "finish_reason=tool_calls but no pending tool_calls, treating as stop")

        if content:
            messages.append({"role": "assistant", "content": content})
            print_final(console, content)
            return
        else:
            print_warn(console, "empty LLM turn, ending")
            return

    # 6. max_iter reached (safety net — the inner has_tool_calls branch above
    # already handles this case and returns early, so this only runs if the
    # LLM never returned has_tool_calls=True but somehow the loop also never
    # appended an assistant message and never returned).
    print_warn(console, "max iterations reached")
    if content:
        messages.append({"role": "assistant", "content": content})
        print_final(console, content)


def _pending_to_openai_tc(p) -> dict:
    """Convert a PendingToolCall to OpenAI's tool_calls entry shape."""
    return {
        "id": p.id or "",
        "type": "function",
        "function": {
            "name": p.name or "",
            "arguments": p.arguments_json,
        },
    }
```

- [ ] **Step 4: Run, verify PASS**

```bash
.venv/Scripts/python -m pytest tests/test_agent.py -v
```
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add cc_harness/agent.py tests/test_agent.py
git commit -m "Task 7: agent.py with run_turn ReAct loop, all routing cases covered"
```

---

## Task 8: repl.py

**Files:**
- Create: `cc_harness/repl.py`

No unit test — pure I/O glue, integration-tested in Task 10.

- [ ] **Step 1: Implement `repl.py`**

```python
# cc_harness/repl.py
"""Multi-turn REPL: reads user input (async-bridged), drives run_turn."""
from __future__ import annotations
import asyncio
from rich.console import Console
from rich.prompt import Prompt
from cc_harness.render import print_info, print_warn


async def _read_user() -> str:
    """Block on input() in a worker thread so the event loop stays responsive."""
    return await asyncio.to_thread(input, "› ")


async def run_repl(llm, mcp, *, max_iter: int = 20) -> None:
    console = Console()
    messages: list[dict] = []

    print_info(console, f"cc-harness ready ({len(mcp.list_tools())} tools loaded)")
    print_info(console, "type 'exit' or 'quit' to leave, Ctrl+C / Ctrl+D also works")

    while True:
        try:
            user_input = (await _read_user()).strip()
        except (EOFError, KeyboardInterrupt):
            print_info(console, "shutting down")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print_info(console, "shutting down")
            break

        messages.append({"role": "user", "content": user_input})
        from cc_harness.agent import _run_turn_async
        await _run_turn_async(messages, llm, mcp, max_iter=max_iter)
```

- [ ] **Step 2: Smoke check imports**

```bash
.venv/Scripts/python -c "from cc_harness.repl import run_repl; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add cc_harness/repl.py
git commit -m "Task 8: repl.py with asyncio.to_thread input bridge"
```

---

## Task 9: main.py

**Files:**
- Create: `main.py`

- [ ] **Step 1: Implement `main.py`**

```python
#!/usr/bin/env python3
"""cc-harness entry point."""
from __future__ import annotations
import asyncio
from pathlib import Path
from rich.console import Console
from cc_harness.config import load_config, ConfigError
from cc_harness.llm import LLMClient
from cc_harness.mcp_client import MCPClient
from cc_harness.repl import run_repl

PROJECT_ROOT = Path(__file__).parent

def main() -> None:
    console = Console()
    try:
        cfg = load_config(
            env_path=PROJECT_ROOT / ".env",
            mcp_json_path=PROJECT_ROOT / "mcp.json",
        )
    except ConfigError as e:
        console.print(f"[red]config error: {e}[/red]")
        raise SystemExit(1)

    llm = LLMClient(
        api_key=cfg.openai_api_key,
        model=cfg.openai_model,
        base_url=cfg.openai_base_url,
    )

    async def boot():
        mcp = MCPClient(cfg.mcp_servers)
        try:
            await mcp.start()
            await run_repl(llm, mcp)
        finally:
            await mcp.shutdown()

    asyncio.run(boot())


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke check**

```bash
.venv/Scripts/python -c "import main; print('main module loaded ok')"
```
Expected: `main module loaded ok`

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "Task 9: main.py entry point wiring config, LLM, MCP, REPL"
```

---

## Task 10: Full test suite + lint

- [ ] **Step 1: Run full test suite**

```bash
.venv/Scripts/python -m pytest -v
```
Expected: all tests pass (≈ 30+ tests across 5 test files).

- [ ] **Step 2: Run lint**

```bash
.venv/Scripts/python -m ruff check cc_harness/ tests/ main.py
```
Expected: zero issues. Fix any reported issues and re-run.

- [ ] **Step 3: Commit any fixes**

```bash
git add -u
git commit -m "chore: ruff lint cleanup" || echo "nothing to commit"
```

---

## Task 11: Integration smoke (manual, real MCP)

This is a manual verification — the agent cannot drive its own smoke test (chicken-and-egg).

- [ ] **Step 1: Prepare a fake OPENAI key for the smoke**

```bash
echo "OPENAI_API_KEY=sk-doesnt-matter" > .env
echo "OPENAI_BASE_URL=http://localhost:9999/v1" >> .env
echo "OPENAI_MODEL=gpt-4o-mini" >> .env
```

- [ ] **Step 2: Run with a 5-second timeout — expect it to fail LLM connection, not crash on import or config**

```bash
timeout 5 .venv/Scripts/python main.py 2>&1 | head -20
```
Expected: starts, loads config, lists tool count, then errors on LLM connection (since no real server is running). No tracebacks from cc_harness code itself.

- [ ] **Step 3: Reset `.env` to a real key (don't commit it)**

```bash
# Edit .env with a real OPENAI_API_KEY
notepad .env  # or use your editor
```

(Note: .env is in .gitignore — it will NOT be committed.)

- [ ] **Step 4: Optional: run with a real LLM and the filesystem MCP server, ask "list files in current directory"**

```bash
.venv/Scripts/python main.py
# type: 列出当前目录的 .py 文件
# type: exit
```
Expected: blue thought → yellow tool call → green tool result → white final summary. No crashes.

- [ ] **Step 5: Cleanup**

```bash
# Remove the fake .env from step 1 if you didn't overwrite it
# Leave the real one if you ran step 4
```

---

## Done Criteria (Definition of Done)

All checkboxes above are checked, plus:

1. `pytest -v` — all tests pass
2. `ruff check` — zero issues
3. `python main.py` — starts the REPL, loads MCP tools, accepts input
4. A real LLM + real filesystem MCP server can complete one round of "read a file → summarize" with the expected color flow (blue thought → yellow action → green result → white final)

The spec's full Definition of Done (`docs/superpowers/specs/2026-06-10-cc-harness-design.md` § 验收标准) is also satisfied by completing all 12 tasks.

---

## Notes for the executor

- **Run the failing test first.** TDD discipline — see the red bar before writing code.
- **Don't skip commits.** Every task ends with a commit. If a task grows past its scope, commit a checkpoint before moving on.
- **When `mcp` SDK import paths differ from what the plan shows**, defer to `python -c "import mcp.client; help(mcp.client)"` rather than guessing. Update this plan with the verified path.
- **When the fake MCP server doesn't handshake**, the most common cause is the `server.create_initialization_options()` call. Check that `tests/fake_mcp_server.py` imports `mcp.server` (not `mcp.server.fastmcp`).
- **The agent has no built-in shell tool.** Users get shell access by adding a `mcp__bash__*` server to `mcp.json`. Out of the box, only the filesystem server is configured.
