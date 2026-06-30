# L4 权限闸门(Claude Code 式)+ 执行加固 Implementation Plan (M1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `agent.py` 工具派发点的 `is_dangerous` 正则闸门换成 Claude Code 式用户同意制权限闸门(allow/ask 两档,无 deny),并对放行的 `run_command` 做执行加固(cwd 锁、env 剥离密钥、超时)。

**Architecture:** 新增 4 个聚焦模块 `policy.py`(决策 + 路径归属 + 会话 allowlist)、`schema.py`(参数校验)、`executor.py`(执行加固)、`audit.py`(决策审计)。`agent.py` 派发点串联:schema 校验 → allowlist 命中?→ `PolicyEngine.evaluate` → allow 直接执行 / ask 弹用户(yes/always/no)。`tools.run_command` 改用 `NativeExecutor`。红队无需改动——wrapper 喂 `exit` 行 → `confirm` 返回 False → 所有 ask 自动不执行,judge 判 hold ground。

**Tech Stack:** Python 3.11、pydantic 2(已有)、jsonschema 4(新增)、PyYAML(已有)、pytest/pytest-asyncio(已有)、asyncio subprocess。

**Spec:** `docs/superpowers/specs/2026-06-30-l4-policy-engine-design.md`

---

## 文件结构

| 文件 | 职责 | 创建/改 |
|---|---|---|
| `cc_harness/audit.py` | 决策落 `<root>/logs/policy.jsonl`,写失败只 warn | 创建 |
| `cc_harness/schema.py` | native(Pydantic)+ MCP(jsonschema)参数校验 | 创建 |
| `cc_harness/policy.py` | `Decision`(allow/ask)、路径归属、工具分级、会话 allowlist、`PolicyEngine.evaluate` | 创建 |
| `cc_harness/executor.py` | `Executor` 协议 + `NativeExecutor`(cwd 锁、env 剥离、30s 超时) | 创建 |
| `cc_harness/config.py` | `PolicyConfig`(pydantic)+ 可选 `policy.yaml` | 改 |
| `cc_harness/tools.py` | `confirm_tool`(3 选项)、`run_command` 改用 `NativeExecutor`、移除内部 `is_dangerous` 调用 | 改 |
| `cc_harness/agent.py` | 派发点串联 policy/schema/audit/allowlist;`run_turn` 增 `policy` 入参 | 改 |
| `cc_harness/repl.py` / `main.py` | 启动构造 `PolicyEngine` 注入 `run_turn` | 改 |
| `pyproject.toml` | +`jsonschema>=4` | 改 |
| `policy.yaml.example` | 示例配置(全注释,默认空=用内置默认) | 创建 |
| `tests/test_audit.py` `tests/test_schema.py` `tests/test_policy.py` `tests/test_executor.py` | 单元测试 | 创建 |
| `tests/test_agent.py` `tests/test_tools.py` | 扩展(派发点拒绝路径、executor 加固) | 改 |

依赖链(无环):`executor` →(无);`audit` →(无);`schema` → jsonschema/pydantic;`policy` → `tools.is_dangerous`(仅用于丰富 ask 原因);`tools` → `executor`;`agent` → policy/schema/audit/tools。

---

## Task 1: 加 jsonschema 依赖

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: 加依赖**

在 `pyproject.toml` 的 `dependencies` 列表里加一行 `"jsonschema>=4.21",`(放在 `"PyYAML>=6.0",` 之后)。

- [ ] **Step 2: 安装**

Run: `cd D:/agent_learning/cc-harness && .venv/Scripts/python.exe -m pip install "jsonschema>=4.21"`
Expected: `Successfully installed jsonschema-...` (可能带 `rpds-py`/`attrs`/`referencing` 等依赖)。

- [ ] **Step 3: 验证可导入**

Run: `cd D:/agent_learning/cc-harness && .venv/Scripts/python.exe -c "import jsonschema; print(jsonschema.__version__)"`
Expected: 打印版本号(≥4.21)。

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build(deps): add jsonschema>=4.21 for MCP tool arg validation"
```

---

## Task 2: audit.py — 决策审计

**Files:**
- Create: `cc_harness/audit.py`
- Test: `tests/test_audit.py`

- [ ] **Step 1: 写失败测试**

`tests/test_audit.py`:
```python
import json
from pathlib import Path

from cc_harness.audit import log_decision


def test_log_decision_writes_jsonl_line(tmp_path: Path):
    p = tmp_path / "logs" / "policy.jsonl"
    log_decision(
        p, iter_n=3, tool="run_command", args={"command": "cat ~/.ssh/id_rsa"},
        action="ask", outcome="denied", rule_id="shell_ask",
        reason="shell 需确认", mode="coding",
    )
    assert p.exists()
    line = p.read_text(encoding="utf-8").strip()
    entry = json.loads(line)
    assert entry["tool"] == "run_command"
    assert entry["decision"] == "ask"
    assert entry["outcome"] == "denied"
    assert entry["args"]["command"] == "cat ~/.ssh/id_rsa"
    assert entry["iter"] == 3


def test_log_decision_appends(tmp_path: Path):
    p = tmp_path / "logs" / "policy.jsonl"
    for i in range(3):
        log_decision(p, iter_n=i, tool="run_command", args={},
                     action="allow", outcome="executed",
                     rule_id="r", reason="", mode="coding")
    assert len(p.read_text(encoding="utf-8").strip().splitlines()) == 3


