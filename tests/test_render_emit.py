"""Tests for cc_harness.render.emit() dispatcher.

The emit() function is the public API that turns a RenderEvent into the
corresponding driver method call. It must dispatch on the concrete event
type and forward all relevant fields, including ToolCallEnd.duration_ms.
"""
from cc_harness.render import emit
from cc_harness.render_test_driver import TestDriver
from cc_harness.render_protocol import (
    ThinkingChunk,
    ToolCallStart,
    ToolCallEnd,
    FinalText,
    TodoUpdate,
    Usage,
)


def test_emit_thinking_chunk_appends():
    d = TestDriver()
    emit(ThinkingChunk(delta="hello"), driver=d)
    assert d.tokens == ["hello"]


def test_emit_tool_call_start_records():
    d = TestDriver()
    emit(ToolCallStart(name="run_command", args={"cmd": "ls"}), driver=d)
    assert d.tool_calls == [("run_command", {"cmd": "ls"})]


def test_emit_tool_call_end_records():
    d = TestDriver()
    emit(ToolCallEnd(name="ls", result="file1\nfile2", error=False, duration_ms=120), driver=d)
    assert d.tool_results == [("file1\nfile2", False, 120)]


def test_emit_final_text_writes():
    d = TestDriver()
    emit(FinalText(text="summary"), driver=d)
    assert d.events == ["summary"]


def test_emit_todo_update_writes():
    d = TestDriver()
    emit(TodoUpdate(items=[{"id": "1", "title": "Read", "status": "done"}]), driver=d)
    assert d.todos == [[{"id": "1", "title": "Read", "status": "done"}]]


def test_emit_usage_refreshes_token():
    d = TestDriver()
    emit(Usage(input_tokens=100, output_tokens=50, cached_tokens=0, reasoning_tokens=10), driver=d)
    assert len(d.token_stats) == 1
