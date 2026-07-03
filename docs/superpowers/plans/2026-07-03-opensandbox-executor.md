# OpenSandbox 沙箱执行器 Implementation Plan(Plan 1: 执行器核心)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 cc-harness 的 `run_command` 执行后端从 `NativeExecutor`(宿主机直接跑)升级为可配置的 `SandboxExecutor`(OpenSandbox 沙箱隔离执行),保留 `NativeExecutor` 作降级。

**Architecture:** `Executor` 协议不动,加 `SandboxExecutor`(OpenSandbox Python SDK 封装,会话级 lazy create sandbox + 重试 3 次 + 降级 native)。`tools.py` 模块级 session 单例持有 executor,`repl` 启动 init / 退出 shutdown。`policy.py` 的 ask 闸门不动。

**Tech Stack:** Python 3.13、OpenSandbox Python SDK(`opensandbox` 包)、Docker(运行时)、pydantic(config)、pytest / pytest-asyncio。

**Spec:** `docs/superpowers/specs/2026-07-03-opensandbox-executor-design.md`(spec-review approved, `dc31300`)

**Scope:** Plan 1 = 执行器核心(组件 ①-⑨)。红队适配(wrapper 双模式 + L8)是 **Plan 2**,执行器落地后做。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `cc_harness/config.py` | +`ExecutorConfig` + `SandboxConfig` + `load_executor_config`(读 `policy.yaml` 的 `executor:` 段) |
| `cc_harness/sandbox_server.py` | **新**:server lifecycle(`ping :8000` / auto-start / `shutdown_owned`) |
| `cc_harness/sandbox.py` | **新**:`SandboxExecutor(Executor)`(SDK 封装 + mount + retry + env-strip + 降级 + kill) |
| `cc_harness/executor.py` | +`build_executor(config, project_root)` 工厂(协议不动) |
| `cc_harness/tools.py` | +session executor 单例(`init`/`get`/`shutdown`/`reset`);`run_command` 改 |
| `cc_harness/repl.py` | 启动 init / 退出 shutdown 钩子 |
| `cc_harness/prompts.py` | `tool_discipline` 教 agent 写文件用 fs 工具(沙箱 RO 拒 shell 重定向) |
| `sandboxes/Dockerfile` | **新**:轻量运行时镜像 |
| `policy.yaml.example` | +`executor:` 段 |
| `pyproject.toml` | +`[sandbox]` extra |

---

## Task 1: config.py — ExecutorConfig + load_executor_config

**Files:**
- Modify: `cc_harness/config.py`(末尾追加)
- Test: `tests/test_config.py`(augment;若不存在则建)

- [ ] **Step 1: 写失败测试** — `tests/test_config.py` 末尾加:

```python
from cc_harness.config import (ExecutorConfig, SandboxConfig, ExecutorBackend,
                                load_executor_config)


def test_executor_config_defaults_native():
    cfg = ExecutorConfig()
    assert cfg.enabled is True
    assert cfg.backend is ExecutorBackend.NATIVE   # 缺省 native(降级安全)
    assert cfg.sandbox.server_port == 8000


def test_load_executor_config_reads_yaml(tmp_path):
    from pathlib import Path
    p = tmp_path / "policy.yaml"
    p.write_text(
        "executor:\n  enabled: true\n  backend: sandbox\n"
        "  sandbox:\n    server_port: 8000\n    image: cc-harness-runtime:local\n"
        "    timeout_s: 120\n    egress_allow: [api.deepseek.com]\n",
        encoding="utf-8",
    )
    cfg = load_executor_config(p)
    assert cfg.backend is ExecutorBackend.SANDBOX
    assert cfg.sandbox.server_port == 8000
    assert cfg.sandbox.image == "cc-harness-runtime:local"
    assert "api.deepseek.com" in cfg.sandbox.egress_allow


def test_load_executor_config_missing_file_returns_default():
    from pathlib import Path
    cfg = load_executor_config(Path("/nonexistent/policy.yaml"))
    assert cfg.backend is ExecutorBackend.NATIVE   # 无文件 = native(现状)
```

- [ ] **Step 2: 跑测试确认失败**

```
PYTHONIOENCODING=utf-8 D:/agent_learning/cc-harness/.venv/Scripts/python.exe -m pytest tests/test_config.py -k executor -v
```
Expected: FAIL(ImportError: cannot import name ExecutorConfig)

- [ ] **Step 3: 改 `cc_harness/config.py`** — 顶部加 `from enum import Enum`(若未有),末尾追加:

```python
class ExecutorBackend(str, Enum):
    NATIVE = "native"
    SANDBOX = "sandbox"


class SandboxConfig(BaseModel):
    """沙箱执行器配置(policy.yaml 的 executor.sandbox 段)。"""
    server_port: int = 8000
    image: str = "cc-harness-runtime:local"
    timeout_s: int = 120          # 沙箱命令超时(比 native 30s 长,含容器开销)
    cpu: int = 2
    memory_mb: int = 2048
    egress_allow: list[str] = ["api.deepseek.com", "api.siliconflow.cn",
                               "pypi.org", "github.com"]
    vault: bool = True            # Credential Vault(失败退化 strip_secrets)
    fallback_on_error: str = "native"   # native(降级) | hard(报错)

    model_config = {"extra": "ignore"}


class ExecutorConfig(BaseModel):
    """执行后端配置。缺省 native(现状);sandbox 启用 OpenSandbox。"""
    enabled: bool = True          # 总开关:false = 强制 native(紧急回退)
    backend: ExecutorBackend = ExecutorBackend.NATIVE
    sandbox: SandboxConfig = SandboxConfig()

    model_config = {"extra": "ignore"}


def load_executor_config(path: Path) -> ExecutorConfig:
    """读 policy.yaml 的 `executor:` 段;文件/段缺失→默认(native)。"""
    if not path.exists():
        return ExecutorConfig()
    import yaml
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ExecutorConfig(**(raw.get("executor") or {}))
```

- [ ] **Step 4: 跑测试确认通过**

```
PYTHONIOENCODING=utf-8 D:/agent_learning/cc-harness/.venv/Scripts/python.exe -m pytest tests/test_config.py -v
```
Expected: PASS

- [ ] **Step 5: ruff + commit**

