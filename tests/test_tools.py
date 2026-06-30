from cc_harness.tools import (
    is_dangerous,
    run_command,
    RUN_COMMAND_SPEC,
)


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
    """Working directory is the `cwd` argument, not the caller's cwd."""
    import asyncio
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "marker.txt").write_text("HERE", encoding="utf-8")
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
