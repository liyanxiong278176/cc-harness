import pytest
from unittest.mock import AsyncMock, MagicMock

from cc_harness.tools import (
    is_dangerous,
    run_command,
    RUN_COMMAND_SPEC,
)


# Session executor 是模块级单例;每个测试前/后重置以保证隔离
# (否则首个测试 lazy-init 的 NativeExecutor(timeout_s=30, project_root=".")
#  会被后续测试复用,monkeypatch RUN_COMMAND_TIMEOUT_S 失效)。
@pytest.fixture(autouse=True)
def _reset_session_executor():
    from cc_harness import tools
    tools.reset_session_executor()
    yield
    tools.reset_session_executor()


# --- is_dangerous (existing) ---

def test_unsafe_bash_tool_matches_rm_rf():
    assert is_dangerous("mcp__bash__run", {"command": "rm -rf /tmp/x"})

def test_safe_bash_tool_does_not_match_rm_r():
    """MVP: only -rf is flagged; plain -r is fine for daily dev."""
    assert not is_dangerous("mcp__bash__run", {"command": "rm -r /tmp/build"})

def test_safe_bash_tool_does_not_match_ls():
    assert not is_dangerous("mcp__bash__run", {"command": "ls -la"})

def test_write_file_content_not_scanned():
    """Per spec: write_file content is NEVER scanned (false positives)."""
    assert not is_dangerous(
        "mcp__filesystem__write_file",
        {"path": "docs.md", "content": "How to back up before rm -rf ..."},
    )

def test_non_shell_tool_with_command_field_still_flagged():
    """If a non-shell tool happens to have a 'command' field, scan it."""
    assert is_dangerous("mcp__custom__do_thing", {"command": "drop table users"})

def test_drop_database_caught():
    assert is_dangerous("mcp__db__exec", {"command": "drop database prod"})

def test_format_drive_caught():
    assert is_dangerous("mcp__os__run", {"command": "format C:"})

def test_shutdown_caught():
    assert is_dangerous("mcp__os__run", {"command": "shutdown now"})

def test_fork_bomb_caught():
    assert is_dangerous("mcp__os__run", {"command": ":(){ :|:&};:"})


# --- run_command: built-in tool ---

def test_run_command_happy_path(tmp_path):
    """A simple echo-style command returns its stdout."""
    import asyncio
    result = asyncio.run(run_command(
        {"command": "echo hello"},
        cwd=str(tmp_path),
    ))
    assert result.is_error is False
    assert "hello" in result.llm_text
    assert "hello" in result.display_text


def test_run_command_respects_cwd(tmp_path):
    """Working directory is the session executor's project_root.

    会话级 executor 决定 cwd(Task 9 语义变更):per-call cwd 不再生效,
    需 init_session_executor 指定。这里验证 init 的 project_root 被尊重。
    """
    import asyncio
    from cc_harness import tools
    from cc_harness.config import ExecutorConfig
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "marker.txt").write_text("HERE", encoding="utf-8")
    tools.init_session_executor(ExecutorConfig(), str(sub))
    result = asyncio.run(run_command(
        {"command": "type marker.txt"} if _is_windows() else {"command": "cat marker.txt"},
        cwd=str(sub),
    ))
    assert "HERE" in result.llm_text


def test_run_command_nonzero_exit_returns_error():
    import asyncio
    result = asyncio.run(run_command(
        {"command": "exit 7"},
        cwd=".",
    ))
    assert result.is_error is True
    assert "7" in result.llm_text or "exit" in result.llm_text.lower()


def test_run_command_empty_command_returns_error():
    import asyncio
    for empty in ("", "   "):
        result = asyncio.run(run_command({"command": empty}, cwd="."))
        assert result.is_error is True
        assert "non-empty" in result.llm_text or "must be" in result.llm_text


def test_run_command_non_string_command_returns_error():
    import asyncio
    result = asyncio.run(run_command({"command": 123}, cwd="."))
    assert result.is_error is True
    assert "string" in result.llm_text


def test_run_command_timeout(monkeypatch):
    """A command that exceeds the timeout returns a timeout error."""
    import asyncio
    from cc_harness import tools as tools_mod

    # Patch the timeout to something tiny so the test is fast.
    monkeypatch.setattr(tools_mod, "RUN_COMMAND_TIMEOUT_S", 0.5)

    result = asyncio.run(run_command(
        # On Windows, `timeout` is not a builtin. Use Python's sleep instead.
        {"command": "python -c \"import time; time.sleep(5)\"" if _is_windows()
                   else "sleep 5"},
        cwd=".",
    ))
    assert result.is_error is True
    assert "timeout" in result.llm_text.lower()