```
D:/agent_learning/cc-harness/.venv/Scripts/python.exe -m ruff check cc_harness/config.py tests/test_config.py
```
```bash
git -C D:/agent_learning/cc-harness add cc_harness/config.py tests/test_config.py
git -C D:/agent_learning/cc-harness commit -m "feat(config): ExecutorConfig + load_executor_config(policy.yaml executor: 段)

缺省 native(降级安全);sandbox 子段含 port 8000/image/timeout/cpu/mem/
egress_allow/vault/fallback_on_error。照 L2Config/L5Config 模式。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: sandbox_server.py — server ping + auto-start + shutdown

**Files:**
- Create: `cc_harness/sandbox_server.py`
- Test: `tests/test_sandbox_server.py`

- [ ] **Step 1: 写失败测试** — `tests/test_sandbox_server.py`:

```python
import asyncio
import pytest
from unittest.mock import patch, AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_ping_returns_true_when_port_open():
    from cc_harness.sandbox_server import ping
    # mock asyncio.open_connection 成功
    with patch("cc_harness.sandbox_server.asyncio.open_connection", new_callable=AsyncMock):
        assert await ping("localhost", 8000) is True


@pytest.mark.asyncio
async def test_ping_returns_false_when_refused():
    from cc_harness.sandbox_server import ping
    with patch("cc_harness.sandbox_server.asyncio.open_connection",
               side_effect=ConnectionRefusedError):
        assert await ping("localhost", 8000) is False


@pytest.mark.asyncio
async def test_ensure_server_reuses_existing(monkeypatch):
    """server 已在跑 → 复用,不 fork,标记 external。"""
    from cc_harness import sandbox_server as ss
    monkeypatch.setattr(ss, "ping", AsyncMock(return_value=True))
    fork = MagicMock()
    monkeypatch.setattr(ss, "_fork_server", fork)
    state = await ss.ensure_server(port=8000)
    assert state.owned is False          # external,退出不 kill
    fork.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_server_starts_when_absent(monkeypatch):
    """server 没跑 + Docker 可用 → fork,标 owned。"""
    from cc_harness import sandbox_server as ss
    monkeypatch.setattr(ss, "ping", AsyncMock(side_effect=[False, True]))  # 先无后有
    monkeypatch.setattr(ss, "_docker_available", MagicMock(return_value=True))
    monkeypatch.setattr(ss, "_fork_server", MagicMock())
    state = await ss.ensure_server(port=8000)
    assert state.owned is True


@pytest.mark.asyncio
async def test_ensure_server_fallback_when_no_docker(monkeypatch):
    """Docker 不可用 → 返回 None(调用方降级 native)。"""
    from cc_harness import sandbox_server as ss
    monkeypatch.setattr(ss, "ping", AsyncMock(return_value=False))
    monkeypatch.setattr(ss, "_docker_available", MagicMock(return_value=False))
    state = await ss.ensure_server(port=8000)
    assert state is None
```

- [ ] **Step 2: 跑测试确认失败**

```
PYTHONIOENCODING=utf-8 D:/agent_learning/cc-harness/.venv/Scripts/python.exe -m pytest tests/test_sandbox_server.py -v
```
Expected: FAIL(ModuleNotFoundError)

- [ ] **Step 3: 创建 `cc_harness/sandbox_server.py`**:

```python
"""opensandbox-server lifecycle:ping / auto-start(setsid 子进程)/ shutdown_owned。

混合策略:先 ping,port 占用就复用(external,退出不 kill);否则检测 Docker,
可用就 fork `uvx opensandbox-server`,轮询等 ready,标记 owned(退出 kill)。
Docker 不可用 / 起不来 → 返回 None(调用方降级 NativeExecutor)。
"""
from __future__ import annotations
import asyncio
import os
import sys
from dataclasses import dataclass


@dataclass
class ServerState:
    owned: bool          # True = 我们起的(退出要 kill);False = external(复用)


async def ping(host: str, port: int, timeout: float = 1.0) -> bool:
    """TCP 探活。连得上 = server 在跑。"""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except (ConnectionRefusedError, asyncio.TimeoutError, OSError):
        return False


def _docker_available() -> bool:
    """docker info 是否可用(子进程,快)。"""
    try:
        r = os.system("docker info > /dev/null 2>&1")
        return r == 0
    except Exception:
        return False


def _fork_server(port: int) -> asyncio.subprocess.Process:
    """fork opensandbox-server 子进程(新会话组,cross-platform start_new_session)。"""
    # 首次:生成 ~/.sandbox.toml(幂等:已存在不覆盖)
    toml = os.path.expanduser("~/.sandbox.toml")
    if not os.path.exists(toml):
        os.system(f'uvx opensandbox-server init-config "{toml}" --example docker > /dev/null 2>&1')
    return asyncio.create_subprocess_exec(
        sys.executable, "-m", "opensandbox_server",  # 或 "uvx", "opensandbox-server"
        "--port", str(port),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,            # Unix setsid;Windows no-op 但不报错
    )


async def ensure_server(port: int, host: str = "localhost",
                        ready_timeout: float = 30.0) -> ServerState | None:
    """确保 server 在跑。复用 / 自动起 / Docker 不可用返回 None。"""
    if await ping(host, port):
        return ServerState(owned=False)     # external
    if not _docker_available():
        return None                          # 调用方降级 native
    proc = await _fork_server(port)
    # 轮询等 ready
    deadline = asyncio.get_event_loop().time() + ready_timeout
    while asyncio.get_event_loop().time() < deadline:
        if await ping(host, port):
            _OWNED_PROC[0] = proc
            return ServerState(owned=True)
        await asyncio.sleep(0.5)
    # 超时没起来 —— kill 残进程,返回 None
    try:
        proc.kill()
    except Exception:
        pass
    return None


_OWNED_PROC: list = [None]   # 模块级持有 owned 子进程


async def shutdown_owned() -> None:
    """退出时 kill 我们起的 server 子进程(整组)。external 的不动。"""
    proc = _OWNED_PROC[0]
    if proc is None:
        return
    try:
        proc.kill()
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        pass
    _OWNED_PROC[0] = None
