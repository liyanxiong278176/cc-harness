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
        self._sandbox = await Sandbox.create(
            self.cfg.image,
            env=strip_secrets(dict(os.environ)),
            timeout=timedelta(seconds=self.cfg.timeout_s),
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
        """会话结束清理。"""
        if self._sandbox is not None:
            try:
                await self._sandbox.kill()
            except Exception:
                pass
            self._sandbox = None
