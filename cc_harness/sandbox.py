"""SandboxExecutor:OpenSandbox SDK 封装,实现 Executor 协议。

会话级 lazy create sandbox(首次 run 建,后续复用);commands.run 收
stdout/stderr/exit → ToolResult(格式同 NativeExecutor)。
通信错(create / commands.run 抛异常)经 _with_retry 重试 3 次(1s/2s/4s
指数退避);全败抛 SandboxUnavailableError 让调用方(tools.py)降级 native。
命令结果(exit≠0)是正常返回,不重试。
"""
from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from pathlib import Path

from cc_harness.config import SandboxConfig
from cc_harness.executor import strip_secrets
from cc_harness.mcp_client import ToolResult

# OpenSandbox SDK(lazy import:无 [sandbox] extra 时模块加载不崩,调用时报错)
try:
    from opensandbox import Sandbox
except ImportError:
    Sandbox = None

# Mount 配置对象(真实 SDK 路径 / fallback)。具体 API 在 Task 12 按 SDK 实际签名锁定。
try:
    from opensandbox.models import Mount   # 真实 SDK 路径(Task 12 锁定)
except ImportError:
    class Mount:                            # fallback:无 SDK extra 时单元测试 / 模块加载用
        def __init__(self, source: str, target: str, read_only: bool = False) -> None:
            self.source, self.target, self.read_only = source, target, read_only

        def __repr__(self) -> str:
            # source 用原值嵌入(不用 !r):Windows 路径分隔符 \ 被 !r 转义成 \\,
            # 会让 `str(path) in str(mount)` 这类断言在 Windows 上假阴。
            return (f"Mount(source={self.source}, target={self.target!r}, "
                    f"read_only={self.read_only})")


class SandboxUnavailableError(RuntimeError):
    """沙箱层连败 3 次,调用方(tools.py:get_session_executor)应降级 NativeExecutor。"""


async def _with_retry(coro_factory, attempts: int = 3):
    """指数退避 1s/2s/4s。返回 coro 结果;全败抛 SandboxUnavailableError(包 last)。

    - coro_factory:零参返回新协程的 callable(每次重试重建协程,避免
      "coroutine was never awaited" / 不能 reuse)。
    - 命令正常返回(exit≠0)不会进异常分支,因此不会被重试——只有通信错
      (create/run 抛异常)才重试。这是设计意图。
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await coro_factory()
        except Exception as e:
            last = e
            if i < attempts - 1:
                await asyncio.sleep(2 ** i)
    # 全败:包成 SandboxUnavailableError,让调用方按统一类型降级。
    raise SandboxUnavailableError(str(last)) from last


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
        # 项目根 RO mount:fs 工具改动实时反映(读一致);workdir 写隔离(销毁即清)。
        # 具体 Mount API 在 Task 12 按 SDK 实际签名锁定。
        # _with_retry 内含 1s/2s/4s 重试,全败抛 SandboxUnavailableError(让调用方降级)。
        self._sandbox = await _with_retry(lambda: Sandbox.create(
            self.cfg.image,
            mounts=[
                Mount(source=str(self.project_root), target="/workspace", read_only=True),
            ],
            workdir="/tmp/work",
            env=strip_secrets(dict(os.environ)),
            timeout=timedelta(seconds=self.cfg.timeout_s),
        ))
        return self._sandbox

    async def run(self, args: dict, *, cwd: Path) -> ToolResult:
        # cwd 接受仅为协议对齐;实际工作目录由 mount/project_root + workdir 决定(Task 12 完整接线)。
        command = args.get("command", "")
        if not isinstance(command, str) or not command.strip():
            return ToolResult.error(
                display="'command' must be a non-empty string",
                llm="[Tool Error] 'command' must be a non-empty string",
            )
        try:
            sb = await self._ensure_sandbox()    # 内含 retry,3 次后抛 SandboxUnavailableError
            execution = await _with_retry(lambda: sb.commands.run(command))
        except SandboxUnavailableError:
            raise        # 让调用方(build_executor / tools.py 包装处)降级 native
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
        """会话结束清理。"""
        if self._sandbox is not None:
            try:
                await self._sandbox.kill()
            except Exception:
                pass
            self._sandbox = None
