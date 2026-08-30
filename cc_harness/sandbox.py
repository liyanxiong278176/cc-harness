"""SandboxExecutor:OpenSandbox SDK 封装,实现 Executor 协议。

会话级 lazy create sandbox(首次 run 建,后续复用);commands.run 收
stdout/stderr/exit → ToolResult(格式同 NativeExecutor)。
通信错(create / commands.run 抛异常)经 _with_retry 尝试 3 次(重试前等待 1s/2s);
全败抛 SandboxUnavailableError，调用方 fail closed，不降级到 native。
命令结果(exit≠0)是正常返回,不重试。
"""
from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import logging
import os
import socket
import time
import uuid
from datetime import timedelta
from pathlib import Path

from cc_harness.config import SandboxConfig
from cc_harness.credential_broker import CredentialBroker, CredentialBrokerError
from cc_harness.mcp_client import ToolResult
from cc_harness.sandbox_workspace import WorkspaceMaskPlan, discover_mask_targets

log = logging.getLogger(__name__)

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
    from opensandbox.config.connection import ConnectionConfig
    from opensandbox.models.sandboxes import (
        CredentialProxyConfig,
        Host,
        NetworkPolicy,
        NetworkRule,
        Volume,
    )
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

    class NetworkRule:
        def __init__(self, *, action: str, target: str) -> None:
            self.action = action
            self.target = target

    class NetworkPolicy:
        def __init__(self, *, default_action: str, egress: list[NetworkRule]) -> None:
            self.default_action = default_action
            self.egress = egress

    class CredentialProxyConfig:
        def __init__(self, *, enabled: bool) -> None:
            self.enabled = enabled


class SandboxUnavailableError(RuntimeError):
    """The sandbox could not execute a command after bounded retries."""