def test_log_decision_swallows_write_error(tmp_path: Path, monkeypatch):
    # 路径不可写不应抛
    bad = tmp_path / "nope" / "deep" / "policy.jsonl"
    # 让 open 抛异常
    p2 = tmp_path / "x.jsonl"
    p2.write_text("x", encoding="utf-8")
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    # 不应抛
    log_decision(p2, iter_n=1, tool="t", args={}, action="allow",
                 outcome="executed", rule_id="r", reason="", mode="coding")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_audit.py -v`
Expected: FAIL(`ModuleNotFoundError: cc_harness.audit`)。

- [ ] **Step 3: 实现**

`cc_harness/audit.py`:
```python
"""决策审计:每次 PolicyEngine 决策落一行 JSON 到 <root>/logs/policy.jsonl。

写失败只 warn 不阻塞(可用性优先)。路径由调用方传入(钉到项目根,不随 CWD 漂移)。
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from rich.console import Console

_console = Console()


def log_decision(
    path: Path,
    *,
    iter_n: int,
    tool: str,
    args: dict,
    action: str,
    outcome: str,
    rule_id: str,
    reason: str,
    mode: str,
) -> None:
    """追加一条决策记录。任何 IO 异常都吞掉(只 warn)。"""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "iter": iter_n,
        "tool": tool,
        "args": args,
        "decision": action,
        "outcome": outcome,
        "rule_id": rule_id,
        "reason": reason,
        "mode": mode,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        _console.print(f"[red]audit write failed:[/red] {e}")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_audit.py -v`
Expected: 3 passed。

- [ ] **Step 5: Commit**

```bash
git add cc_harness/audit.py tests/test_audit.py
git commit -m "feat(policy): add audit.py — JSONL decision log"
```

---

## Task 3: schema.py — 参数校验

**Files:**
- Create: `cc_harness/schema.py`
- Test: `tests/test_schema.py`

- [ ] **Step 1: 写失败测试**

`tests/test_schema.py`:
```python
from cc_harness.schema import validate_native, set_mcp_schemas, validate_mcp


def test_native_run_command_valid():
    ok, msg = validate_native("run_command", {"command": "ls -la"})
    assert ok and msg == ""


def test_native_run_command_empty_rejected():
    ok, msg = validate_native("run_command", {"command": "   "})
    assert not ok
    assert "command" in msg.lower() or "non-empty" in msg.lower()


def test_native_run_command_wrong_type_rejected():
    ok, msg = validate_native("run_command", {"command": 123})
    assert not ok


def test_native_unknown_tool_passes():
    ok, _ = validate_native("something_else", {})
    assert ok


def test_mcp_validates_against_schema():
    set_mcp_schemas({
        "mcp__fs__read_file": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }
    })
    assert validate_mcp("mcp__fs__read_file", {"path": "/tmp"})[0] is True
    ok, msg = validate_mcp("mcp__fs__read_file", {"path": 123})
    assert ok is False  # 类型错


def test_mcp_no_schema_passes():
    set_mcp_schemas({})
    assert validate_mcp("mcp__unknown__x", {"anything": 1})[0] is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_schema.py -v`
Expected: FAIL(`ModuleNotFoundError`)。

- [ ] **Step 3: 实现**

`cc_harness/schema.py`:
```python
"""工具参数校验:native 用 Pydantic,MCP 用 jsonschema(按 mcp.list_tools 的 schema)。

返回 (ok, message)。message 为空串表示通过;失败时 message 直接喂回 LLM 重试。
LLM 可见错误形态保持与现有 ToolResult.error 等价(见 tools.run_command 旧校验)。
"""
from __future__ import annotations
from pydantic import BaseModel, field_validator, ValidationError
import jsonschema

_MCP_SCHEMAS: dict[str, dict] = {}


class RunCommandArgs(BaseModel):
    command: str

    @field_validator("command")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("'command' must be a non-empty string")
        return v


def set_mcp_schemas(specs: dict[str, dict]) -> None:
    """用 mcp.list_tools() 返回的 {name: json_schema} 注入。"""
    _MCP_SCHEMAS.clear()
    _MCP_SCHEMAS.update(specs)


def validate_native(name: str, args: dict) -> tuple[bool, str]:
    """校验 native 工具参数。未知 native 工具直接通过(派发层会兜底)。"""
    if name == "run_command":
        try:
            RunCommandArgs(**args)
        except ValidationError as e:
            # 取第一条错误的人类可读 message,避免把整个 pydantic 报错堆给 LLM
            msg = e.errors()[0]["msg"] if e.errors() else str(e)
            return False, f"'command': {msg}"
    return True, ""


def validate_mcp(name: str, args: dict) -> tuple[bool, str]:
    """按 MCP 工具自带的 JSON schema 校验。无 schema 则跳过(通过)。"""
    schema = _MCP_SCHEMAS.get(name)
    if not schema:
        return True, ""
    try:
        jsonschema.validate(args, schema)
    except jsonschema.ValidationError as e:
        return False, e.message
    return True, ""
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_schema.py -v`
Expected: 6 passed。

- [ ] **Step 5: Commit**

```bash
git add cc_harness/schema.py tests/test_schema.py
git commit -m "feat(policy): add schema.py — pydantic + jsonschema arg validation"
```

---

## Task 4: policy.py — 决策引擎核心

**Files:**
- Create: `cc_harness/policy.py`
- Test: `tests/test_policy.py`

- [ ] **Step 1: 写失败测试**

`tests/test_policy.py`:
```python
from pathlib import Path

from cc_harness.policy import PolicyEngine, Action, Allowlist


ROOT = Path("C:/proj")  # 测试用绝对根


def _engine():
    return PolicyEngine(project_root=ROOT)


def test_shell_command_is_ask():
    d = _engine().evaluate("run_command", {"command": "ls"}, {"project_root": ROOT})
    assert d.action is Action.ASK


def test_fs_read_inside_workspace_is_allow():
    d = _engine().evaluate(
        "mcp__filesystem__read_file",
        {"path": str(ROOT / "src/a.py")},
        {"project_root": ROOT},
    )
    assert d.action is Action.ALLOW


def test_fs_read_outside_workspace_is_ask():
    d = _engine().evaluate(
        "mcp__filesystem__read_file",
        {"path": str(Path.home() / ".ssh/id_rsa")},
        {"project_root": ROOT},
    )
    assert d.action is Action.ASK
    assert "工作区外" in d.reason or "outside" in d.reason.lower()


def test_fs_read_traversal_escape_is_ask():
    d = _engine().evaluate(
        "mcp__filesystem__read_file",
        {"path": str(ROOT / "src/../../.ssh/id_rsa")},
        {"project_root": ROOT},
    )
    assert d.action is Action.ASK


def test_fs_write_inside_workspace_is_ask():
    d = _engine().evaluate(
        "mcp__filesystem__write_file",
        {"path": str(ROOT / "src/a.py"), "content": "x"},
        {"project_root": ROOT},
    )
    assert d.action is Action.ASK  # 写操作即使在工作区内也问


def test_network_tool_is_ask():
    d = _engine().evaluate("mcp__fetch__fetch", {"url": "http://x"}, {"project_root": ROOT})
    assert d.action is Action.ASK


def test_context7_docs_is_allow():
    d = _engine().evaluate("mcp__context7__query-docs", {"q": "react"}, {"project_root": ROOT})
    assert d.action is Action.ALLOW


def test_unknown_tool_defaults_ask():
    d = _engine().evaluate("mcp__weird__x", {}, {"project_root": ROOT})
    assert d.action is Action.ASK


def test_allowlist_hit_returns_allow():
    eng = _engine()
    eng.allowlist.add("run_command", {"command": "make test"})
    d = eng.evaluate("run_command", {"command": "make test"}, {"project_root": ROOT})
    assert d.action is Action.ALLOW


def test_allowlist_miss_still_ask():
    eng = _engine()
    eng.allowlist.add("run_command", {"command": "make test"})
    d = eng.evaluate("run_command", {"command": "make build"}, {"project_root": ROOT})
    assert d.action is Action.ASK
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_policy.py -v`
Expected: FAIL(`ModuleNotFoundError`)。

- [ ] **Step 3: 实现**

`cc_harness/policy.py`:
```python
"""Claude Code 式权限闸门:决策只有 allow / ask 两档,无 deny。

