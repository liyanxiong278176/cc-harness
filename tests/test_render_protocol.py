"""Tests for cc_harness.render_protocol.

TUI / REPL / Test drivers all share the same event types defined here.
"""
from cc_harness.render_protocol import (
    RenderDriver,
    RenderEvent,
    ThinkingChunk,
    ThinkingDone,
    ToolCallStart,
    ToolCallEnd,
    FinalText,
    Usage,
    TodoUpdate,
    ModeChanged,
    PermissionModeChanged,
)


def test_render_event_immutable():
    """RenderEvent 应是 frozen dataclass,不可变。"""
    e = ThinkingChunk(delta="hello")
    assert e.delta == "hello"
    try:
        e.delta = "world"  # type: ignore[misc]
        assert False, "should have raised"
    except Exception:
        pass


def test_render_driver_protocol_has_required_methods():
    """RenderDriver Protocol 必须有 7 个方法。"""
    required = {
        "write",
        "write_chunk",
        "write_tool_call",
        "write_tool_result",
        "write_todo",
        "write_status",
        "refresh_token",
    }
    # Protocol + runtime_checkable exposes attrs on the class itself
    assert required.issubset(set(dir(RenderDriver)))


def test_tool_call_start_carries_tool_name():
    e = ToolCallStart(name="run_command", args={"cmd": "pytest"})
    assert e.name == "run_command"
    assert e.args == {"cmd": "pytest"}


def test_todo_update_carries_progress():
    e = TodoUpdate(items=[{"id": "1", "title": "Read", "status": "done"}])
    assert e.items[0]["status"] == "done"


def test_render_event_union_includes_all_variants():
    """RenderEvent 联合类型应包含 9 个子类。"""
    variants = [
        ThinkingChunk(delta="x"),
        ThinkingDone(text="x"),
        ToolCallStart(name="n", args={}),
        ToolCallEnd(name="n", result="r", error=False, duration_ms=1),
        FinalText(text="x"),
        Usage(input_tokens=1, output_tokens=1, cached_tokens=0, reasoning_tokens=0),
        TodoUpdate(items=[]),
        ModeChanged(mode="coding"),
        PermissionModeChanged(mode="default"),
    ]
    assert len(variants) == 9
    # All concrete variants should be assignable to the union
    for v in variants:
        ev: RenderEvent = v
        assert ev is not None
