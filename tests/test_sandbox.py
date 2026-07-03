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
async def test_ensure_sandbox_passes_mount(tmp_path, monkeypatch):
    """Sandbox.create 收到项目根 RO mount + /tmp/work workdir。"""
    from cc_harness.sandbox import SandboxExecutor
    from cc_harness.config import SandboxConfig
    # Task-4 forward-fix:commands.run 必须是 AsyncMock,否则 await MagicMock
    # 抛 TypeError → 走 _with_retry(3 次 + sleeps)→ 测试变慢且验证错路径。
    fake_exec = MagicMock(exit_code=0,
                          logs=MagicMock(stdout=[MagicMock(text="")], stderr=[]))
    fake_sandbox = MagicMock()
    fake_sandbox.commands.run = AsyncMock(return_value=fake_exec)
    fake_sandbox.kill = AsyncMock()
    captured = {}

    async def fake_create(*args, **kw):
        captured.update(kw)
        return fake_sandbox

    # 防御:即便走错路径也不要真睡(本测试 happy path 不会触发)。
    monkeypatch.setattr("cc_harness.sandbox.asyncio.sleep", AsyncMock())
    with patch("cc_harness.sandbox.Sandbox") as SDK:
        SDK.create = fake_create
        ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
        await ex.run({"command": "ls"}, cwd=tmp_path)
    mounts = captured.get("mounts") or []
    assert any(str(tmp_path) in str(m) for m in mounts), "缺项目根 mount"
    assert captured.get("workdir") == "/tmp/work", "workdir 未设为 /tmp/work"


@pytest.mark.asyncio
async def test_run_retries_then_succeeds(tmp_path, monkeypatch):
    """create 失败 2 次第 3 次成功 → 不降级,返回结果。"""
    from cc_harness.sandbox import SandboxExecutor
    from cc_harness.config import SandboxConfig
    fake_exec = MagicMock(exit_code=0, logs=MagicMock(stdout=[MagicMock(text="ok")], stderr=[]))
    fake_sandbox = MagicMock()
    fake_sandbox.commands.run = AsyncMock(return_value=fake_exec)
    fake_sandbox.kill = AsyncMock()
    calls = {"n": 0}

    async def flaky_create(*a, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("transient")
        return fake_sandbox

    # 真睡 1+2=3s 会让套件变慢/Flaky;monkeypatch 成 AsyncMock 即可。
    monkeypatch.setattr("cc_harness.sandbox.asyncio.sleep", AsyncMock())
    with patch("cc_harness.sandbox.Sandbox") as SDK:
        SDK.create = flaky_create
        ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
        result = await ex.run({"command": "echo ok"}, cwd=tmp_path)
    assert calls["n"] == 3
    assert "ok" in result.llm_text


@pytest.mark.asyncio
async def test_run_falls_back_after_3_failures(tmp_path, monkeypatch):
    """create 连败 3 次 → run 抛 SandboxUnavailableError(调用方降级 native)。"""
    from cc_harness.sandbox import SandboxExecutor, SandboxUnavailableError
    from cc_harness.config import SandboxConfig
    monkeypatch.setattr("cc_harness.sandbox.asyncio.sleep", AsyncMock())
    with patch("cc_harness.sandbox.Sandbox") as SDK:
        SDK.create = AsyncMock(side_effect=ConnectionError("down"))
        ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
        with pytest.raises(SandboxUnavailableError):
            await ex.run({"command": "echo"}, cwd=tmp_path)
