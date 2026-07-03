"""SandboxExecutor:OpenSandbox SDK 封装,实现 Executor 协议。

会话级 lazy create sandbox(首次 run 建,后续复用);commands.run 收
stdout/stderr/exit → ToolResult(格式同 NativeExecutor)。
通信错(create / commands.run 抛异常)经 _with_retry 重试 3 次(1s/2s/4s
指数退避);全败抛 SandboxUnavailableError 让调用方(tools.py)降级 native。
命令结果(exit≠0)是正常返回,不重试。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import timedelta
from pathlib import Path

from cc_harness.config import SandboxConfig
from cc_harness.executor import strip_secrets
from cc_harness.mcp_client import ToolResult

# OpenSandbox SDK(lazy import:无 [sandbox] extra 时模块加载不崩,调用时报错)。
# 真 SDK 签名锁定(opensandbox 0.1.13,inspect.signature 核实,非 WebSearch):
#   - Volume(*, name, host=Host|None, pvc=..., ossfs=..., mountPath, readOnly=False, subPath=None)
#     kwargs 是 camelCase、keyword-only;属性存成 snake_case(mount_path / read_only)。
#   - Host(*, path: str)
#   - ConnectionConfig(*, api_key=None, domain=None, protocol='http', ...)
# fallback stub 镜像真签名(camelCase kwargs + snake_case 属性),让 CI 无 [sandbox] extra
# 时模块加载 + 单元测试(仅 mock Sandbox.create,Volume/Host 仍走 stub)不崩。
try:
    from opensandbox import Sandbox
    from opensandbox.models.sandboxes import Volume, Host
    from opensandbox.config.connection import ConnectionConfig
    _HAS_SANDBOX_SDK = True
except ImportError:  # 无 [sandbox] extra(CI / 基础安装)
    Sandbox = None
    _HAS_SANDBOX_SDK = False
    ConnectionConfig = None

    class Host:
        def __init__(self, *, path: str) -> None:
            self.path = path

        def __repr__(self) -> str:
            # path 用原值嵌入(不用 !r):Windows 路径分隔符 \ 被 !r 转义成 \\,
            # 会让 `str(path) in str(host)` 这类断言在 Windows 上假阴。
            return f"Host(path={self.path})"

    class Volume:
        # 镜像真 SDK:camelCase kwargs、snake_case 属性(repr 与真类一致,
        # 真类 repr 也含 host=Host(path=<原值>) → substring 断言两端都成立)。
        def __init__(self, *, name: str, host: "Host | None" = None,
                     mountPath: str, readOnly: bool = False) -> None:
            self.name = name
            self.host = host
            self.mount_path = mountPath      # 真类属性也是 snake_case
            self.read_only = readOnly

        def __repr__(self) -> str:
            return (f"Volume(name={self.name}, host={self.host}, "
                    f"mount_path={self.mount_path}, read_only={self.read_only})")


class SandboxUnavailableError(RuntimeError):
    """沙箱层连败 3 次;调用方(run_command 包装处)应降级 NativeExecutor。"""


async def _with_retry(coro_factory, attempts: int = 3):
    """指数退避:第 1、2 次重试前睡 1s、2s(第 3 次是最后尝试不睡)。返回 coro 结果;全败抛 SandboxUnavailableError(包 last)。

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


def _audit_fallback(project_root: Path, reason: str, retries: int = 3) -> None:
    """降级审计:写一行 JSON 到 <project_root>/logs/sandbox.jsonl。

    best-effort:IO 失败只吞(降级路径不能再因审计崩;调用方即将 raise,
    若审计抛 OSError 会 mask 真实的 SandboxUnavailableError)。沿用 audit.py 模式。
    """
    entry = {
        # ISO 字符串匹配 audit.py(<root>/logs/*.jsonl 消费方格式统一),
        # 而非 epoch float。
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "action": "fallback_after_retry",
        "reason": reason,
        "retries": retries,
    }
    log = project_root / "logs" / "sandbox.jsonl"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


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
        # Gap 1 修复:确保 opensandbox-server 在跑(复用 external / 自动起 owned / 无 Docker 返 None)。
        # ensure_server 内部已轮询 ready_timeout 等 server 起,故放 _with_retry 外(不重试 server lifecycle);
        # 仅 Sandbox.create 通信步进 _with_retry。state None → SandboxUnavailableError 触发既有降级链。
        # 懒 import(patch 目标 = cc_harness.sandbox_server.ensure_server 属性,单测 monkeypatch 该模块属性即可生效)。
        from cc_harness.sandbox_server import ensure_server
        state = await ensure_server(port=self.cfg.server_port, host="localhost")
        if state is None:
            # Docker 没装/server 起不来 → 直接走降级(run() 的 except SandboxUnavailableError 接住)。
            raise SandboxUnavailableError(
                "opensandbox-server 不可用(Docker 未装/未运行,或 server 起不来)")
        # Gap 2:kwargs 已锁真 SDK(opensandbox 0.1.13,inspect.signature 核实):
        #   volumes=[Volume(name, host=Host(path), mountPath, readOnly)] 替代 mounts=/Mount
        #   (真 SDK 无 Mount 类、无 mounts= 参数);connection_config=ConnectionConfig(domain=...)
        #   指向 opensandbox-server;真 SDK 无 workdir= 参数(已删,工作目录由 mount 决定)。
        # image / env / timeout 与真签名一致,保留。resource / network_policy / credential_proxy
        # reserved(SandboxConfig 死字段,见下方 TODO,SDK 增强时接)。
        # _with_retry 内含 1s/2s/4s 重试,全败抛 SandboxUnavailableError(让调用方降级)。
        # 项目根 RO mount:fs 工具改动实时反映(读一致)。
        cc = (ConnectionConfig(domain=f"localhost:{self.cfg.server_port}")
              if ConnectionConfig is not None else None)
        self._sandbox = await _with_retry(lambda: Sandbox.create(
            self.cfg.image,
            volumes=[
                Volume(name="workspace",
                       host=Host(path=str(self.project_root)),
                       mountPath="/workspace",
                       readOnly=True),
            ],
            env=strip_secrets(dict(os.environ)),
            timeout=timedelta(seconds=self.cfg.timeout_s),
            connection_config=cc,
            # TODO(Gap 2 增强):resource={"cpu": str(self.cfg.cpu), "memory": f"{self.cfg.memory_mb}Mi"},
            #                    network_policy=<NetworkPolicy from egress_allow>,
            #                    credential_proxy=<Vault>
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
        except SandboxUnavailableError as e:
            # 降级前落审计(action/reason/retries → logs/sandbox.jsonl),再上抛让调用方降级 native。
            _audit_fallback(project_root=self.project_root, reason=str(e), retries=3)
            raise
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
