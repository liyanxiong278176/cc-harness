"""SandboxExecutor:OpenSandbox SDK 封装,实现 Executor 协议。

会话级 lazy create sandbox(首次 run 建,后续复用);commands.run 收
stdout/stderr/exit → ToolResult(格式同 NativeExecutor)。
"""
from __future__ import annotations

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
        self._sandbox = await Sandbox.create(
            self.cfg.image,
            mounts=[
                Mount(source=str(self.project_root), target="/workspace", read_only=True),
            ],
            workdir="/tmp/work",
            env=strip_secrets(dict(os.environ)),
            timeout=timedelta(seconds=self.cfg.timeout_s),
        )
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
        """会话结束清理。"""
        if self._sandbox is not None:
            try:
                await self._sandbox.kill()
            except Exception:
                pass
            self._sandbox = None
