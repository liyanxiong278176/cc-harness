import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_run_returns_stdout_as_toolresult(tmp_path):
    """commands.run 返回的 stdout → ToolResult.success。"""
    from cc_harness.sandbox import SandboxExecutor
    from cc_harness.config import SandboxConfig

    fake_exec = MagicMock()
    fake_exec.exit_code = 0
    fake_exec.logs.stdout = [MagicMock(text="hello\n")]
    fake_sandbox = MagicMock()
    fake_sandbox.commands.run = AsyncMock(return_value=fake_exec)
    fake_sandbox.kill = AsyncMock()

    with patch("cc_harness.sandbox.Sandbox") as SDK:
        SDK.create = AsyncMock(return_value=fake_sandbox)
        ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
        result = await ex.run({"command": "echo hello"}, cwd=tmp_path)
    assert "hello" in result.llm_text
    assert result.is_error is False


@pytest.mark.asyncio
async def test_run_nonzero_exit_returns_error_toolresult(tmp_path):
    from cc_harness.sandbox import SandboxExecutor
    from cc_harness.config import SandboxConfig
    fake_exec = MagicMock()
    fake_exec.exit_code = 2
    fake_exec.logs.stdout = [MagicMock(text="")]
    fake_exec.logs.stderr = [MagicMock(text="boom")]
    fake_sandbox = MagicMock()
    fake_sandbox.commands.run = AsyncMock(return_value=fake_exec)
    fake_sandbox.kill = AsyncMock()
    with patch("cc_harness.sandbox.Sandbox") as SDK:
        SDK.create = AsyncMock(return_value=fake_sandbox)
        ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
        result = await ex.run({"command": "bad-cmd"}, cwd=tmp_path)
    assert result.is_error is True
    assert "boom" in result.llm_text


@pytest.mark.asyncio
async def test_ensure_sandbox_passes_mount(tmp_path):
    """Sandbox.create 收到项目根 RO mount + /tmp/work workdir。"""
    from cc_harness.sandbox import SandboxExecutor
    from cc_harness.config import SandboxConfig
    fake_sandbox = MagicMock()
    fake_sandbox.kill = AsyncMock()
    captured = {}

    async def fake_create(*args, **kw):
        captured.update(kw)
        return fake_sandbox

    with patch("cc_harness.sandbox.Sandbox") as SDK:
        SDK.create = fake_create
        ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
        await ex.run({"command": "ls"}, cwd=tmp_path)
    mounts = captured.get("mounts") or []
    assert any(str(tmp_path) in str(m) for m in mounts), "缺项目根 mount"
    assert captured.get("workdir") == "/tmp/work", "workdir 未设为 /tmp/work"