```

> 注:`create_subprocess_exec` 的命令(`python -m opensandbox_server` vs `uvx opensandbox-server`)在 Task 12 集成时按实际包入口锁定;单元测试 mock `_fork_server` 不依赖真命令。

- [ ] **Step 4: 跑测试确认通过**

```
PYTHONIOENCODING=utf-8 D:/agent_learning/cc-harness/.venv/Scripts/python.exe -m pytest tests/test_sandbox_server.py -v
```
Expected: PASS(5 个)

- [ ] **Step 5: ruff + commit**

```
D:/agent_learning/cc-harness/.venv/Scripts/python.exe -m ruff check cc_harness/sandbox_server.py tests/test_sandbox_server.py
```
```bash
git -C D:/agent_learning/cc-harness add cc_harness/sandbox_server.py tests/test_sandbox_server.py
git -C D:/agent_learning/cc-harness commit -m "feat(sandbox_server): opensandbox-server ping/auto-start/shutdown

混合 lifecycle:ping 复用(external)/ Docker 可用则 fork setsid 子进程等
ready(owned)/ 不可用返回 None 调用方降级。退出 shutdown_owned 整组 kill。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: sandbox.py — SandboxExecutor 核心(lazy create + commands.run + ToolResult)

**Files:**
- Create: `cc_harness/sandbox.py`
- Test: `tests/test_sandbox.py`

- [ ] **Step 1: 写失败测试** — `tests/test_sandbox.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_run_returns_stdout_as_toolresult(tmp_path):
    """commands.run 返回的 stdout → ToolResult.success。"""
    from cc_harness.sandbox import SandboxExecutor
    from cc_harness.config import ExecutorConfig, SandboxConfig

    # mock OpenSandbox SDK: Sandbox.create → 假 sandbox,commands.run → "hello\n"
    fake_exec = MagicMock()
    fake_exec.exit_code = 0
    fake_exec.logs.stdout = [MagicMock(text="hello\n")]
    fake_sandbox = MagicMock()
    fake_sandbox.commands.run = AsyncMock(return_value=fake_exec)
    fake_sandbox.kill = AsyncMock()

    with patch("cc_harness.sandbox.Sandbox") as SDK:
        SDK.create = AsyncMock(return_value=fake_sandbox)
        ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
        result = await ex.run({"command": "echo hello"}, cwd=tmp_path)
    assert "hello" in result.llm_text
    assert result.error is False


@pytest.mark.asyncio
async def test_run_nonzero_exit_returns_error_toolresult(tmp_path):
    from cc_harness.sandbox import SandboxExecutor
    from cc_harness.config import SandboxConfig
    fake_exec = MagicMock()
    fake_exec.exit_code = 2
    fake_exec.logs.stdout = [MagicMock(text="")]
    fake_exec.logs.stderr = [MagicMock(text="boom")]
    fake_sandbox = MagicMock()
    fake_sandbox.commands.run = AsyncMock(return_value=fake_exec)
    fake_sandbox.kill = AsyncMock()
    with patch("cc_harness.sandbox.Sandbox") as SDK:
        SDK.create = AsyncMock(return_value=fake_sandbox)
        ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
        result = await ex.run({"command": "bad-cmd"}, cwd=tmp_path)
    assert result.error is True
    assert "boom" in result.llm_text
```

- [ ] **Step 2: 跑测试确认失败**

```
PYTHONIOENCODING=utf-8 D:/agent_learning/cc-harness/.venv/Scripts/python.exe -m pytest tests/test_sandbox.py -v
```
Expected: FAIL(ModuleNotFoundError: cc_harness.sandbox)

- [ ] **Step 3: 创建 `cc_harness/sandbox.py`**(核心,先无 retry/mount/vault,后续 task 加):

```python
"""SandboxExecutor:OpenSandbox SDK 封装,实现 Executor 协议。

会话级 lazy create sandbox(首次 run 建,后续复用);commands.run 收
stdout/stderr/exit → ToolResult(格式同 NativeExecutor)。
"""
from __future__ import annotations
import asyncio
from pathlib import Path

from cc_harness.config import SandboxConfig
from cc_harness.executor import strip_secrets
from cc_harness.mcp_client import ToolResult

# OpenSandbox SDK(lazy import:无 [sandbox] extra 时模块加载不崩,调用时报错)
try:
    from opensandbox import Sandbox
except ImportError:
    Sandbox = None


class SandboxExecutor:
    def __init__(self, cfg: SandboxConfig, project_root: Path) -> None:
        self.cfg = cfg
        self.project_root = Path(project_root).resolve()
        self._sandbox = None     # lazy create,会话级复用

    async def _ensure_sandbox(self):
        if self._sandbox is not None:
            return self._sandbox
        if Sandbox is None:
            raise RuntimeError("opensandbox SDK 未装(pip install -e '.[sandbox]')")
        # mount / env / vault 在 Task 4/6 扩展;先最小 create
        self._sandbox = await Sandbox.create(
            self.cfg.image,
            env=strip_secrets(dict(__import__("os").environ)),
            timeout=asyncio.timedelta(seconds=self.cfg.timeout_s),
        )
        return self._sandbox

    async def run(self, args: dict, *, cwd: Path) -> ToolResult:
        command = args.get("command", "")
        if not isinstance(command, str) or not command.strip():
            return ToolResult.error(
                display="'command' must be a non-empty string",
                llm="[Tool Error] 'command' must be a non-empty string",
            )
        try:
            sb = await self._ensure_sandbox()
            execution = await sb.commands.run(command)
        except Exception as e:
            return ToolResult.error(
                display=f"sandbox run failed: {e}",
                llm=f"[Tool Error] sandbox: {type(e).__name__}: {e}",
            )
        stdout = "".join(log.text for log in (execution.logs.stdout or []))
        stderr = "".join(log.text for log in (execution.logs.stderr or []))
        if execution.exit_code != 0:
            combined = (stdout + stderr).strip() or f"(no output, exit {execution.exit_code})"
            return ToolResult.error(
                display=f"exit {execution.exit_code}: {combined[:200]}",
                llm=f"[Tool Error] exit {execution.exit_code}\nstdout: {stdout}\nstderr: {stderr}",
            )
        return ToolResult.success(stdout if stdout else "(no output)")

    async def kill(self):
        """会话结束清理(sandbox + server 由 sandbox_server 管 server 进程)。"""
        if self._sandbox is not None:
            try:
                await self._sandbox.kill()
            except Exception:
                pass
            self._sandbox = None
```