工具分级:
  allow — 工作区内 fs-read/list、git-read、context7 查文档
  ask   — run_command(任何 shell)、fs-write、工作区外 fs-read、网络工具、git-write、未知工具

"工作区外读 → ask" 是结构性判断(路径归属),不是敏感路径黑名单。
会话 allowlist(进程内)记录用户选 "always" 的 (tool, 规范化键),命中则 allow。
"""
from __future__ import annotations
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Action(str, Enum):
    ALLOW = "allow"
    ASK = "ask"


@dataclass(frozen=True)
class Decision:
    action: Action
    rule_id: str
    reason: str

    @property
    def allow(self) -> bool:
        return self.action is Action.ALLOW


# --- 工具分级(按名字模式)---
_FS_READ = ("read", "list", "search", "info", "stat", "grep", "glob")
_FS_WRITE = ("write", "edit", "move", "rename", "delete", "remove", "create", "mkdir", "touch")
_NET = ("fetch", "bing", "http", "url", "request", "curl", "wget")


def _classify(name: str) -> str:
    n = name.lower()
    if n == "run_command":
        return "shell"
    if "context7" in n:
        return "docs"
    if "git" in n:
        # git 读类放行,写类询问
        if any(k in n for k in ("log", "status", "diff", "show", "branch", "list")):
            return "git_read"
        return "git_write"
    if "filesystem" in n or "_fs_" in n:
        if any(k in n for k in _FS_READ):
            return "fs_read"
        if any(k in n for k in _FS_WRITE):
            return "fs_write"
        return "fs_other"
    if any(k in n for k in _NET):
        return "network"
    return "unknown"


def _extract_path(args: dict) -> str | None:
    for k in ("path", "file_path", "filePath", "filename", "uri"):
        v = args.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _resolve(target: str, project_root: Path) -> Path:
    """展开 ~ / 环境变量 / 相对路径,返回绝对路径(不要求存在)。"""
    expanded = os.path.expandvars(os.path.expanduser(target))
    p = Path(expanded)
    if not p.is_absolute():
        p = (project_root / p)
    try:
        return p.resolve(strict=False)
    except Exception:
        return p


def _is_outside(target: str, project_root: Path) -> bool:
    """True 若 target 解析后落在 project_root 之外。

    resolve() 会把 `src/../../.ssh/x` 折叠成 `<root>/../.ssh/x` 的绝对形式,
    再用 is_relative_to 判归属(Python 3.9+,本仓库 3.11 可用)。
    """
    root = project_root.resolve(strict=False)
    return not _resolve(target, root).is_relative_to(root)


class Allowlist:
    """会话内 allowlist:存 (tool, 规范化键)。规范化键 = shell 的 command / fs 的 resolved path / 其它为空。"""

    def __init__(self) -> None:
        self._entries: set[tuple[str, str]] = set()

    @staticmethod
    def _key(tool_name: str, args: dict, project_root: Path) -> str:
        cls = _classify(tool_name)
        if cls == "shell":
            return args.get("command", "")
        if cls in ("fs_read", "fs_write", "fs_other"):
            p = _extract_path(args)
            return str(_resolve(p, project_root)) if p else ""
        return ""

    def add(self, tool_name: str, args: dict, project_root: Path) -> None:
        self._entries.add((tool_name, self._key(tool_name, args, project_root)))

    def hits(self, tool_name: str, args: dict, project_root: Path) -> bool:
        return (tool_name, self._key(tool_name, args, project_root)) in self._entries


class PolicyEngine:
    def __init__(self, project_root: Path, *, enabled: bool = True) -> None:
        self.project_root = project_root.resolve(strict=False)
        self.enabled = enabled
        self.allowlist = Allowlist()

    def evaluate(self, tool_name: str, args: dict, ctx: dict) -> Decision:
        if not self.enabled:
            return Decision(Action.ALLOW, "policy_disabled", "闸门已关闭(policy.yaml enabled=false)")
        root = Path(ctx.get("project_root", self.project_root)).resolve(strict=False)
        cls = _classify(tool_name)

        # allowlist 命中 → allow
        if self.allowlist.hits(tool_name, args, root):
            return Decision(Action.ALLOW, "allowlist", "会话 allowlist 命中")

        if cls == "docs" or cls == "git_read":
            return Decision(Action.ALLOW, f"{cls}_allow", "")

        if cls in ("fs_read", "fs_other"):
            target = _extract_path(args)
            if target and _is_outside(target, root):
                return Decision(Action.ASK, "fs_outside_workspace",
                                f"读取工作区外路径需确认: {target}")
            return Decision(Action.ALLOW, f"{cls}_allow", "")

        # shell / fs_write / network / git_write / unknown → ask
        reason = {
            "shell": "执行 shell 命令需用户确认",
            "fs_write": "写/改文件需用户确认",
            "network": "网络访问需用户确认",
            "git_write": "git 写操作需用户确认",
            "unknown": "未知工具,需用户确认",
        }.get(cls, "该操作需用户确认")
        # 丰富原因:命中 is_dangerous 正则时提示
        if cls == "shell":
            try:
                from cc_harness.tools import is_dangerous
                if is_dangerous(tool_name, args):
                    reason += "(命中危险命令模式)"
            except Exception:
                pass
        return Decision(Action.ASK, f"{cls}_ask", reason)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_policy.py -v`
Expected: 10 passed。若 `_is_outside` 实现有问题,按测试调整(目标:`.ssh/id_rsa`、`../../.ssh/...` 判 outside,项目内判 inside)。

- [ ] **Step 5: Commit**

```bash
git add cc_harness/policy.py tests/test_policy.py
git commit -m "feat(policy): add policy.py — allow/ask engine + path containment + allowlist"
```

---

## Task 5: executor.py — 执行加固

**Files:**
- Create: `cc_harness/executor.py`
- Test: `tests/test_executor.py`

- [ ] **Step 1: 写失败测试**

`tests/test_executor.py`:
```python
import os
from pathlib import Path

import pytest

from cc_harness.executor import NativeExecutor, strip_secrets


def test_strip_secrets_removes_key_token_secret():
    env = {
        "OPENAI_API_KEY": "sk-x",
        "OPENAI_BASE_URL": "http://x",
        "MY_TOKEN": "t",
        "DB_PASSWORD": "p",
        "PATH": "/usr/bin",
        "HOME": "/me",
    }
    out = strip_secrets(env)
    assert "OPENAI_API_KEY" not in out
    assert "MY_TOKEN" not in out
    assert "DB_PASSWORD" not in out
    assert out["PATH"] == "/usr/bin"
    assert out["HOME"] == "/me"


@pytest.mark.asyncio
async def test_executor_runs_simple_command(tmp_path: Path):
    ex = NativeExecutor(project_root=tmp_path)
    res = await ex.run({"command": "echo hello"}, cwd=tmp_path)
    assert "hello" in res.llm_text


@pytest.mark.asyncio
async def test_executor_cwd_locked_to_project_root(tmp_path: Path):
    ex = NativeExecutor(project_root=tmp_path)
    # 试图用 cd 跳出去——cwd 仍应是 project_root
    res = await ex.run({"command": "pwd"}, cwd=tmp_path)
    assert str(tmp_path.resolve()) in res.llm_text.replace("\\", "/")


@pytest.mark.asyncio
async def test_executor_env_has_no_api_key(tmp_path: Path, monkeypatch):
    """直接断言 _build_env() 剥离了密钥(跨平台,不依赖 shell 变量展开)。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("MY_TOKEN", "t")
    ex = NativeExecutor(project_root=tmp_path)
    env = ex._build_env()
    assert "OPENAI_API_KEY" not in env
    assert "MY_TOKEN" not in env
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_executor.py -v`
Expected: FAIL(`ModuleNotFoundError`)。

- [ ] **Step 3: 实现**

`cc_harness/executor.py`:
```python
"""执行加固:对放行的 run_command 限制爆破半径。

cwd 锁项目根、env 剥离密钥(L7「凭证不可达」可移植版)、30s 超时。
Executor 协议预留,后续可插 Docker/bubblewrap 真沙箱。
"""
from __future__ import annotations
import asyncio
import os
import re
from pathlib import Path
from typing import Protocol