def _resolve_egress_target(target: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    hostname = target.removeprefix("*.")
    try:
        answers = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SandboxUnavailableError(f"sandbox egress DNS preflight failed for {hostname}") from exc
    addresses = {ipaddress.ip_address(answer[4][0]) for answer in answers}
    if not addresses:
        raise SandboxUnavailableError(f"sandbox egress DNS returned no addresses for {hostname}")
    unsafe = sorted(str(address) for address in addresses if not address.is_global)
    if unsafe:
        raise SandboxUnavailableError(
            f"sandbox egress target {hostname} resolved to non-public address(es): "
            + ", ".join(unsafe)
        )
    return addresses


async def _validate_egress_targets(targets: list[str]) -> None:
    """Fail closed when an allowed domain currently resolves outside public IP space."""
    for target in targets:
        await asyncio.to_thread(_resolve_egress_target, target)


SANDBOX_RETRY_ATTEMPTS_ENV = "CC_HARNESS_SANDBOX_RETRY_ATTEMPTS"
MAX_SANDBOX_RETRY_ATTEMPTS = 10
# A durable coding run can outlive the SDK's 10-minute default sandbox
# expiration by hours.  Keep the command timeout separate from the sandbox
# lifetime: the former bounds one action, while this value keeps a healthy
# session reusable across model turns.  OpenSandbox's server configuration
# caps this at 24 hours, so the default matches that cap and remains finite.
SANDBOX_LIFETIME_SECONDS_ENV = "CC_HARNESS_SANDBOX_LIFETIME_SECONDS"
DEFAULT_SANDBOX_LIFETIME_SECONDS = 86_400
MAX_SANDBOX_LIFETIME_SECONDS = 86_400


def _resolve_retry_attempts(value: int | float | str | None = None, *, default: int = 3) -> int:
    """Resolve bounded transport retries without changing side-effect semantics.

    A failed HTTP connection is still reported as ``outcome_unknown`` by the
    durable worker; retries only cover the transport setup before that fact is
    committed.  The normal interactive default remains three attempts, while
    long-running CI/agent sessions may opt into a larger (but finite) budget
    through ``CC_HARNESS_SANDBOX_RETRY_ATTEMPTS``.
    """

    raw = value if value is not None else os.getenv(SANDBOX_RETRY_ATTEMPTS_ENV)
    try:
        parsed = int(float(raw)) if raw is not None else int(default)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(1, min(parsed, MAX_SANDBOX_RETRY_ATTEMPTS))


def _resolve_sandbox_lifetime(value: int | float | str | None = None) -> float:
    """Resolve a finite reusable-session lifetime for long durable runs."""

    raw = value if value is not None else os.getenv(SANDBOX_LIFETIME_SECONDS_ENV)
    try:
        parsed = float(raw) if raw is not None else float(DEFAULT_SANDBOX_LIFETIME_SECONDS)
    except (TypeError, ValueError):
        parsed = float(DEFAULT_SANDBOX_LIFETIME_SECONDS)
    if not parsed or not (parsed > 0) or not (parsed < float("inf")):
        parsed = float(DEFAULT_SANDBOX_LIFETIME_SECONDS)
    return min(parsed, float(MAX_SANDBOX_LIFETIME_SECONDS))


async def _with_retry(coro_factory, attempts: int | None = None):
    """指数退避:第 1、2 次重试前睡 1s、2s(第 3 次是最后尝试不睡)。返回 coro 结果;全败抛 SandboxUnavailableError(包 last)。

    - coro_factory:零参返回新协程的 callable(每次重试重建协程,避免
      "coroutine was never awaited" / 不能 reuse)。
    - 命令正常返回(exit≠0)不会进异常分支,因此不会被重试——只有通信错
      (create/run 抛异常)才重试。这是设计意图。
    """
    retry_budget = _resolve_retry_attempts(attempts)
    last: Exception | None = None
    for i in range(retry_budget):
        try:
            return await coro_factory()
        except Exception as e:
            last = e
            if i < retry_budget - 1:
                await asyncio.sleep(2 ** i)
    # 全败:包成 SandboxUnavailableError,让调用方按统一类型降级。
    raise SandboxUnavailableError(str(last)) from last


def _audit_fallback(project_root: Path, reason: str, retries: int = 3) -> None:
    """降级审计:写一行 JSON 到 <project_root>/.cc-harness/logs/sandbox.jsonl。

    best-effort:IO 失败只吞(降级路径不能再因审计崩;调用方即将 raise,
    若审计抛 OSError 会 mask 真实的 SandboxUnavailableError)。沿用 audit.py 模式。
    """
    entry = {
        # ISO 字符串匹配 audit.py(<root>/.cc-harness/logs/*.jsonl 消费方格式统一),
        # 而非 epoch float。
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "action": "fallback_after_retry",
        "reason": reason,
        "retries": retries,
    }
    log = project_root / ".cc-harness" / "logs" / "sandbox.jsonl"
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
        self._mask_plan: WorkspaceMaskPlan | None = None
        # Keep mask mounts below the project root.  Besides making the
        # allowlist stable for externally managed servers, this lets startup
        # prewarm the service without walking the entire repository first.
        self._mask_parent = self.project_root / ".cc-harness" / "workspace-masks"
        self._credential_broker = CredentialBroker(cfg, self.project_root)

    def _network_policy(self) -> NetworkPolicy:
        return NetworkPolicy(
            default_action="deny",
            egress=[
                NetworkRule(action="allow", target=target)
                for target in self.cfg.egress_allow
            ],
        )

    async def _destroy_sandbox(self) -> None:
        sandbox = self._sandbox
        self._sandbox = None
        if sandbox is None:
            return
        try:
            await sandbox.kill()
        finally:
            close = getattr(sandbox, "close", None)
            if close is not None:
                close_result = close()
                if inspect.isawaitable(close_result):
                    await close_result

    async def _refresh_workspace_masks(self) -> None:
        targets = discover_mask_targets(self.project_root)
        signature = tuple((target.relative_path.as_posix(), target.is_dir) for target in targets)
        if self._mask_plan is not None and self._mask_plan.signature == signature:
            return
        await self._destroy_sandbox()
        reusable_root = None
        if self._mask_plan is not None:
            reusable_root = self._mask_plan.root
            self._mask_plan.cleanup()
        if reusable_root is None:
            self._mask_parent.mkdir(parents=True, exist_ok=True)
            reusable_root = self._mask_parent / (
                f"session-{os.getpid()}-{uuid.uuid4().hex}"
            )
        self._mask_plan = WorkspaceMaskPlan.create(targets, root=reusable_root)

    def _volumes(self) -> list[Volume]:
        volumes = [
            Volume(
                name="workspace",
                host=Host(path=str(self.project_root)),
                mountPath="/workspace",
                # The coding workspace is intentionally writable: command
                # tools must be able to create and modify the checked-out
                # project. Sensitive paths are mounted below as empty,
                # read-only overlays and therefore remain masked.
                readOnly=False,
            )
        ]
        if self._mask_plan is None:
            return volumes
        for index, target in enumerate(self._mask_plan.targets):
            volumes.append(
                Volume(
                    name=f"credential-mask-{index}",
                    host=Host(path=str(self._mask_plan.host_path(target))),
                    mountPath=f"/workspace/{target.relative_path.as_posix()}",
                    readOnly=True,
                )
                )
        return volumes

    async def _ensure_server(self):
        """Validate egress and ensure OpenSandbox is healthy for this session.

        This is deliberately separate from ``Sandbox.create`` so the Durable
        supervisor can fail before it accepts work or calls a model.  The
        stable mask parent is part of the host-mount attestation; individual
        empty overlays are created lazily immediately before the first
        container.
        """
        if Sandbox is None:
            raise SandboxUnavailableError(
                "opensandbox SDK 未装(pip install -e '.[sandbox]')"
            )
        await _validate_egress_targets(self.cfg.egress_allow)
        # Lazy import keeps the base package importable without the optional
        # OpenSandbox dependency, while allowing tests to patch the module
        # seam before this method is called.
        from cc_harness.sandbox_server import ensure_server

        allowed_host_paths = [str(self.project_root)]
        if self._mask_plan is not None:
            allowed_host_paths.append(str(self._mask_plan.root))
        else:
            # The actual mask directory is created just before the first
            # container.  Allow its stable parent during startup so an
            # external server can attest the same contract without requiring
            # a dynamic /tmp path in its configuration.
            self._mask_parent.mkdir(parents=True, exist_ok=True)
            allowed_host_paths.append(str(self._mask_parent))
        state = await ensure_server(
            host=self.cfg.server_host,
            port=self.cfg.server_port,
            allowed_host_paths=allowed_host_paths,
            config_path=self.cfg.server_config_path,
            pids_limit=self.cfg.pids_limit,
            require_external_attestation=self.cfg.require_external_attestation,
        )
        if state is None:
            raise SandboxUnavailableError(
                "opensandbox-server 不可用(Docker 未装/未运行,或 server 起不来)"
            )
        return state

    async def prewarm_server(self):
        """Start or reuse the OpenSandbox server without creating a container.

        The server is owned by the current Durable supervisor process when it
        is auto-started.  The eventual sandbox container remains lazy and is
        still created on the first command, preserving the existing session
        reuse and workspace-mask behavior.
        """
        return await self._ensure_server()

    async def _ensure_sandbox(self):
        if self._sandbox is not None:
            return self._sandbox
        # Gap 1 修复:确保 opensandbox-server 在跑(复用 external / 自动起 owned / 无 Docker 返 None)。
        # Server lifecycle is intentionally outside _with_retry; only the
        # Sandbox.create communication step gets bounded retries.
        await self._refresh_workspace_masks()
        await self._ensure_server()
        # Gap 2:kwargs 已锁真 SDK(opensandbox 0.1.13,inspect.signature 核实):
        #   volumes=[Volume(name, host=Host(path), mountPath, readOnly)] 替代 mounts=/Mount
        #   (真 SDK 无 Mount 类、无 mounts= 参数);connection_config=ConnectionConfig(domain=...)
        #   指向 opensandbox-server;真 SDK 无 workdir= 参数(已删,工作目录由 mount 决定)。
        # image / env / timeout 与真签名一致,保留。resource / network_policy / credential_proxy
        # reserved(SandboxConfig 死字段,见下方 TODO,SDK 增强时接)。
        # _with_retry 内含 1s/2s/4s 重试,全败抛 SandboxUnavailableError(让调用方降级)。
        # 项目根 RO mount:fs 工具改动实时反映(读一致)。
        cc = (ConnectionConfig(domain=f"{self.cfg.server_host}:{self.cfg.server_port}")
              if ConnectionConfig is not None else None)
        lifetime = timedelta(seconds=_resolve_sandbox_lifetime())
        candidate = await _with_retry(lambda: Sandbox.create(
            self.cfg.image,
            # The SDK default is 600s. A durable coding run regularly spans
            # longer than that; an expired reused handle otherwise makes the
            # next harmless command hang until the per-action timeout. This is
            # a finite sandbox lifetime, not a command or benchmark timeout.
            timeout=lifetime,
            volumes=self._volumes(),
            # 不传 env=:host(Windows)env(PATH/SYSTEMROOT)注入 Linux 沙箱会破坏容器。
            # 沙箱用容器默认 env;凭证后续走 Credential Vault(Task 12 增强),非 host env。
            # 不传 timeout=(sandbox lifetime,默认 600s):cfg.timeout_s(120s)过短,
            # Windows volume mount 慢 → sandbox 在 health check 期间过期。
            ready_timeout=timedelta(seconds=self.cfg.timeout_s),  # 等 ready:Windows mount 慢,默认 30s 不够
            connection_config=cc,
            resource={"cpu": str(self.cfg.cpu), "memory": f"{self.cfg.memory_mb}Mi"},
            network_policy=self._network_policy(),
            credential_proxy=(
                CredentialProxyConfig(enabled=True) if self.cfg.vault else None
            ),
        ))
        self._sandbox = candidate
        if self.cfg.vault:
            try:
                await self._credential_broker.provision(candidate)
            except CredentialBrokerError as exc:
                await self._destroy_sandbox()
                raise SandboxUnavailableError(str(exc)) from None
        return self._sandbox

    async def run(self, args: dict, *, cwd: Path) -> ToolResult:
        # cwd 接受仅为协议对齐;实际工作目录由 mount/project_root + workdir 决定(Task 12 完整接线)。
        command = args.get("command", "")
        if not isinstance(command, str) or not command.strip():
            return ToolResult.error(
                display="'command' must be a non-empty string",
                llm="[Tool Error] 'command' must be a non-empty string",
            )
        # A detached process cannot be owned or audited by the short-lived
        # sandbox command call.  Fail closed instead of silently ignoring the
        # flag (which would make the model believe a service was started).
        if bool(args.get("background", False)):
            return ToolResult.error(
                display="background execution is only supported by the native executor",
                llm=(
                    "[Tool Error] background execution is unavailable in the sandbox backend; "
                    "use a managed service/run instead"
                ),
                metadata={
                    "background": True,
                    "background_supported": False,
                    "state": "unsupported",
                },
            )
        try:
            await self._refresh_workspace_masks()
            sb = await self._ensure_sandbox()    # 内含 retry,3 次后抛 SandboxUnavailableError
            execution = await asyncio.wait_for(
                _with_retry(lambda: sb.commands.run(command)),
                timeout=self.cfg.timeout_s,
            )
        except TimeoutError:
            await self.kill()
            return ToolResult.error(
                display=f"sandbox timeout after {self.cfg.timeout_s}s; sandbox destroyed",
                llm=(
                    f"[Tool Error] sandbox timeout after {self.cfg.timeout_s}s; "
                    "the sandbox was destroyed"
                ),
                metadata={"exit_code": None, "timed_out": True},
            )
        except SandboxUnavailableError as e:
            # Drop the session handle before surfacing the unknown outcome.
            # The server may have restarted or the container endpoint may be
            # stale; retaining it would make every later continuation reuse a
            # dead client.  We still fail closed (the durable worker records
            # outcome_unknown) and never replay a potentially mutating command.
            await self._destroy_sandbox()
            # 降级前落审计,再上抛让调用方按配置处理。
            _audit_fallback(
                project_root=self.project_root,
                reason=str(e),
                retries=_resolve_retry_attempts(),
            )
            raise
        except Exception as e:
            return ToolResult.error(
                display=f"sandbox run failed: {e}",
                llm=f"[Tool Error] sandbox: {type(e).__name__}: {e}",
                metadata={"exit_code": None, "timed_out": False, "exception": type(e).__name__},
            )
        stdout = "".join(log.text for log in (execution.logs.stdout or []))
        stderr = "".join(log.text for log in (execution.logs.stderr or []))
        if execution.exit_code != 0:
            combined = (stdout + stderr).strip() or f"(no output, exit {execution.exit_code})"
            return ToolResult.error(
                display=f"exit {execution.exit_code}: {combined[:200]}",
                llm=f"[Tool Error] exit {execution.exit_code}\nstdout: {stdout}\nstderr: {stderr}",
                metadata={
                    "exit_code": execution.exit_code,
                    "timed_out": False,
                    "stdout": stdout,
                    "stderr": stderr,
                },
            )
        return ToolResult.success(
            stdout if stdout else "(no output)",
            metadata={
                "exit_code": 0,
                "timed_out": False,
                "stdout": stdout,
                "stderr": stderr,
            },
        )

    async def kill(self) -> bool:
        """会话结束清理。"""
        destroyed = True
        if self._sandbox is not None and self.cfg.vault:
            try:
                await self._credential_broker.revoke(self._sandbox)
            except CredentialBrokerError:
                destroyed = False
                log.warning("credential vault revocation failed; destroying sandbox", exc_info=True)
        try:
            await self._destroy_sandbox()
        except Exception:
            destroyed = False
            log.warning("sandbox teardown failed", exc_info=True)
        if self._mask_plan is not None:
            self._mask_plan.cleanup()
            self._mask_plan = None
        return destroyed