- [ ] **Step 4: 跑测试确认通过**

```
PYTHONIOENCODING=utf-8 D:/agent_learning/cc-harness/.venv/Scripts/python.exe -m pytest tests/test_sandbox.py -v
```
Expected: PASS(2 个)

- [ ] **Step 5: ruff + commit**

```
D:/agent_learning/cc-harness/.venv/Scripts/python.exe -m ruff check cc_harness/sandbox.py tests/test_sandbox.py
```
```bash
git -C D:/agent_learning/cc-harness add cc_harness/sandbox.py tests/test_sandbox.py
git -C D:/agent_learning/cc-harness commit -m "feat(sandbox): SandboxExecutor 核心(lazy create + commands.run)

OpenSandbox SDK 封装,实现 Executor 协议。会话级 lazy create sandbox,
commands.run 收 stdout/stderr/exit → ToolResult(格式同 NativeExecutor)。
SDK lazy import(无 [sandbox] extra 模块加载不崩)。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: sandbox.py — mount(项目根 RO → /workspace)+ workdir

**Files:**
- Modify: `cc_harness/sandbox.py`(`_ensure_sandbox` 加 mount)
- Test: `tests/test_sandbox.py`(augment)

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_ensure_sandbox_passes_mount(tmp_path):
    """Sandbox.create 收到项目根 RO mount + /tmp/work workdir。"""
    from cc_harness.sandbox import SandboxExecutor
    from cc_harness.config import SandboxConfig
    fake_sandbox = MagicMock()
    fake_sandbox.kill = AsyncMock()
    captured = {}
    async def fake_create(*args, **kw):
        captured.update(kw)
        return fake_sandbox
    with patch("cc_harness.sandbox.Sandbox") as SDK:
        SDK.create = fake_create
        ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
        await ex.run({"command": "ls"}, cwd=tmp_path)
    # mount 含项目根(RO)+ workdir
    mounts = captured.get("mounts") or []
    assert any(str(tmp_path) in str(m) for m in mounts), "缺项目根 mount"
```

- [ ] **Step 2: 跑测试确认失败**

```
PYTHONIOENCODING=utf-8 D:/agent_learning/cc-harness/.venv/Scripts/python.exe -m pytest tests/test_sandbox.py::test_ensure_sandbox_passes_mount -v
```
Expected: FAIL(当前 `_ensure_sandbox` 没传 mounts)

- [ ] **Step 3: 改 `_ensure_sandbox`** — 加 mount 配置(具体 OpenSandbox mount API 在 Task 12 集成时按 SDK 实际签名锁定;此处用 `mounts=` kwarg):

```python
    async def _ensure_sandbox(self):
        if self._sandbox is not None:
            return self._sandbox
        if Sandbox is None:
            raise RuntimeError("opensandbox SDK 未装(pip install -e '.[sandbox]')")
        import os
        from opensandbox.models import Mount  # Task 12 按实际 API 锁定
        self._sandbox = await Sandbox.create(
            self.cfg.image,
            mounts=[
                # 项目根 RO → /workspace(实时反映 agent 用 fs 工具改的代码)
                Mount(source=str(self.project_root), target="/workspace", read_only=True),
            ],
            workdir="/tmp/work",     # 可写,沙箱内,销毁即清
            env=strip_secrets(dict(os.environ)),
            timeout=__import__("asyncio").timedelta(seconds=self.cfg.timeout_s),
        )
        return self._sandbox
```

- [ ] **Step 4: 跑测试确认通过**(测试 mock `Sandbox`,Mount import 在 try/except 内防 SDK 未装;若 `opensandbox.models.Mount` 签名不同,Task 12 集成时校正)

```
PYTHONIOENCODING=utf-8 D:/agent_learning/cc-harness/.venv/Scripts/python.exe -m pytest tests/test_sandbox.py -v
```

- [ ] **Step 5: ruff + commit**

```bash
git -C D:/agent_learning/cc-harness add cc_harness/sandbox.py tests/test_sandbox.py
git -C D:/agent_learning/cc-harness commit -m "feat(sandbox): mount 项目根 RO + /tmp/work workdir

RO mount 实时反映 fs 工具改动(读一致);workdir 写隔离(销毁即清)。
具体 Mount API 集成时(Task 12)按 SDK 实际签名锁定。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: sandbox.py — 重试 3 次 + 降级 native

**Files:**
- Modify: `cc_harness/sandbox.py`(`run` 加 retry + 降级)
- Test: `tests/test_sandbox.py`(augment)

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_run_retries_then_succeeds(tmp_path):
    """create 失败 2 次第 3 次成功 → 不降级,返回结果。"""
    from cc_harness.sandbox import SandboxExecutor
    from cc_harness.config import SandboxConfig
    fake_exec = MagicMock(exit_code=0, logs=MagicMock(stdout=[MagicMock(text="ok")], stderr=[]))
    fake_sandbox = MagicMock()
    fake_sandbox.commands.run = AsyncMock(return_value=fake_exec)
    fake_sandbox.kill = AsyncMock()
    calls = {"n": 0}
    async def flaky_create(*a, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("transient")
        return fake_sandbox
    with patch("cc_harness.sandbox.Sandbox") as SDK:
        SDK.create = flaky_create
        ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
        result = await ex.run({"command": "echo ok"}, cwd=tmp_path)
    assert calls["n"] == 3
    assert "ok" in result.llm_text


@pytest.mark.asyncio
async def test_run_falls_back_after_3_failures(tmp_path):
    """create 连败 3 次 → run 抛 SandboxUnavailableError(调用方降级 native)。"""
    from cc_harness.sandbox import SandboxExecutor, SandboxUnavailableError
    from cc_harness.config import SandboxConfig
    with patch("cc_harness.sandbox.Sandbox") as SDK:
        SDK.create = AsyncMock(side_effect=ConnectionError("down"))
        ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
        with pytest.raises(SandboxUnavailableError):
            await ex.run({"command": "echo"}, cwd=tmp_path)
```

- [ ] **Step 2: 跑测试确认失败**

```
PYTHONIOENCODING=utf-8 D:/agent_learning/cc-harness/.venv/Scripts/python.exe -m pytest tests/test_sandbox.py -k "retries or falls_back" -v
```
Expected: FAIL(无重试逻辑 / 无 SandboxUnavailableError)