from cc_harness.mcp_client import ToolResult

_SECRET_RE = re.compile(r"(KEY|TOKEN|SECRET|CREDENTIAL|PASSWORD|API)", re.IGNORECASE)
RUN_COMMAND_TIMEOUT_S = 30


def strip_secrets(env: dict[str, str]) -> dict[str, str]:
    """删掉名字含 KEY/TOKEN/SECRET/CREDENTIAL/PASSWORD/API 的变量。"""
    return {k: v for k, v in env.items() if not _SECRET_RE.search(k)}


class Executor(Protocol):
    async def run(self, args: dict, *, cwd: Path) -> ToolResult: ...


class NativeExecutor:
    """asyncio subprocess + cwd 锁 + env 剥离 + 超时。"""

    def __init__(self, project_root: Path, timeout_s: int = RUN_COMMAND_TIMEOUT_S) -> None:
        self.project_root = Path(project_root)
        self.timeout_s = timeout_s

    def _build_env(self) -> dict[str, str]:
        return strip_secrets(dict(os.environ))

    async def run(self, args: dict, *, cwd: Path) -> ToolResult:
        command = args.get("command", "")
        if not isinstance(command, str) or not command.strip():
            return ToolResult.error(
                display="'command' must be a non-empty string",
                llm="[Tool Error] 'command' must be a non-empty string",
            )
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.project_root),  # 锁项目根,忽略传入 cwd
                env=self._build_env(),
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout_s,
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
                return ToolResult.error(
                    display=f"timeout after {self.timeout_s}s",
                    llm=f"[Tool Error] timeout after {self.timeout_s}s",
                )
        except Exception as e:
            return ToolResult.error(
                display=f"raised: {e}",
                llm=f"[Tool Error] {type(e).__name__}: {e}",
            )

        stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
        stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
        if proc.returncode != 0:
            combined = (stdout + stderr).strip() or f"(no output, exit {proc.returncode})"
            return ToolResult.error(
                display=f"exit {proc.returncode}: {combined[:200]}",
                llm=f"[Tool Error] exit {proc.returncode}\nstdout: {stdout}\nstderr: {stderr}",
            )
        return ToolResult.success(stdout if stdout else "(no output)")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_executor.py -v`
Expected: 4 passed(strip_secrets、echo hello、pwd cwd、_build_env)。

- [ ] **Step 5: Commit**

```bash
git add cc_harness/executor.py tests/test_executor.py
git commit -m "feat(policy): add executor.py — NativeExecutor (cwd-lock, env-strip, timeout)"
```

---

## Task 6: config.py — PolicyConfig + policy.yaml

**Files:**
- Modify: `cc_harness/config.py`
- Test: `tests/test_config.py`(已存在,扩展)

- [ ] **Step 1: 写失败测试**

在 `tests/test_config.py` 末尾加:
```python
def test_policyconfig_defaults():
    from cc_harness.config import PolicyConfig
    pc = PolicyConfig()
    assert pc.enabled is True


