"""集成测试:真起 Docker + opensandbox-server,端到端验证 SandboxExecutor。

前置:
  - docker build -t cc-harness-runtime:local sandboxes/
  - pip install -e '.[sandbox]'(opensandbox SDK + opensandbox-server)

手动跑:.venv/Scripts/python.exe -m pytest tests/_test_sandbox_integration.py -v
默认不收集(前缀 _,需显式指定文件)。

用 8765 端口(非默认 8000):本测试多次跑会累积 orphan server(进程组跨 pytest
不清理),ensure_server ping 会复用 orphan + 其旧 config → 假阳性。独立端口避开。
production REPL 每 session 一次 init/shutdown,无 orphan 问题。
"""
import importlib.util
import socket
import tempfile
from pathlib import Path

import pytest


def _free_port() -> int:
    """每次跑挑空闲端口,避上次 run 残留 orphan server(ping 复用旧 config 假阳性)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = _free_port()

# 运行时 skip:SDK 未装 → 整文件 skip(不静态 True 跳过,让有环境时能真跑)。
pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("opensandbox") is None,
    reason="opensandbox SDK 未装(pip install -e '.[sandbox]')",
)


@pytest.mark.asyncio
async def test_end_to_end_echo():
    from cc_harness.sandbox import SandboxExecutor
    from cc_harness.config import SandboxConfig
    from cc_harness.sandbox_server import _docker_available, shutdown_owned

    if not _docker_available():
        pytest.skip("Docker 未装/未运行")
    try:
        tmp = Path(tempfile.mkdtemp())
        (tmp / "marker.txt").write_text("host-write", encoding="utf-8")
        # SandboxExecutor._ensure_sandbox 自己 ensure_server(带 allowed=[project_root])。
        # 这里不另调 ensure_server —— 否则无 allowed 的 fork 先起 server,_ensure_sandbox
        # ping 复用它(allowed=[]),create volume 报 HOST_PATH_NOT_ALLOWED。
        ex = SandboxExecutor(SandboxConfig(server_port=PORT), project_root=tmp)
        r = await ex.run({"command": "echo integration-ok"}, cwd=Path("."))
        assert r.is_error is False
        assert "integration-ok" in r.llm_text
        # RO mount 验证:host 写的文件 sandbox 能读
        r2 = await ex.run({"command": "cat /workspace/marker.txt"}, cwd=Path("."))
        assert r2.is_error is False
        assert "host-write" in r2.llm_text
        await ex.kill()
    finally:
        await shutdown_owned()