# --- run_command: spec shape (for OpenAI function-calling) ---

def test_run_command_spec_is_openai_function_format():
    assert RUN_COMMAND_SPEC["type"] == "function"
    fn = RUN_COMMAND_SPEC["function"]
    assert fn["name"] == "run_command"
    assert isinstance(fn["description"], str) and fn["description"]
    params = fn["parameters"]
    assert params["type"] == "object"
    assert "command" in params["properties"]
    assert "command" in params["required"]


# --- confirm_tool (3-way L4 gate) ---

def test_confirm_tool_yes(monkeypatch):
    from cc_harness.tools import confirm_tool
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    assert confirm_tool("run_command", {"command": "ls"}) == "yes"


def test_confirm_tool_always(monkeypatch):
    from cc_harness.tools import confirm_tool
    monkeypatch.setattr("builtins.input", lambda *a, **k: "always")
    assert confirm_tool("run_command", {"command": "ls"}) == "always"


def test_confirm_tool_no_default(monkeypatch):
    from cc_harness.tools import confirm_tool
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")  # Enter = default no
    assert confirm_tool("run_command", {"command": "ls"}) == "no"


def test_confirm_tool_eof_is_no(monkeypatch):
    from cc_harness.tools import confirm_tool
    def _raise(*a, **k):
        raise EOFError
    monkeypatch.setattr("builtins.input", _raise)
    assert confirm_tool("run_command", {"command": "ls"}) == "no"


# --- helpers ---

def _is_windows() -> bool:
    import sys
    return sys.platform == "win32"


# --- session executor singleton (Task 9) ---

@pytest.mark.asyncio
async def test_run_command_uses_session_executor(monkeypatch, tmp_path):
    """run_command 经 get_session_executor().run(),不再内联 NativeExecutor。"""
    from cc_harness import tools
    fake = MagicMock()
    fake.run = AsyncMock(return_value=MagicMock(llm_text="hi", error=False))
    monkeypatch.setattr(tools, "get_session_executor", lambda: fake)
    await tools.run_command({"command": "echo hi"}, cwd=str(tmp_path))
    fake.run.assert_awaited_once()


def test_init_then_get_returns_same(monkeypatch, tmp_path):
    """init_session_executor 后 get 返回同一实例(会话级复用)。"""
    from cc_harness import tools
    from cc_harness.config import ExecutorConfig
    tools.reset_session_executor()
    tools.init_session_executor(ExecutorConfig(), tmp_path)
    a = tools.get_session_executor()
    b = tools.get_session_executor()
    assert a is b


@pytest.mark.asyncio
async def test_run_falls_back_to_native_on_sandbox_unavailable(monkeypatch, tmp_path):
    """sandbox 连败 → run 内部降级 native(用户无感,警告)。"""
    from cc_harness import tools
    from cc_harness.sandbox import SandboxUnavailableError
    sb = MagicMock()
    sb.run = AsyncMock(side_effect=SandboxUnavailableError("down"))
    native = MagicMock()
    native.run = AsyncMock(return_value=MagicMock(llm_text="fallback", error=False))
    monkeypatch.setattr(tools, "get_session_executor", lambda: sb)
    monkeypatch.setattr(tools, "_native_fallback",
                        lambda cwd: native)
    result = await tools.run_command({"command": "echo"}, cwd=str(tmp_path))
    assert "fallback" in result.llm_text


@pytest.mark.asyncio
async def test_shutdown_session_executor_kills_and_clears(monkeypatch):
    """shutdown 调 executor.kill + shutdown_owned,清空单例(fail-soft)。"""
    from cc_harness import tools
    from unittest.mock import AsyncMock, MagicMock
    fake_exec = MagicMock()
    fake_exec.kill = AsyncMock()
    monkeypatch.setattr(tools, "_session_executor", fake_exec)
    from cc_harness import sandbox_server as ss
    monkeypatch.setattr(ss, "shutdown_owned", AsyncMock())
    await tools.shutdown_session_executor()
    fake_exec.kill.assert_awaited()
    assert tools._session_executor is None
