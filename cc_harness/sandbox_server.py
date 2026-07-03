"""opensandbox-server lifecycle:ping / auto-start(setsid 子进程)/ shutdown_owned。

混合策略:先 ping,port 占用就复用(external,退出不 kill);否则检测 Docker,
可用就 fork opensandbox-server(console script)子进程 —— init-config --example docker
生成 toml,--config 起 server,OPENSANDBOX_INSECURE_SERVER=YES 应对空 api_key ——
轮询等 ready,标记 owned(退出 kill)。Docker 不可用 / 起不来 → 返回 None(调用方降级 NativeExecutor)。

⚠️ Windows:host 必须用 127.0.0.1(localhost→IPv6 ::1 连不上绑 IPv4 的 server)。
"""
from __future__ import annotations
import asyncio
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


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


def _kill_proc_tree(proc) -> None:
    """整组 kill 子进程(Unix killpg / Windows taskkill /T),任一失败 fallback proc.kill()。

    opensandbox-server 会 spawn Docker 容器,只 kill 直连子进程会孤儿化容器——
    这正是本模块要防的泄漏。MagicMock 般假 pid / taskkill 非零返回 → fallback proc.kill(),
    保证可测。start_new_session=True 在 Windows 是 no-op,故 Windows 必须走 taskkill /T 树删。
    """
    try:
        if sys.platform != "win32":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            r = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if r.returncode != 0:
                raise RuntimeError(f"taskkill exited {r.returncode}")
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _server_cli() -> Path:
    """opensandbox-server console script 路径(venv 内 .exe / 类 unix 无后缀)。

    真 server 入口是 console script(非 python -m opensandbox_server,该包无 __main__)。
    install 时 pip 在 venv 的 Scripts/bin 下生成可执行入口,与 sys.executable 同目录。
    """
    exe = "opensandbox-server.exe" if sys.platform == "win32" else "opensandbox-server"
    return Path(sys.executable).parent / exe


def _set_config_port(config_path: Path, port: int) -> None:
    """init-config 默认 port 8080,改成我们的 port(简单行替换,避免引入 toml 写库依赖)。

    幂等:已是目标 port 时替换不变。仅替换首次出现的 `port = 8080`(server 段)。
    """
    txt = config_path.read_text(encoding="utf-8")
    txt = txt.replace("port = 8080", f"port = {port}", 1)
    config_path.write_text(txt, encoding="utf-8")


async def _fork_server(port: int, host: str, config_path: Path):
    """fork opensandbox-server(console script)子进程。

    config_path 指定 toml(含 port/host);不存在则 init-config --example docker 生成
    (默认 port 8080、host 127.0.0.1),再 _set_config_port 改成目标 port。
    api_key 空 → 需 env OPENSANDBOX_INSECURE_SERVER=YES(server 才会 insecure ack 启动)。
    返回 asyncio.create_subprocess_exec 协程(await 得 Process)。
    """
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        # init-config 生成 docker example(默认 port 8080,host 127.0.0.1)。
        # best-effort:失败不阻断(下次 ensure_server 仍可尝试);check=False 让非零退出不抛。
        subprocess.run(
            [str(_server_cli()), "init-config", str(config_path), "--example", "docker"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if config_path.exists():
            _set_config_port(config_path, port)   # 8080 → port
    env = {**os.environ, "OPENSANDBOX_INSECURE_SERVER": "YES"}  # api_key 空 → insecure ack
    return await asyncio.create_subprocess_exec(
        str(_server_cli()), "--config", str(config_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,
    )


async def ensure_server(port: int, host: str = "127.0.0.1",
                        ready_timeout: float = 30.0) -> ServerState | None:
    """确保 server 在跑。复用 / 自动起 / Docker 不可用返回 None。

    host 默认 127.0.0.1(非 localhost):Windows localhost 解析到 IPv6 ::1,
    而 server 绑 127.0.0.1(IPv4)→ ping 连不上。显式 127.0.0.1 强制 IPv4。
    """
    if await ping(host, port):
        return ServerState(owned=False)
    if not _docker_available():
        return None
    config_path = Path.home() / ".cc-harness-sandbox.toml"
    proc = await _fork_server(port, host, config_path)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ready_timeout
    while loop.time() < deadline:
        if await ping(host, port):
            if _OWNED_PROC[0] is not None:
                _kill_proc_tree(_OWNED_PROC[0])
            _OWNED_PROC[0] = proc
            return ServerState(owned=True)
        await asyncio.sleep(0.5)
    _kill_proc_tree(proc)
    return None


async def shutdown_owned() -> None:
    """退出时 kill 我们起的 server 子进程(整组)。external 的不动。"""
    proc = _OWNED_PROC[0]
    if proc is None:
        return
    _kill_proc_tree(proc)
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        pass
    _OWNED_PROC[0] = None
