from cc_harness.schema import validate_native, set_mcp_schemas, validate_mcp


def test_native_run_command_valid():
    ok, msg = validate_native("run_command", {"command": "ls -la"})
    assert ok and msg == ""


def test_native_run_command_empty_rejected():
    ok, msg = validate_native("run_command", {"command": "   "})
    assert not ok
    assert "command" in msg.lower() or "non-empty" in msg.lower()


def test_native_run_command_wrong_type_rejected():
    ok, msg = validate_native("run_command", {"command": 123})
    assert not ok


def test_native_unknown_tool_passes():
    ok, _ = validate_native("something_else", {})
    assert ok


def test_mcp_validates_against_schema():
    set_mcp_schemas({
        "mcp__fs__read_file": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }
    })
    assert validate_mcp("mcp__fs__read_file", {"path": "/tmp"})[0] is True
    ok, msg = validate_mcp("mcp__fs__read_file", {"path": 123})
    assert ok is False  # 类型错


def test_mcp_no_schema_passes():
    set_mcp_schemas({})
    assert validate_mcp("mcp__unknown__x", {"anything": 1})[0] is True
