"""集成测试:真起 Docker + opensandbox-server,端到端验证 SandboxExecutor。
需:docker build -t cc-harness-runtime:local sandboxes/ + pip install -e '.[sandbox]'
+ opensandbox-server 运行在 :8000。
手动跑:pytest tests/_test_sandbox_integration.py -v
默认不收集(前缀 _)。

⚠️ TASK 12 SDK 差异(WebSearch 2026-07-03 发现):真 OpenSandbox SDK 的
Sandbox.create 签名是 (image, connection_config=, resource=, env=,
network_policy=, credential_proxy=, ...),不支持当前 sandbox.py 用的
mounts=/workdir= kwargs,且缺必需的 connection_config=。在 sandbox.py 按真 SDK
API 调整前(加 ConnectionConfig、文件共享改 sandbox.files.write_files 或 server
PVC),本集成测试的 SandboxExecutor.run 会 TypeError。先跑通需先做 SDK 锁定改造。
"""
import pytest
from pathlib import Path
import tempfile

pytestmark = [
    pytest.mark.skipif(
        True,  # 静态 skip:集成测试默认不跑(需手动 --run-integration + Docker + SDK)
        reason="integration test needs Docker + opensandbox SDK + server; run manually"
    ),
]


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