- [ ] **Step 3: 改 `sandbox.py`** — 加 `SandboxUnavailableError` + retry 包装:

```python
class SandboxUnavailableError(RuntimeError):
    """沙箱层连败 3 次,调用方应降级 NativeExecutor。"""


async def _with_retry(coro_factory, attempts: int = 3):
    """指数退避 1s/2s/4s。返回 coro 结果;全败抛 last exception。"""
    last = None
    for i in range(attempts):
        try:
            return await coro_factory()
        except Exception as e:
            last = e
            if i < attempts - 1:
                await asyncio.sleep(2 ** i)
    raise last
```

把 `_ensure_sandbox` 里 `await Sandbox.create(...)` 换成 `await _with_retry(lambda: Sandbox.create(...))`;`run` 里 `sb.commands.run` 同样换。`run` 顶层包 try/except 捕 `SandboxUnavailableError` 后**重新抛出**(不在 SandboxExecutor 内降级——降级由 `build_executor`/调用方决定,见 spec §7.1):
```python
        try:
            sb = await self._ensure_sandbox()    # 内含 retry,3 次后抛
            execution = await _with_retry(lambda: sb.commands.run(command))
        except SandboxUnavailableError:
            raise        # 让调用方(build_executor 包装处)降级
        except Exception as e:
            return ToolResult.error(...)
```

> 设计说明:重试在 SandboxExecutor 内,降级在调用方(`tools.py:get_session_executor` 的 run 调用包 try/except,捕 SandboxUnavailableError → fallback NativeExecutor)。这样 SandboxExecutor 单一职责(跑沙箱),降级策略在接线点。

- [ ] **Step 4: 跑测试确认通过**

```
PYTHONIOENCODING=utf-8 D:/agent_learning/cc-harness/.venv/Scripts/python.exe -m pytest tests/test_sandbox.py -v
```
Expected: PASS(4 个)

- [ ] **Step 5: ruff + commit**

```bash
git -C D:/agent_learning/cc-harness add cc_harness/sandbox.py tests/test_sandbox.py
git -C D:/agent_learning/cc-harness commit -m "feat(sandbox): 重试 3 次(指数退避)+ SandboxUnavailableError

create/commands.run 通信错重试 3 次(1s/2s/4s);全败抛 SandboxUnavailableError
让调用方降级 native。命令结果(exit≠0)不重试。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 6: sandbox.py — Vault / env-strip 兜底

**Files:**
- Modify: `cc_harness/sandbox.py`(`_ensure_sandbox` env 构造)
- Test: `tests/test_sandbox.py`(augment)

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_env_stripped_no_secrets(tmp_path):
    """沙箱 env 不含 KEY/TOKEN/SECRET(Vault 未接时 strip_secrets 兜底)。"""
    import os
    from cc_harness.sandbox import SandboxExecutor
    from cc_harness.config import SandboxConfig
    captured = {}
    async def fake_create(*a, **kw):
        captured.update(kw)
        return MagicMock(kill=AsyncMock(),
                         commands=MagicMock(run=AsyncMock(return_value=MagicMock(
                             exit_code=0, logs=MagicMock(stdout=[MagicMock(text="")], stderr=[])))))
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-leak", "PATH": "/x"}):
        with patch("cc_harness.sandbox.Sandbox") as SDK:
            SDK.create = fake_create
            ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
            await ex.run({"command": "env"}, cwd=tmp_path)
    env = captured.get("env", {})
    assert "OPENAI_API_KEY" not in env, "密钥泄露进沙箱 env"
    assert "PATH" in env                      # 非密钥保留
```

- [ ] **Step 2: 跑测试确认失败 / 通过**(Task 3 已用 strip_secrets,应已通过——若未通过则修)

- [ ] **Step 3: 确认 `_ensure_sandbox` 用 `strip_secrets(dict(os.environ))`**(Task 3 已写)。Vault 真接入(egress 白名单 + 凭证注册到 OpenSandbox Vault)在 Task 12 集成时按 OpenSandbox Vault 文档加;此处测试保证 strip_secrets 兜底不泄露。

> 注:`vault: True` 时 plan 阶段查 OpenSandbox Vault API(`Sandbox.create(credential_vault=...)` 或类似),把 .env 凭证注册;**当前测试只验 strip_secrets 兜底**(spec §9 允许 Vault 分阶段,先 strip_secrets)。

- [ ] **Step 4: 跑测试确认通过**

- [ ] **Step 5: ruff + commit**

```bash
git -C D:/agent_learning/cc-harness add cc_harness/sandbox.py tests/test_sandbox.py
git -C D:/agent_learning/cc-harness commit -m "test(sandbox): 沙箱 env strip_secrets 兜底(密钥不进 env)

Vault 真接入待集成(Task 12 查 API);当前 strip_secrets 保证密钥不泄露进沙箱。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 7: sandbox.py — 会话级 kill + audit

**Files:**
- Modify: `cc_harness/sandbox.py`(kill + 审计落 logs/sandbox.jsonl)
- Test: `tests/test_sandbox.py`(augment)

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_kill_destroys_sandbox(tmp_path):
    from cc_harness.sandbox import SandboxExecutor
    from cc_harness.config import SandboxConfig
    fake_sandbox = MagicMock(kill=AsyncMock())
    with patch("cc_harness.sandbox.Sandbox") as SDK:
        SDK.create = AsyncMock(return_value=fake_sandbox)
        ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
        await ex.run({"command": "x"}, cwd=tmp_path)
        await ex.kill()
    fake_sandbox.kill.assert_awaited()
    assert ex._sandbox is None     # kill 后清空,下次 run 重建


@pytest.mark.asyncio
async def test_fallback_audited(tmp_path, monkeypatch):
    """降级事件落 logs/sandbox.jsonl。"""
    from cc_harness.sandbox import SandboxExecutor, SandboxUnavailableError
    from cc_harness.config import SandboxConfig
    logged = []
    monkeypatch.setattr("cc_harness.sandbox._audit_fallback",
                        lambda **kw: logged.append(kw))
    with patch("cc_harness.sandbox.Sandbox") as SDK:
        SDK.create = AsyncMock(side_effect=ConnectionError("down"))
        ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
        with pytest.raises(SandboxUnavailableError):
            await ex.run({"command": "x"}, cwd=tmp_path)
    assert logged and logged[0]["retries"] == 3
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 加 `_audit_fallback`**(写 logs/sandbox.jsonl,沿用 `audit.py` 模式):

```python
import json, time
def _audit_fallback(project_root: Path, reason: str, retries: int = 3):
    log = project_root / "logs" / "sandbox.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "action": "fallback_after_retry",
                            "reason": reason, "retries": retries}) + "\n")
