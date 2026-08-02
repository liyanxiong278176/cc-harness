"""Tests for TestDriver — the recording RenderDriver used in tests."""
from cc_harness.render_test_driver import TestDriver


def test_records_writes():
    d = TestDriver()
    d.write("hello")
    d.write("world")
    assert d.events == ["hello", "world"]


def test_records_chunks_as_tokens():
    d = TestDriver()
    d.write_chunk("a")
    d.write_chunk("b")
    assert d.tokens == ["a", "b"]


def test_records_tool_calls():
    d = TestDriver()
    d.write_tool_call("run_command", {"cmd": "pytest"})
    assert d.tool_calls == [("run_command", {"cmd": "pytest"})]


def test_flush_returns_aggregated_stream():
    d = TestDriver()
    d.write_chunk("a")
    d.write_chunk("b")
    d.write_chunk("c")
    assert d.flush_stream() == "abc"


def test_reset_clears_history():
    d = TestDriver()
    d.write("x")
    d.reset()
    assert d.events == []
    assert d.tokens == []
