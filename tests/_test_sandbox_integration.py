"""集成测试:真起 Docker + opensandbox-server,端到端验证 SandboxExecutor。

前置:
  - docker build -t cc-harness-runtime:local sandboxes/
  - opensandbox-server 在 127.0.0.1:8000(ensure_server 会自动起 owned server;
    或手动起:opensandbox-server --config <toml>,记得 OPENSANDBOX_INSECURE_SERVER=YES)
  - pip install -e '.[sandbox]'(opensandbox SDK)

手动跑:.venv/Scripts/python.exe -m pytest tests/_test_sandbox_integration.py -v
默认不收集(前缀 _,需显式指定文件)。
"""
import importlib.util
import tempfile
from pathlib import Path

import pytest

# 运行时 skip:SDK 未装 → 整文件 skip(不静态 True 跳过,让有环境时能真跑)。
pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("opensandbox") is None,
    reason="opensandbox SDK 未装(pip install -e '.[sandbox]')",
)


@pytest.mark.asyncio
async def test_end_to_end_echo():
    from cc_harness.sandbox import SandboxExecutor
    from cc_harness.config import SandboxConfig
    from cc_harness.sandbox_server import ensure_server, shutdown_owned

    state = await ensure_server(host="127.0.0.1", port=8000)
    if state is None:
        pytest.skip("opensandbox-server 起不来(Docker 未装/未运行)")
    try:
        ex = SandboxExecutor(SandboxConfig(), project_root=Path(tempfile.mkdtemp()))
        r = await ex.run({"command": "echo integration-ok"}, cwd=Path("."))
        assert r.is_error is False
        assert "integration-ok" in r.llm_text
        await ex.kill()
    finally:
        await shutdown_owned()