```

`run` 捕 `SandboxUnavailableError` 后调 `_audit_fallback` 再抛:

```python
        except SandboxUnavailableError as e:
            _audit_fallback(self.project_root, reason=str(e), retries=3)
            raise
```

- [ ] **Step 4: 跑测试确认通过**

- [ ] **Step 5: ruff + commit**

```bash
git -C D:/agent_learning/cc-harness add cc_harness/sandbox.py tests/test_sandbox.py
git -C D:/agent_learning/cc-harness commit -m "feat(sandbox): kill 清理 + 降级审计 logs/sandbox.jsonl

会话结束 kill sandbox;降级(action=fallback_after_retry,retries)落审计。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 8: executor.py — build_executor 工厂

**Files:**
- Modify: `cc_harness/executor.py`(加 `build_executor`)
- Test: `tests/test_executor.py`(augment;若不存在则建)

- [ ] **Step 1: 写失败测试**

```python
import pytest
from unittest.mock import patch


def test_build_executor_native():
    from cc_harness.executor import build_executor, NativeExecutor
    from cc_harness.config import ExecutorConfig, ExecutorBackend
    ex = build_executor(ExecutorConfig(backend=ExecutorBackend.NATIVE),
                        project_root=__import__("pathlib").Path("/tmp"))
    assert isinstance(ex, NativeExecutor)


def test_build_executor_sandbox():
    from cc_harness.executor import build_executor
    from cc_harness.config import ExecutorConfig, ExecutorBackend
    from cc_harness.sandbox import SandboxExecutor
    ex = build_executor(ExecutorConfig(backend=ExecutorBackend.SANDBOX),
                        project_root=__import__("pathlib").Path("/tmp"))
    assert isinstance(ex, SandboxExecutor)


def test_build_executor_disabled_forces_native():
    """enabled=False → 即使 backend=sandbox 也强制 native(紧急回退)。"""
    from cc_harness.executor import build_executor, NativeExecutor
    from cc_harness.config import ExecutorConfig, ExecutorBackend
    ex = build_executor(ExecutorConfig(enabled=False, backend=ExecutorBackend.SANDBOX),
                        project_root=__import__("pathlib").Path("/tmp"))
    assert isinstance(ex, NativeExecutor)
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 改 `cc_harness/executor.py`** — 加工厂(`Executor` 协议不动):

```python
def build_executor(cfg, project_root):
    """按 ExecutorConfig 选 NativeExecutor / SandboxExecutor。
    cfg.enabled=False 强制 native(紧急 kill-switch)。"""
    from cc_harness.config import ExecutorBackend
    if not cfg.enabled or cfg.backend is ExecutorBackend.NATIVE:
        return NativeExecutor(project_root=project_root)
    # sandbox
    from cc_harness.sandbox import SandboxExecutor
    return SandboxExecutor(cfg.sandbox, project_root=project_root)
```

- [ ] **Step 4: 跑测试确认通过**

- [ ] **Step 5: ruff + commit**

```bash
git -C D:/agent_learning/cc-harness add cc_harness/executor.py tests/test_executor.py
git -C D:/agent_learning/cc-harness commit -m "feat(executor): build_executor 工厂(native/sandbox 切换)

按 ExecutorConfig.backend 选;enabled=False 强制 native(kill-switch)。
Executor 协议不动。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 9: tools.py — session executor 单例 + run_command 改

**Files:**
- Modify: `cc_harness/tools.py`(`run_command` + 加 session 单例 API)
- Test: `tests/test_tools.py`(augment)

- [ ] **Step 1: 写失败测试**

```python
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_run_command_uses_session_executor(monkeypatch, tmp_path):
    """run_command 经 get_session_executor().run(),不再内联 NativeExecutor。"""
    from cc_harness import tools
    fake = MagicMock()
    fake.run = AsyncMock(return_value=MagicMock(llm_text="hi", error=False))
    monkeypatch.setattr(tools, "get_session_executor", lambda: fake)
    await tools.run_command({"command": "echo hi"}, cwd=str(tmp_path))
    fake.run.assert_awaited_once()


def test_init_then_get_returns_same(monkeypatch, tmp_path):
    """init_session_executor 后 get 返回同一实例(会话级复用)。"""
    from cc_harness import tools
    from cc_harness.config import ExecutorConfig
    tools.reset_session_executor()
    tools.init_session_executor(ExecutorConfig(), tmp_path)
    a = tools.get_session_executor()
    b = tools.get_session_executor()
    assert a is b


@pytest.mark.asyncio
async def test_run_falls_back_to_native_on_sandbox_unavailable(monkeypatch, tmp_path):
    """sandbox 连败 → run 内部降级 native(用户无感,警告)。"""
    from cc_harness import tools
    from cc_harness.sandbox import SandboxUnavailableError
    sb = MagicMock()
    sb.run = AsyncMock(side_effect=SandboxUnavailableError("down"))
    native = MagicMock()
    native.run = AsyncMock(return_value=MagicMock(llm_text="fallback", error=False))
    monkeypatch.setattr(tools, "get_session_executor", lambda: sb)
    monkeypatch.setattr(tools, "_native_fallback",
                        lambda cwd: native)   # 降级构造器
    result = await tools.run_command({"command": "echo"}, cwd=str(tmp_path))
    assert "fallback" in result.llm_text
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 改 `cc_harness/tools.py`** — 替换 `run_command` 内联 + 加 session API:

```python
from typing import Optional
from pathlib import Path
from cc_harness.executor import Executor, NativeExecutor

_session_executor: Optional[Executor] = None


def init_session_executor(config, project_root):
    """repl 启动调:按 config.backend 建会话级 executor。"""
    global _session_executor
    from cc_harness.executor import build_executor
    _session_executor = build_executor(config, Path(project_root))