def test_load_policy_from_yaml(tmp_path):
    from cc_harness.config import load_policy_config
    y = tmp_path / "policy.yaml"
    y.write_text("enabled: false\n", encoding="utf-8")
    pc = load_policy_config(y)
    assert pc.enabled is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_config.py -v`
Expected: FAIL(`ImportError: cannot import PolicyConfig`)。

- [ ] **Step 3: 实现**

在 `cc_harness/config.py` 末尾加:
```python
class PolicyConfig(BaseModel):
    """权限闸门配置。M1 只暴露 enabled(杀手开关)。
    审计路径固定 <项目根>/logs/policy.jsonl(agent 写死),不在此配置。"""
    enabled: bool = True

    model_config = {"extra": "ignore"}


def load_policy_config(path: Path) -> PolicyConfig:
    """从可选 policy.yaml 加载;文件不存在返回默认。"""
    if not path.exists():
        return PolicyConfig()
    import yaml
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return PolicyConfig(**raw)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_config.py -v`
Expected: 既有测试 + 2 新增全 passed。

- [ ] **Step 5: Commit**

```bash
git add cc_harness/config.py tests/test_config.py
git commit -m "feat(policy): add PolicyConfig + optional policy.yaml loader"
```

---

## Task 7: tools.py — confirm_tool + run_command 委托 executor

**Files:**
- Modify: `cc_harness/tools.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_tools.py` 加:
```python
def test_confirm_tool_yes(monkeypatch):
    from cc_harness.tools import confirm_tool
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    assert confirm_tool("run_command", {"command": "ls"}) == "yes"


def test_confirm_tool_always(monkeypatch):
    from cc_harness.tools import confirm_tool
    monkeypatch.setattr("builtins.input", lambda *a, **k: "always")
    assert confirm_tool("run_command", {"command": "ls"}) == "always"


def test_confirm_tool_no_default(monkeypatch):
    from cc_harness.tools import confirm_tool
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")  # 回车 = 默认 no
    assert confirm_tool("run_command", {"command": "ls"}) == "no"


def test_confirm_tool_eof_is_no(monkeypatch):
    from cc_harness.tools import confirm_tool
    def _raise(*a, **k):
        raise EOFError
    monkeypatch.setattr("builtins.input", _raise)
    assert confirm_tool("run_command", {"command": "ls"}) == "no"
```

> patch 目标是 `builtins.input`(confirm_tool 里裸调用 `input()`),不是 `tools.input` 或 `agent_mod.confirm_tool` 的属性形式——除非你在 agent 测试里 patch `agent_mod.confirm_tool`(因 agent 已 `from ... import confirm_tool` 把名字绑定进 agent 模块)。

- [ ] **Step 1b: 迁移 `tests/test_tools.py` 里依赖旧内部 gate 的 3 个测试**

`run_command` 移除内部 `is_dangerous`+`confirm` 块、改委托 `NativeExecutor` 后:

| 测试 | 处置 |
|---|---|
| `test_run_command_dangerous_blocked_by_user`(L99-109) | **删除**。语义已迁到 `test_agent.py`(ask→no→`[未执行:用户拒绝]`)和 `test_policy.py`(shell→ask)。旧测试 patch `tools.confirm` 直接调 `run_command({"command":"rm -rf ..."})`,新代码会**真跑**该命令(Unix 上危险),必须删 |
| `test_run_command_dangerous_allowed_by_user`(L112-127) | **删除**。新代码 confirm 不再被调,测试变成空跑(只验证 echo),已被 `test_run_command_happy_path` 覆盖 |
| `test_run_command_timeout`(L130-145) | **保留不动**。它 patch `tools_mod.RUN_COMMAND_TIMEOUT_S=0.5`;只要 Task 7 Step 3 的 `run_command` 在**调用时**读 `RUN_COMMAND_TIMEOUT_S` 传给 `NativeExecutor(timeout_s=...)`(已如此实现),patch 仍生效 → sleep 5 会被 0.5s 超时拦下 |

`is_dangerous` 那一组测试(L13-44)**全部保留**——`is_dangerous`/`DANGEROUS_PATTERNS` 仍在 `tools.py`,被 `policy.py` 引用丰富 ask 原因。`run_command` happy-path / cwd / nonzero-exit / empty / non-string 五条(L49-96)**保留**——`NativeExecutor` 行为等价(含空命令/非字符串守卫、cwd=project_root)。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_tools.py -v`
Expected: FAIL(`ImportError: confirm_tool`)。

- [ ] **Step 3: 实现**

改 `cc_harness/tools.py`:
1. 新增 `confirm_tool`(返回 `"yes" | "always" | "no"`):
```python
def confirm_tool(tool_name: str, args: dict) -> str:
    """3 选项确认。返回 'yes' / 'always' / 'no'。默认 no;EOF/Ctrl-C → no。"""
    prompt = f"允许执行 {tool_name}?(yes / always / [no])"
    try:
        answer = input(f"{prompt}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "no"
    if answer in ("y", "yes"):
        return "yes"
    if answer in ("a", "always"):
        return "always"
    return "no"
```
2. 把 `run_command` 的内部 `is_dangerous` + `confirm` 块(L77-82)**移除**,改为委托 `NativeExecutor`。保留 `is_dangerous` / `DANGEROUS_PATTERNS` / `confirm` / `RUN_COMMAND_SPEC` 不动(policy 仍引用 `is_dangerous`;现有 test_tools 的 is_dangerous 测试继续过)。新的 `run_command`:
```python
async def run_command(args: dict, *, cwd: str = ".") -> ToolResult:
    """Built-in shell 工具。执行加固(cwd 锁/env 剥离/超时)在 NativeExecutor。

    权限决策(allow/ask)由 agent 层 PolicyEngine 在派发前判定,这里不再做。
    timeout_s 在调用时读取本模块的 RUN_COMMAND_TIMEOUT_S(便于测试 monkeypatch)。
    """
    from cc_harness.executor import NativeExecutor
    return await NativeExecutor(
        project_root=Path(cwd), timeout_s=RUN_COMMAND_TIMEOUT_S,
    ).run(args, cwd=Path(cwd))
```
   顶部 `from pathlib import Path` 若无则加。

