# tests/test_render.py
import re
from cc_harness.render import (
    print_thought, print_tool_call, print_tool_result, print_final,
    print_warn, print_error, print_info,
)

ANSI_BLUE = re.compile(r"\x1b\[.*?34m")
ANSI_YELLOW = re.compile(r"\x1b\[.*?33m")
ANSI_GREEN = re.compile(r"\x1b\[.*?32m")
ANSI_RED = re.compile(r"\x1b\[.*?31m")
ANSI_WHITE = re.compile(r"\x1b\[.*?37m")
ANSI_CYAN = re.compile(r"\x1b\[.*?36m")

def test_print_thought_blue(console):
    console.print = lambda *a, **kw: print_thought(console, "thinking")
    # Use Console.capture path
    from io import StringIO
    from rich.console import Console as C
    buf = StringIO()
    c = C(file=buf, force_terminal=True, color_system="truecolor", width=120)
    print_thought(c, "hello thought")
    text = buf.getvalue()
    assert ANSI_BLUE.search(text), f"expected blue ANSI in: {text!r}"
    assert "hello thought" in text

def test_print_tool_call_yellow(console):
    from io import StringIO
    from rich.console import Console as C
    buf = StringIO()
    c = C(file=buf, force_terminal=True, color_system="truecolor", width=120)
    print_tool_call(c, "mcp__fs__read_file", {"path": "main.py"})
    text = buf.getvalue()
    assert ANSI_YELLOW.search(text)
    assert "mcp__fs__read_file" in text
    assert "main.py" in text

def test_print_tool_result_success_green():
    from io import StringIO
    from rich.console import Console as C
    buf = StringIO()
    c = C(file=buf, force_terminal=True, color_system="truecolor", width=120)
    print_tool_result(c, "file contents here", is_error=False)
    text = buf.getvalue()
    assert ANSI_GREEN.search(text)
    assert "file contents here" in text

def test_print_tool_result_error_red():
    from io import StringIO
    from rich.console import Console as C
    buf = StringIO()
    c = C(file=buf, force_terminal=True, color_system="truecolor", width=120)
    print_tool_result(c, "ENOENT", is_error=True)
    text = buf.getvalue()
    assert ANSI_RED.search(text)

def test_print_final_white():
    from io import StringIO
    from rich.console import Console as C
    buf = StringIO()
    c = C(file=buf, force_terminal=True, color_system="truecolor", width=120)
    print_final(c, "the answer is 42")
    text = buf.getvalue()
    assert ANSI_WHITE.search(text)
    assert "the answer is 42" in text

def test_print_warn_yellow():
    from io import StringIO
    from rich.console import Console as C
    buf = StringIO()
    c = C(file=buf, force_terminal=True, color_system="truecolor", width=120)
    print_warn(c, "careful")
    text = buf.getvalue()
    assert ANSI_YELLOW.search(text)

def test_print_error_red():
    from io import StringIO
    from rich.console import Console as C
    buf = StringIO()
    c = C(file=buf, force_terminal=True, color_system="truecolor", width=120)
    print_error(c, "boom")
    text = buf.getvalue()
    assert ANSI_RED.search(text)

def test_print_info_cyan():
    from io import StringIO
    from rich.console import Console as C
    buf = StringIO()
    c = C(file=buf, force_terminal=True, color_system="truecolor", width=120)
    print_info(c, "info")
    text = buf.getvalue()
    assert ANSI_CYAN.search(text)