def get_session_executor() -> Executor:
    """run_command 取;未 init(repl 外,如测试)lazy 兜底 NativeExecutor。"""
    global _session_executor
    if _session_executor is None:
        _session_executor = NativeExecutor(project_root=Path("."), timeout_s=RUN_COMMAND_TIMEOUT_S)
    return _session_executor


def reset_session_executor():
    """测试隔离。"""
    global _session_executor
    _session_executor = None


async def shutdown_session_executor():
    """repl 退出调:sandbox 时 kill + shutdown_owned_server。"""
    global _session_executor
    if _session_executor is None:
        return
    kill = getattr(_session_executor, "kill", None)
    if kill is not None:
        try:
            await kill()
        except Exception:
            pass
    # server 子进程(若 owned)
    from cc_harness.sandbox_server import shutdown_owned
    try:
        await shutdown_owned()
    except Exception:
        pass
    _session_executor = None


def _native_fallback(cwd):
    """sandbox 降级用。"""
    return NativeExecutor(project_root=Path(cwd), timeout_s=RUN_COMMAND_TIMEOUT_S)


async def run_command(args: dict, *, cwd: str = ".") -> ToolResult:
    """走 session executor;sandbox 连败 3 次 → 降级 native + 警告。"""
    from cc_harness.sandbox import SandboxUnavailableError
    try:
        return await get_session_executor().run(args, cwd=Path(cwd))
    except SandboxUnavailableError:
        print("[warn] 沙箱不可用,降级 native 执行(非隔离模式)")
        return await _native_fallback(cwd).run(args, cwd=Path(cwd))
```

- [ ] **Step 4: 跑测试确认通过**

```
PYTHONIOENCODING=utf-8 D:/agent_learning/cc-harness/.venv/Scripts/python.exe -m pytest tests/test_tools.py tests/test_agent.py -v
```
Expected: PASS(含现有 agent 测试不回归——agent.py 不动,handler 仍 `NATIVE_TOOLS[name]["handler"]`)

- [ ] **Step 5: ruff + commit**

```bash
git -C D:/agent_learning/cc-harness add cc_harness/tools.py tests/test_tools.py
git -C D:/agent_learning/cc-harness commit -m "feat(tools): session executor 单例 + run_command 走 get_session_executor

init/get/shutdown/reset 模块级单例(会话级复用)。run_command 替换内联
NativeExecutor 构造;sandbox 连败 3 次 → 降级 native + 警告。agent.py 不动。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 10: repl.py — init/shutdown 钩子

**Files:**
- Modify: `cc_harness/repl.py`(启动 init / 主循环退出 shutdown)
- Test: `tests/test_repl.py`(augment;mock `_read_user`)

- [ ] **Step 1: 写失败测试** — 验证 repl 启动调 init、退出调 shutdown:

```python
@pytest.mark.asyncio
async def test_repl_inits_and_shuts_down_executor(monkeypatch, tmp_path):
    from cc_harness import repl, tools
    monkeypatch.setattr(repl, "_read_user", lambda **kw: "exit")
    init_calls, shutdown_calls = [], []
    monkeypatch.setattr(tools, "init_session_executor",
                        lambda c, r: init_calls.append((c, r)))
    monkeypatch.setattr(tools, "shutdown_session_executor",
                        lambda: shutdown_calls.append(1))
    # 使 shutdown 可 await:包一层 async
    async def _aw():
        pass
    monkeypatch.setattr(tools, "shutdown_session_executor",
                        lambda: shutdown_calls.append(1) or _aw())
    await repl.run_repl(...)
    assert init_calls, "repl 启动未调 init_session_executor"
    assert shutdown_calls, "repl 退出未调 shutdown_session_executor"
```

> 注:`run_repl` 签名按现有补齐参数;测试用现有 `test_repl.py` 的 fixture 风格(mock `_read_user` 喂 "exit" 退出)。

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 改 `cc_harness/repl.py`** — `run_repl` 开头(init)、主循环正常退出路径(shutdown):

```python
    # 启动钩子(读 policy.yaml → ExecutorConfig → init session executor)
    from cc_harness.config import load_executor_config
    from cc_harness.tools import init_session_executor, shutdown_session_executor
    policy_path = project_root / "policy.yaml"
    exec_cfg = load_executor_config(policy_path)
    init_session_executor(exec_cfg, project_root)
    try:
        # ... 现有主循环 ...
    finally:
        await shutdown_session_executor()    # 非 atexit(async,主循环退出 finally)
```

- [ ] **Step 4: 跑测试确认通过**(含现有 repl 测试不回归)

- [ ] **Step 5: ruff + commit**

```bash
git -C D:/agent_learning/cc-harness add cc_harness/repl.py tests/test_repl.py
git -C D:/agent_learning/cc-harness commit -m "feat(repl): 启动 init / 退出 shutdown session executor

读 policy.yaml ExecutorConfig → init;主循环 finally 调 await shutdown
(async,非 atexit)。kill-switch 在 config.enabled/backend。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 11: prompts.py — tool_discipline 教 agent 写文件用 fs 工具

**Files:**
- Modify: `cc_harness/prompts.py`(`tool_discipline` #3 末追加)
- Test: `tests/test_prompts.py`(augment)

- [ ] **Step 1: 写失败测试**

```python
def test_tool_discipline_warns_shell_redirect_in_sandbox():
    """sandbox 模式 prompt 教 agent:写文件用 fs 工具,别用 shell 重定向(RO 拒)。"""
    from cc_harness.prompts import build_system_prompt
    out = build_system_prompt("/x", mode="coding")
    assert "写文件用文件工具" in out or "别用 shell 重定向" in out, \
        "缺沙箱模式写文件指导"
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 改 `cc_harness/prompts.py`** — `tool_discipline` section body 末追加一句:

```
在沙箱执行模式下,写文件务必用文件类工具(read_file/write_file/edit_file),
不要用 shell 重定向(echo > / cat <<EOF / tee)——命令在沙箱里,项目目录
read-only mount 会拒绝 shell 写;只有文件类工具能改项目文件。
```

- [ ] **Step 4: 跑测试确认通过**

```
PYTHONIOENCODING=utf-8 D:/agent_learning/cc-harness/.venv/Scripts/python.exe -m pytest tests/test_prompts.py -v
```

- [ ] **Step 5: ruff + commit**

