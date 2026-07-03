"""opensandbox-server lifecycle:ping / auto-start(setsid 子进程)/ shutdown_owned。

混合策略:先 ping,port 占用就复用(external,退出不 kill);否则检测 Docker,
可用就 fork `uvx opensandbox-server`,轮询等 ready,标记 owned(退出 kill)。
Docker 不可用 / 起不来 → 返回 None(调用方降级 NativeExecutor)。
"""
from __future__ import annotations
import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass


# 我们起的 server 子进程(退出时 shutdown_owned 整组 kill)。external 的不动。
_OWNED_PROC: list = [None]


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
    """docker info 是否可用(子进程,跨平台,匹配 executor.py 风格)。"""
    try:
        r = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return r.returncode == 0
    except Exception:
        return False


def _fork_server(port: int) -> asyncio.subprocess.Process:
    """fork opensandbox-server 子进程(新会话组,cross-platform start_new_session)。"""
    toml = os.path.expanduser("~/.sandbox.toml")
    if not os.path.exists(toml):
        # best-effort 幂等初始化,失败不阻断(下次 ensure_server 仍可起)
        try:
            subprocess.run(
                ["uvx", "opensandbox-server", "init-config", toml, "--example", "docker"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass
    return asyncio.create_subprocess_exec(  # type: ignore[return-value]
        sys.executable, "-m", "opensandbox_server",
        "--port", str(port),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )


async def ensure_server(port: int, host: str = "localhost",
                        ready_timeout: float = 30.0) -> ServerState | None:
    """确保 server 在跑。复用 / 自动起 / Docker 不可用返回 None。"""
    if await ping(host, port):
        return ServerState(owned=False)
    if not _docker_available():
        return None
    proc = await _fork_server(port)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ready_timeout
    while loop.time() < deadline:
        if await ping(host, port):
            _OWNED_PROC[0] = proc
            return ServerState(owned=True)
        await asyncio.sleep(0.5)
    try:
        proc.kill()
    except Exception:
        pass
    return None


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
