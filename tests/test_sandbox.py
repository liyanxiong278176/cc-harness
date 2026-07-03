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
        with pytest.raises(SandboxUnavailableError) as excinfo:
            await ex.run({"command": "echo"}, cwd=tmp_path)
    assert isinstance(excinfo.value.__cause__, ConnectionError)


@pytest.mark.asyncio
async def test_run_retries_commands_run_then_succeeds(tmp_path, monkeypatch):
    """commands.run 通信错也重试(第二个 retry 调用点)。"""
    monkeypatch.setattr("cc_harness.sandbox.asyncio.sleep", AsyncMock())
    from cc_harness.sandbox import SandboxExecutor
    from cc_harness.config import SandboxConfig
    fake_exec = MagicMock(exit_code=0, logs=MagicMock(stdout=[MagicMock(text="ok2")], stderr=[]))
    fake_sandbox = MagicMock()
    fake_sandbox.kill = AsyncMock()
    calls = {"n": 0}
    async def flaky_run(cmd):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("transient run")
        return fake_exec
    fake_sandbox.commands.run = flaky_run
    with patch("cc_harness.sandbox.Sandbox") as SDK:
        SDK.create = AsyncMock(return_value=fake_sandbox)
        ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
        result = await ex.run({"command": "echo ok2"}, cwd=tmp_path)
    assert calls["n"] == 3
    assert "ok2" in result.llm_text


@pytest.mark.asyncio
async def test_env_stripped_no_secrets(tmp_path):
    """沙箱 env 不含 KEY/TOKEN/SECRET(Vault 未接时 strip_secrets 兜底)。"""
    import os
    from cc_harness.sandbox import SandboxExecutor
    from cc_harness.config import SandboxConfig
    captured = {}
    async def fake_create(*a, **kw):
        captured.update(kw)
        return MagicMock(kill=AsyncMock(),
                         commands=MagicMock(run=AsyncMock(return_value=MagicMock(
                             exit_code=0, logs=MagicMock(stdout=[MagicMock(text="")], stderr=[])))))
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-leak", "PATH": "/x"}):
        with patch("cc_harness.sandbox.Sandbox") as SDK:
            SDK.create = fake_create
            ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
            await ex.run({"command": "env"}, cwd=tmp_path)
    env = captured.get("env", {})
    assert "OPENAI_API_KEY" not in env, "密钥泄露进沙箱 env"
    assert "PATH" in env


@pytest.mark.asyncio
async def test_kill_destroys_sandbox(tmp_path):
    from cc_harness.sandbox import SandboxExecutor
    from cc_harness.config import SandboxConfig
    fake_exec = MagicMock(exit_code=0,
                          logs=MagicMock(stdout=[MagicMock(text="x")], stderr=[]))
    fake_sandbox = MagicMock(kill=AsyncMock())
    fake_sandbox.commands.run = AsyncMock(return_value=fake_exec)
    with patch("cc_harness.sandbox.Sandbox") as SDK:
        SDK.create = AsyncMock(return_value=fake_sandbox)
        ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
        await ex.run({"command": "x"}, cwd=tmp_path)
        await ex.kill()
    fake_sandbox.kill.assert_awaited()
    assert ex._sandbox is None


@pytest.mark.asyncio
async def test_fallback_audited(tmp_path, monkeypatch):
    """降级事件落 logs/sandbox.jsonl。"""
    from cc_harness.sandbox import SandboxExecutor, SandboxUnavailableError
    from cc_harness.config import SandboxConfig
    logged = []
    monkeypatch.setattr("cc_harness.sandbox._audit_fallback",
                        lambda **kw: logged.append(kw))
    # create 连败 3 次 → _with_retry 真 sleep 1s+2s 会让测试变慢/Flaky。
    monkeypatch.setattr("cc_harness.sandbox.asyncio.sleep", AsyncMock())
    with patch("cc_harness.sandbox.Sandbox") as SDK:
        SDK.create = AsyncMock(side_effect=ConnectionError("down"))
        ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
        with pytest.raises(SandboxUnavailableError):
            await ex.run({"command": "x"}, cwd=tmp_path)
    assert logged and logged[0]["retries"] == 3