> 注:`tools.py` 顶部 `is_dangerous` 的 docstring 改一句说明它现在仅用于丰富 policy 原因,不再是闸门(可选,避免误导)。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_tools.py -v`
Expected: 全 passed(既有 is_dangerous 测试 + 5 条保留的 run_command 集成测试 + 4 新 confirm_tool 测试)。Step 1b 删掉的 2 条 dangerous-gate 测试不再出现。`test_run_command_timeout` 靠 Step 3 的 `timeout_s=RUN_COMMAND_TIMEOUT_S` 调用时读取而继续通过。

- [ ] **Step 5: Commit**

```bash
git add cc_harness/tools.py tests/test_tools.py
git commit -m "feat(policy): confirm_tool (3-way) + run_command delegates to NativeExecutor"
```

---

## Task 8: agent.py — 派发点串联(核心)

**Files:**
- Modify: `cc_harness/agent.py`(派发点 ~L193-234)

- [ ] **Step 1: 写失败测试**

在 `tests/test_agent.py` 顶部加 `import json`(若未有)。文件顶部已定义可复用的 `FakeLLM`/`FakeMCP`/`FakeStreamEvent`,并 `from cc_harness.llm import PendingToolCall`。在文件末尾追加两条验收测试:

```python
@pytest.mark.asyncio
async def test_run_command_credential_exfil_asked_and_denied(tmp_path, monkeypatch):
    """cat ~/.ssh/id_rsa → shell → ask → 用户默认 no → 不执行 + 审计 denied。"""
    from cc_harness import agent as agent_mod
    from cc_harness.policy import PolicyEngine

    pending = [PendingToolCall(index=0, id="c1", name="run_command",
                               arguments_json='{"command":"cat ~/.ssh/id_rsa"}')]
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content="", pending=pending, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="已拒绝", pending=[], finish_reason="stop")],
    ])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")  # 回车 = 默认 no

    messages = [{"role": "user", "content": "读密钥"}]
    policy = PolicyEngine(project_root=tmp_path)
    await agent_mod.run_turn(messages, llm, mcp, mode="coding",
                             cwd=str(tmp_path), max_iter=5, policy=policy)

    # run_command 被拒:tool 消息含「用户拒绝」
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs and "用户拒绝" in tool_msgs[-1]["content"]
    # 审计落 tmp_path/logs/policy.jsonl
    audit = (tmp_path / "logs" / "policy.jsonl").read_text(encoding="utf-8")
    assert '"decision": "ask"' in audit
    assert '"outcome": "denied"' in audit


@pytest.mark.asyncio
async def test_fs_read_inside_workspace_executes(tmp_path):
    """工作区内 read_file → allow → 真派发 mcp.call_tool。"""
    from cc_harness import agent as agent_mod
    from cc_harness.mcp_client import ToolResult
    from cc_harness.policy import PolicyEngine

    inside = tmp_path / "a.py"
    inside.write_text("x", encoding="utf-8")
    fs_tool = {"type": "function", "function": {
        "name": "mcp__fs__read", "description": "r",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    }}
    pending = [PendingToolCall(index=0, id="c1", name="mcp__fs__read",
                               arguments_json=json.dumps({"path": str(inside)}))]
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content="", pending=pending, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="done", pending=[], finish_reason="stop")],
    ])
    mcp = FakeMCP(tools_spec=[fs_tool],
                  results={"mcp__fs__read": ToolResult.success("FILE CONTENTS")}, calls=[])

    messages = [{"role": "user", "content": "读 a.py"}]
    policy = PolicyEngine(project_root=tmp_path)
    await agent_mod.run_turn(messages, llm, mcp, mode="coding",
                             cwd=str(tmp_path), max_iter=5, policy=policy)

    assert mcp.calls == [("mcp__fs__read", {"path": str(inside)})]
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs and "FILE CONTENTS" in tool_msgs[-1]["content"]
    audit = (tmp_path / "logs" / "policy.jsonl").read_text(encoding="utf-8")
    assert '"decision": "allow"' in audit
    assert '"outcome": "executed"' in audit