```bash
git -C D:/agent_learning/cc-harness add cc_harness/prompts.py tests/test_prompts.py
git -C D:/agent_learning/cc-harness commit -m "feat(prompt): tool_discipline 教沙箱模式写文件用 fs 工具

shell 重定向在沙箱 RO mount 会被拒;教 agent 写文件走文件类工具。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 12: sandboxes/Dockerfile + policy.yaml.example + pyproject [sandbox] + SDK 锁定

**Files:**
- Create: `sandboxes/Dockerfile`
- Modify: `policy.yaml.example`(+ executor: 段)
- Modify: `pyproject.toml`(+ [sandbox] extra)
- 无单测(配置);验证靠 `docker build` + smoke(本地手动)

- [ ] **Step 1: `sandboxes/Dockerfile`**

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
CMD ["sleep", "infinity"]
```

- [ ] **Step 2: `policy.yaml.example`** — 末尾追加:

```yaml
executor:
  enabled: true            # 总开关:false=强制 native(紧急回退现状)
  backend: native          # native(现状)| sandbox(OpenSandbox 隔离)
  sandbox:
    server_port: 8000
    image: cc-harness-runtime:local
    timeout_s: 120
    cpu: 2
    memory_mb: 2048
    egress_allow: [api.deepseek.com, api.siliconflow.cn, pypi.org, github.com]
    vault: true            # Credential Vault(失败退化 strip_secrets)
    fallback_on_error: native   # native(降级)| hard(报错,红队严格测)
```

- [ ] **Step 3: `pyproject.toml`** — `[project.optional-dependencies]` 加(包名按 OpenSandbox PyPI 实际锁定,查 `pip index versions opensandbox` 或 README):

```toml
sandbox = ["opensandbox"]
```

- [ ] **Step 4: 本地 smoke(手动,有 Docker 时)**

```bash
docker build -t cc-harness-runtime:local sandboxes/
pip install -e '.[sandbox]'
uvx opensandbox-server init-config ~/.sandbox.toml --example docker
uvx opensandbox-server --port 8000 &
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -c "
import asyncio; from opensandbox import Sandbox
async def t():
    sb = await Sandbox.create('cc-harness-runtime:local')
    r = await sb.commands.run('echo hi')
    print(r.logs.stdout[0].text); await sb.kill()
asyncio.run(t())"
```
Expected: `hi`(验证 SDK + 镜像 + server 端到端)。**同时校正** Task 4 的 `Mount` API、Task 2 的 server 启动命令、Task 6 的 Vault API 按 OpenSandbox 实际签名(README / `opensandbox --help`)。

- [ ] **Step 5: commit**

```bash
git -C D:/agent_learning/cc-harness add sandboxes/Dockerfile policy.yaml.example pyproject.toml
git -C D:/agent_learning/cc-harness commit -m "feat(sandbox): Dockerfile + policy.yaml executor 段 + [sandbox] extra

轻量运行时镜像(python+node+git);policy.yaml executor 段示例;
pyproject [sandbox]=opensandbox。SDK/mount/vault API 集成时按实际签名锁定。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 13: 集成测试 + 全量回归

**Files:**
- Create: `tests/_test_sandbox_integration.py`(前缀 `_`,pytest 默认不收集,手动跑)
- 无新单元(全量回归)

- [ ] **Step 1: `tests/_test_sandbox_integration.py`**

```python
"""集成测试:真起 Docker + opensandbox-server,端到端验证 SandboxExecutor。
需:docker build -t cc-harness-runtime:local sandboxes/ + uvx opensandbox-server。
手动跑:pytest tests/_test_sandbox_integration.py -v --run-integration
默认不收集(前缀 _)。"""
import asyncio, pytest, tempfile
from pathlib import Path

pytestmark = pytest.mark.skipif(
    not pytest.importorskip("opensandbox", reason="opensandbox 未装"),
    reason="integration")


@pytest.mark.asyncio
async def test_end_to_end_echo():
    from cc_harness.sandbox import SandboxExecutor
    from cc_harness.config import SandboxConfig
    from cc_harness.sandbox_server import ensure_server, shutdown_owned
    state = await ensure_server(port=8000)
    if state is None:
        pytest.skip("opensandbox-server 起不来(Docker?)")
    try:
        ex = SandboxExecutor(SandboxConfig(), project_root=Path(tempfile.mkdtemp()))
        r = await ex.run({"command": "echo integration-ok"}, cwd=Path("."))
        assert "integration-ok" in r.llm_text
        await ex.kill()
    finally:
        await shutdown_owned()
```

- [ ] **Step 2: 全量回归**

```
PYTHONIOENCODING=utf-8 D:/agent_learning/cc-harness/.venv/Scripts/python.exe -m pytest tests/ -q
```
Expected: 全绿(集成测试前缀 `_` 不收集,不影响)

- [ ] **Step 3: ruff 全量**

```
D:/agent_learning/cc-harness/.venv/Scripts/python.exe -m ruff check cc_harness/ tests/
```

- [ ] **Step 4: commit**

```bash
git -C D:/agent_learning/cc-harness add tests/_test_sandbox_integration.py
git -C D:/agent_learning/cc-harness commit -m "test(sandbox): 集成测试(手动,真 Docker+server 端到端)

前缀 _ pytest 默认不收集;CI 不起 Docker,本地手动跑验证 SandboxExecutor 全链路。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## 全部完成后

1. **全量测试**:`pytest tests/ -q`(绿)
2. **集成测试**(本地有 Docker):`pytest tests/_test_sandbox_integration.py -v`
3. **手动 REPL 验证**:配 `policy.yaml: executor.backend: sandbox`,起 cc-harness,跑 `run_command("echo hi")`,确认走沙箱(看 server 子进程起来 + 审计日志)
4. **kill-switch 验证**:`executor.enabled: false` → 回退 native(现状)

## Plan 2(后续,红队适配)

执行器落地后做(依赖 Plan 1):
- wrapper 加 `confirm` 策略参数(deny/allow),allow 模式捕获沙箱 stdout/stderr/exit
- defense_matrix + report_to_md 加 L8 沙箱层,执行类攻击 ASR 单算
- judge 扩展(allow 模式判沙箱隔离)+ 确定性断言(密钥/egress)
- 红队 config 给执行类攻击配 allow provider
