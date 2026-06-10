from cc_harness.tools import is_dangerous

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
