from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from cc_harness.terminal.renderer import TerminalRenderer


class FakeLLM:
    model = "test-model"


class FakeRuntime:
    llm = FakeLLM()
    cwd = Path("C:/work")
    additional_dirs = ()


@pytest.mark.asyncio
async def test_renderer_streams_once_and_keeps_tool_summary_compact():
    output = StringIO()
    renderer = TerminalRenderer(Console(file=output, force_terminal=False, width=100))
    renderer.startup(FakeRuntime(), "1.0")
    await renderer.event({"type": "content_delta", "text": "hello"})
    await renderer.event({"type": "result", "text": "hello"})
    await renderer.event({"type": "action", "name": "read", "args": {"path": "x"}})
    await renderer.event({"type": "observation", "text": "ok", "is_error": False})
    text = output.getvalue()
    assert "cc-harness" in text
    assert text.count("hello") == 1
    assert "read" in text
    assert "ok" in text
