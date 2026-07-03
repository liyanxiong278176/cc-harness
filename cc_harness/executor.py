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


def build_executor(cfg, project_root: Path) -> Executor:
    """按 ExecutorConfig 选 NativeExecutor / SandboxExecutor。

    cfg.enabled=False 强制 native(紧急 kill-switch / 回退)。
    SandboxExecutor 局部 import:避免模块加载即拉起 opensandbox SDK import 链
    (无 [sandbox] extra 的环境也能 import executor.py)。
    """
    from cc_harness.config import ExecutorBackend
    if not cfg.enabled or cfg.backend is ExecutorBackend.NATIVE:
        return NativeExecutor(project_root=project_root)
    from cc_harness.sandbox import SandboxExecutor
    return SandboxExecutor(cfg.sandbox, project_root=project_root)