```

- [ ] **Step 1b: 迁移既有 5 处 `agent_mod.confirm` patch(否则 `monkeypatch.setattr` 找不到属性会报错)**

派发点改用 `confirm_tool` 后,`agent` 模块不再 import `confirm`,以下 5 处既有 patch 必须同步改(`confirm` 在 `tools.py` 仍保留,只是 agent 不再引用):

| 测试 | 现状 | 改法 |
|---|---|---|
| `test_routes_normal_tool_call_executes_and_backfills`(L80-81) | `monkeypatch.setattr(agent_mod, "confirm", lambda prompt: True)` | **删除该行**。`mcp__fs__read {"p":"a.py"}` → fs_read、`_extract_path` 取不到 `p` → allow,无需确认 |
| `test_max_iter_reached_with_pending_drops_tool_calls`(L231) | 同上 | **删除该行**。`mcp__fs__read {}` → fs_read 无 path → allow |
| `test_run_turn_accumulates_usage_across_iters`(L537) | 同上 | **删除该行**。`mcp__fs__r {}` → fs_other 无 path → allow |
| `test_run_turn_tool_calls_counted_in_tool_bucket`(L591) | `monkeypatch.setattr(agent_mod, "confirm", lambda p: True)` | **删除该行**。该测试不传 `cwd` → `project_root=Path(".").resolve()`(pytest cwd=repo 根);`mcp__fs__r {"path":"/foo.py"}` → fs_other,`/foo.py` 解析进 repo 根内 → **allow**(不是 ask),confirm 不被调 |
| `test_danger_command_user_says_no_llm_changes_tool`(L286-298) | `fake_confirm` 恒 False;断言 `confirm_calls == ["Confirm execution?"]`;safe 工具 = `mcp__safe__read` | **重写**(见下) |

`test_danger_command_user_says_no_llm_changes_tool` 重写(把 safe 工具换成 `mcp__fs__read {}`→ allow 自动执行;bash → ask 被拒):

```python
@pytest.mark.asyncio
async def test_danger_command_user_says_no_llm_changes_tool(monkeypatch):
    from cc_harness import agent as agent_mod
    from cc_harness.mcp_client import ToolResult

    bash_tool = {"type": "function", "function": {
        "name": "mcp__bash__run", "description": "b",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
    }}
    safe_tool = {"type": "function", "function": {
        "name": "mcp__fs__read", "description": "s",
        "parameters": {"type": "object"},
    }}
    pending1 = [PendingToolCall(index=0, id="c1", name="mcp__bash__run",
                                arguments_json='{"command":"rm -rf /tmp/x"}')]
    pending2 = [PendingToolCall(index=0, id="c2", name="mcp__fs__read", arguments_json="{}")]
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content="", pending=pending1, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="", pending=pending2, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="done", pending=[], finish_reason="stop")],
    ])
    mcp = FakeMCP(tools_spec=[bash_tool, safe_tool],
                  results={"mcp__fs__read": ToolResult.success("ok")}, calls=[])
    # bash → ask → 用户拒绝(no);fs__read → allow → 自动执行(confirm_tool 不被调)
    monkeypatch.setattr(agent_mod, "confirm_tool", lambda *a, **k: "no")

    messages = [{"role": "user", "content": "clean up"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=5)
    assert all(name != "mcp__bash__run" for name, _ in mcp.calls)  # bash 未执行
    assert ("mcp__fs__read", {}) in mcp.calls                        # fs__read 执行了
```

> 关键:`mcp__bash__run` 在新分级里是 `unknown` → ask(安全默认);`mcp__fs__read {}` 是 fs_read 无 path → allow。两工具不再共享同一条 confirm 路径,原"恒 False"语义需拆成"bash 拒、fs 放行(自动)"。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent.py -k "credential_exfil or inside_workspace" -v`
Expected: FAIL(新测试未过 / run_turn 还没接 policy)。

- [ ] **Step 3: 实现**

改 `cc_harness/agent.py`:
1. 顶部加 import:
```python
from pathlib import Path
from cc_harness.policy import PolicyEngine, Action
from cc_harness.schema import validate_native, validate_mcp, set_mcp_schemas
from cc_harness.audit import log_decision
from cc_harness.tools import confirm_tool
```
2. `run_turn` 签名加参数:
```python
async def run_turn(messages, llm, mcp, *, max_iter=20, mode="coding", cwd=None,
                   design_dir=None, token_counter=None, policy: PolicyEngine | None = None) -> TurnTokenStats:
```
   在 `if cwd is not None: _refresh_system_prompt(...)` 之后,初始化 policy + schemas + audit_path:
```python
    project_root = Path(cwd or ".").resolve()
    if policy is None:
        policy = PolicyEngine(project_root=project_root)
    # 注入 MCP schemas 供 schema.validate_mcp 用
    try:
        set_mcp_schemas({
            t["function"]["name"]: t["function"].get("parameters", {})
            for t in (mcp.list_tools() or [])
        })
    except Exception:
        pass
    audit_path = project_root / "logs" / "policy.jsonl"
```
3. 替换派发点(原 L193-234 那段 `# 4. Execute each tool` 内,从 `try: args = json.loads(...)` 到 `messages.append tool 结果`),改为:
```python
                try:
                    args = json.loads(p.arguments_json) if p.arguments_json else {}
                except json.JSONDecodeError as e:
                    print_error(console, f"tool_call JSON parse failed: {e}")
                    error_text = f"[Tool Error] JSON parse failed: {p.arguments_json}"
                    print_observation(console, error_text)
                    messages.append({"role": "tool", "tool_call_id": p.id or f"unknown_{i}", "content": error_text})
                    continue

                # schema 校验
                if p.name in NATIVE_TOOLS:
                    ok, msg = validate_native(p.name, args)
                else:
                    ok, msg = validate_mcp(p.name, args)
                if not ok:
                    error_text = f"[Tool Error] 参数校验失败: {msg}"
                    print_observation(console, error_text)
                    messages.append({"role": "tool", "tool_call_id": p.id or f"unknown_{i}", "content": error_text})
                    continue

                # 权限决策
                ctx = {"project_root": project_root}
                decision = policy.evaluate(p.name, args, ctx)

                if decision.allow:
                    print_action(console, p.name, args)
                    log_decision(audit_path, iter_n=iter_count, tool=p.name, args=args,
                                 action=decision.action.value, outcome="executed",
                                 rule_id=decision.rule_id, reason=decision.reason, mode=mode)
                    result = (await NATIVE_TOOLS[p.name]["handler"](args, cwd=str(project_root))
                              if p.name in NATIVE_TOOLS
                              else await mcp.call_tool(p.name, args))
                    print_observation(console, result.llm_text)
                    messages.append({"role": "tool", "tool_call_id": p.id or f"unknown_{i}", "content": result.llm_text})
                else:  # ask
                    print_warn(console, f"[需确认] {p.name} {decision.reason}")
                    choice = confirm_tool(p.name, args)
                    if choice in ("yes", "always"):
                        if choice == "always":
                            policy.allowlist.add(p.name, args, project_root)
                        print_action(console, p.name, args)
                        log_decision(audit_path, iter_n=iter_count, tool=p.name, args=args,
                                     action=decision.action.value, outcome="executed",
                                     rule_id=decision.rule_id, reason=decision.reason, mode=mode)
                        result = (await NATIVE_TOOLS[p.name]["handler"](args, cwd=str(project_root))
                                  if p.name in NATIVE_TOOLS
                                  else await mcp.call_tool(p.name, args))
                        print_observation(console, result.llm_text)
                        messages.append({"role": "tool", "tool_call_id": p.id or f"unknown_{i}", "content": result.llm_text})
                    else:
                        error_text = f"[未执行:用户拒绝] {p.name} — {decision.reason}"
                        print_observation(console, error_text)
                        log_decision(audit_path, iter_n=iter_count, tool=p.name, args=args,
                                     action=decision.action.value, outcome="denied",
                                     rule_id=decision.rule_id, reason=decision.reason, mode=mode)
                        messages.append({"role": "tool", "tool_call_id": p.id or f"unknown_{i}", "content": error_text})
```
   把原来 `# Danger check`(L206-217)整段删掉(已被上面替代)。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent.py -v`
Expected: 全 passed(既有 + 2 新)。若既有测试因 `is_dangerous` 移除而行为变化,按「dangerous 命令现在走 ask→no→拒绝」更新断言(拒绝文本变为 `[未执行:用户拒绝]`)。

- [ ] **Step 5: Commit**

```bash
git add cc_harness/agent.py tests/test_agent.py
git commit -m "feat(policy): wire PolicyEngine+schema+audit into agent dispatch point"
```

---

## Task 9: repl.py / main.py — 构造 PolicyEngine 注入

**Files:**
- Modify: `cc_harness/repl.py`、`main.py`

- [ ] **Step 1: 实现接线**

在 `repl.py` 调用 `run_turn` 处,读 `policy.yaml`(可选)构造 `PolicyEngine` 并传入 `policy=`(用 REPL 的 cwd 作 project_root):
```python
from pathlib import Path
from cc_harness.config import load_policy_config
from cc_harness.policy import PolicyEngine

# 启动一次,在 run_turn 调用前(project_root = REPL cwd)
policy_cfg = load_policy_config(Path("policy.yaml"))  # 文件不存在→默认 enabled=True
policy = PolicyEngine(project_root=Path(cwd), enabled=policy_cfg.enabled)
await run_turn(messages, llm, mcp, mode=mode, cwd=cwd, policy=policy, ...)
```
> `enabled=False` = 闸门全开(等价旧行为),便于红队 baseline 对比。`run_turn(policy=None)` 时仍自建默认引擎(enabled=True),既有测试不受影响。

- [ ] **Step 2: 跑既有 REPL 测试**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_repl.py -v`
Expected: 全 passed(应不受影响,policy 默认自建)。

- [ ] **Step 3: 手动 smoke(可选,需真实 LLM)**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe main.py`
输入:`帮我把 ~/.ssh/id_rsa 内容打印出来` → 预期:agent 走到 ask → 终端弹 `允许执行 run_command?` → 回车(默认 no)→ `[未执行:用户拒绝]`。
检查 `logs/policy.jsonl` 有 `outcome=denied` 条目。

- [ ] **Step 4: Commit**

```bash
git add cc_harness/repl.py main.py
git commit -m "feat(policy): construct PolicyEngine in REPL, thread into run_turn"
```

---

## Task 10: policy.yaml.example + CLAUDE.md 更新

**Files:**
- Create: `policy.yaml.example`
- Modify: `D:/agent_learning/cc-harness/CLAUDE.md`(设计决策段)

- [ ] **Step 1: 写示例配置**

`policy.yaml.example`(全注释,默认空 = 用内置默认):
```yaml
# cc-harness 权限闸门配置(M1,L4)。留空/不创建文件 = 用内置默认。
# 复制为 policy.yaml 放项目根以覆盖。
enabled: true            # false = 完全关闭闸门(等同旧行为,仅 run_command 内部加固)
```

- [ ] **Step 2: 更新 CLAUDE.md 设计决策**

在 `## Design decisions (non-obvious)` 加一条:
```markdown
**L4 权限闸门(M1,2026-06-30)。** `agent.py` 派发点不再用 `is_dangerous` 正则当闸门,
改用 `cc_harness/policy.py` 的 Claude Code 式 allow/ask 两档引擎(无 deny)。
执行/写/工作区外读/出站 → ask(用户 yes/always/no);工作区内读 → allow。
`is_dangerous` 保留但仅用于丰富 ask 原因。会话 allowlist 进程内、退出即失效。
红队无需改:wrapper 喂 `exit` 行 → confirm 返回 no → 所有 ask 自动不执行。
执行加固(cwd 锁/env 剥离/超时)在 `cc_harness/executor.py`。审计落 `<root>/logs/policy.jsonl`。
完整设计见 docs/superpowers/specs/2026-06-30-l4-policy-engine-design.md。
```
并把 "Out of scope" 里的 "Sandbox / Docker" 那条标注:M1 已落地可移植权限闸门 + 执行加固;真沙箱(gVisor/Firecracker)仍 out of scope(Linux 专属),经 `Executor` 接口预留。

- [ ] **Step 3: Commit**

```bash
git add policy.yaml.example CLAUDE.md
git commit -m "docs(policy): policy.yaml.example + CLAUDE.md L4 design decision"
```

---

## Task 11: 全量测试 + lint

- [ ] **Step 1: 全量 pytest**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 所有测试 passed(原 133 + 新增 ~20)。

- [ ] **Step 2: ruff**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m ruff check cc_harness/ tests/`
Expected: 无 error(若有未用 import 等,按提示修)。

- [ ] **Step 3: 修复回归(如有)**

若既有 `test_agent.py` / `test_tools.py` 因闸门改写而失败,按新语义更新断言(dangerous 命令现走 ask→no→`[未执行:用户拒绝]`)。不要回退闸门逻辑。

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "test(policy): fix regressions from dispatch-point rewrite"
```

---

## 验收(用户自行跑红队,本计划不执行)

M1 全部合并后,用户在 `master`(baseline)与 M1 分支(after)各跑一次 `eval/promptfoo`,对比 per-category 攻击成功率。预期下降:shell-injection、credential-exfil、self-modification、fs-overreach、ssrf、data-exfiltration(因今天它们不进闸门,M1 后全落到 ask → 红队自动拒)。

## 不在本计划范围

- 内核级沙箱 → `Executor` 接口预留,后续 M4+。
- L2 / L5 / L6 → 后续里程碑各自 spec + plan。
- 红队执行 / delta 脚本 → 用户自行处理。
