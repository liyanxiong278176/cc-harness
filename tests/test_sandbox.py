import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture(autouse=True)
def _mock_ensure_server(monkeypatch):
    """Gap 1:_ensure_sandbox 现调 ensure_server;默认 mock 成 ServerState(owned=False)
    让现有测试不依赖真 Docker(_docker_available 在 CI/本机通常 False → ensure_server 返 None → 降级)。
    个别测试要验 ensure_server 行为时本地 monkeypatch.setattr 覆盖(后 applied 者生效)。"""
    from cc_harness.sandbox_server import ServerState

    async def _fake_ensure(port, host="localhost", **kw):
        return ServerState(owned=False)

    # patch 懒 import 的目标:_ensure_sandbox 内 `from cc_harness.sandbox_server import ensure_server`
    # 在 CALL 时执行,读取的是 sandbox_server 模块的当前 ensure_server 属性 → monkeypatch 模块属性即生效。
    import cc_harness.sandbox_server as ss
    monkeypatch.setattr(ss, "ensure_server", _fake_ensure)


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
    """Sandbox.create 收到项目根 RO volume mount(volumes=,真 SDK 签名)。"""
    from cc_harness.sandbox import SandboxExecutor, _HAS_SANDBOX_SDK
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
    # Gap 2:真 SDK 用 volumes=[Volume(host=Host(path=...))](非 mounts=/Mount)。
    # 注:真 Volume 是 pydantic BaseModel,其 __str__ 会把 Windows 路径分隔符 \ 转义成 \\,
    # 故 `str(tmp_path) in str(v)` 在 Windows 假阴;改断结构化字段 v.host.path(原值未转义)。
    # 真 SDK:v.host 是 Host 对象、v.host.path 是原字符串;CI stub 同理(属性同名)。
    volumes = captured.get("volumes") or []
    assert volumes, "缺 volumes= 参数"
    assert any(getattr(v, "host", None) and str(tmp_path) in str(v.host.path)
               for v in volumes), "缺项目根 volume mount"
    # workdir 断言已删(真 SDK 无 workdir= 参数)。
    # connection_config 仅在 SDK 装好时非 None(CI 无 extra 时 ConnectionConfig=None,
    # 但 mock create 仍接受 None);不写死成必传以避免 CI 假红。
    if _HAS_SANDBOX_SDK:
        assert captured.get("connection_config") is not None, "SDK 装好时 connection_config 必传"


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
async def test_no_host_env_injected(tmp_path):
    """host(Windows)env 不注入沙箱:PATH/SYSTEMROOT 进 Linux 容器会破坏它。
    沙箱用容器默认 env;凭证后续走 Credential Vault(Task 12),非 host env。
    故 Sandbox.create 调用不应带 env= kwarg(host env 天然不进,无密钥泄露路径)。"""
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
    assert "env" not in captured, "host env 不应注入沙箱(Windows env 破坏 Linux 容器)"


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


@pytest.mark.asyncio
async def test_kill_clears_sandbox_even_if_kill_raises(tmp_path):
    """kill() 抛异常也清空 _sandbox(下次 run 重建)。"""
    from cc_harness.sandbox import SandboxExecutor
    from cc_harness.config import SandboxConfig
    fake_exec = MagicMock(exit_code=0, logs=MagicMock(stdout=[MagicMock(text="")], stderr=[]))
    fake_sandbox = MagicMock()
    fake_sandbox.commands.run = AsyncMock(return_value=fake_exec)
    fake_sandbox.kill = AsyncMock(side_effect=RuntimeError("kill boom"))
    with patch("cc_harness.sandbox.Sandbox") as SDK:
        SDK.create = AsyncMock(return_value=fake_sandbox)
        ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
        await ex.run({"command": "x"}, cwd=tmp_path)
        await ex.kill()   # 不抛(kill 异常被吞)
    assert ex._sandbox is None


def test_audit_fallback_writes_jsonl(tmp_path):
    """_audit_fallback 真写入 logs/sandbox.jsonl(不 monkeypatch)。"""
    import json
    from cc_harness.sandbox import _audit_fallback
    _audit_fallback(project_root=tmp_path, reason="conn down", retries=3)
    log = (tmp_path / "logs" / "sandbox.jsonl").read_text(encoding="utf-8").strip()
    rec = json.loads(log)
    assert rec["action"] == "fallback_after_retry"
    assert rec["reason"] == "conn down"
    assert rec["retries"] == 3
    assert "ts" in rec


# ---------------------------------------------------------------------------
# Gap 1 接线测试:_ensure_sandbox 必须先调 ensure_server,再 Sandbox.create。
# autouse _mock_ensure_server 默认返 ServerState(owned=False);下面两测各自覆盖。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_sandbox_calls_ensure_server(tmp_path, monkeypatch):
    """_ensure_sandbox 先调 ensure_server,且传 SandboxConfig.server_port(默认 8000)。"""
    from cc_harness import sandbox_server as ss
    from cc_harness.sandbox_server import ServerState

    called = {"port": None}

    async def spy(port, host="localhost", **kw):
        called["port"] = port
        return ServerState(owned=False)

    # 覆盖 autouse fixture 的 _fake_ensure(后 setattr 者生效)。
    monkeypatch.setattr(ss, "ensure_server", spy)

    from cc_harness.sandbox import SandboxExecutor
    from cc_harness.config import SandboxConfig

    fake_sandbox = MagicMock(
        kill=AsyncMock(),
        commands=MagicMock(run=AsyncMock(return_value=MagicMock(
            exit_code=0, logs=MagicMock(stdout=[MagicMock(text="ok")], stderr=[])))),
    )
    with patch("cc_harness.sandbox.Sandbox") as SDK:
        SDK.create = AsyncMock(return_value=fake_sandbox)
        ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
        await ex.run({"command": "echo ok"}, cwd=tmp_path)
    assert called["port"] == 8000   # SandboxConfig.server_port 默认


@pytest.mark.asyncio
async def test_ensure_server_unavailable_raises(tmp_path, monkeypatch):
    """ensure_server 返 None(Docker 没装/server 起不来)→ SandboxUnavailableError;
    且 Sandbox.create 不该被调(ensure_server 先返 None 短路)。"""
    from cc_harness import sandbox_server as ss

    async def no_server(port, host="localhost", **kw):
        return None

    monkeypatch.setattr(ss, "ensure_server", no_server)

    from cc_harness.sandbox import SandboxExecutor, SandboxUnavailableError
    from cc_harness.config import SandboxConfig

    with patch("cc_harness.sandbox.Sandbox") as SDK:
        SDK.create = AsyncMock()   # 不该被调(ensure_server 先返 None)
        ex = SandboxExecutor(SandboxConfig(), project_root=tmp_path)
        with pytest.raises(SandboxUnavailableError):
            await ex.run({"command": "echo"}, cwd=tmp_path)
        SDK.create.assert_not_called()   # ensure_server None 时不该 create
