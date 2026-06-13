"""Tests for the 4-phase ReAct renderers in cc_harness.render.

Per the current design:
  - No ANSI color codes
  - 4 phases: 思考 / 行动 / 观察 / 结果, each on its own block
  - 思考 = LLM's full text (no truncation)
  - 行动 = tool name + one arg per line
  - 观察 = tool's actual result (what the LLM sees), indented
  - 结果 = final answer
  - Each block preceded by a blank line for visual separation
  - print_warn uses ⚠, print_error uses ✗, print_info is plain
"""
from io import StringIO
from rich.console import Console as C
from cc_harness.render import (
    print_thought, print_action, print_observation, print_result,
    print_warn, print_error, print_info,
)


def _make_console() -> tuple[C, StringIO]:
    """Console writing to a StringIO. color_system=None → no ANSI."""
    buf = StringIO()
    c = C(file=buf, force_terminal=False, color_system=None, width=120)
    return c, buf


# --- 思考 (Thought) ---

def test_print_thought_emits_thought_label_and_full_text():
    c, buf = _make_console()
    print_thought(c, "我需要找到 JSON 文件。")
    text = buf.getvalue()
    assert "思考:" in text
    assert "我需要找到 JSON 文件。" in text
    assert "\x1b[" not in text


def test_print_thought_preserves_full_text_no_truncation():
    """Per user spec: 思考 must be the COMPLETE LLM text, not truncated.
    Note: Rich wraps long lines at the console width, so we check for the
    presence of the text by stripping line breaks."""
    c, buf = _make_console()
    long_text = "final answer. " * 50  # 700+ chars
    print_thought(c, long_text)
    text = buf.getvalue()
    # Remove whitespace (newlines inserted by Rich's wrapping) and compare
    plain_compact = "".join(text.split())
    expected_compact = "".join(long_text.split())
    assert expected_compact in plain_compact, "long thought text should NOT be truncated"


def test_print_thought_preceded_by_blank_line():
    """Each non-streaming phase is preceded by a blank line."""
    c, buf = _make_console()
    print_thought(c, "first thought")
    text = buf.getvalue()
    # First line should be blank (the leading \\n\\n from _blank)
    lines = text.splitlines()
    assert lines[0] == "", f"first line should be blank, got: {lines[0]!r}"
    assert "思考: first thought" in lines[1]


# --- 行动 (Action) ---

def test_print_action_shows_action_label():
    c, buf = _make_console()
    print_action(c, "mcp__fs__read_file", {"path": "main.py"})
    text = buf.getvalue()
    assert "行动: mcp__fs__read_file" in text
    assert "path" in text
    assert "main.py" in text
    assert "\x1b[" not in text


def test_print_action_compact_args():
    """One arg per line, not multi-line indented JSON."""
    c, buf = _make_console()
    print_action(c, "mcp__fs__list_dir", {"path": "D:/x", "recursive": True})
    text = buf.getvalue()
    assert "path" in text
    assert "D:/x" in text
    assert "recursive" in text
    assert "  path:" in text
    assert "  recursive:" in text


def test_print_action_with_no_args():
    c, buf = _make_console()
    print_action(c, "mcp__fs__list_allowed_directories", {})
    text = buf.getvalue()
    assert "行动: mcp__fs__list_allowed_directories" in text


def test_print_action_preceded_by_blank_line():
    c, buf = _make_console()
    print_action(c, "mcp__x__y", {})
    text = buf.getvalue()
    lines = text.splitlines()
    assert lines[0] == ""


# --- 观察 (Observation) ---

def test_print_observation_shows_observation_label():
    c, buf = _make_console()
    print_observation(c, "Allowed directories: D:\\agent_learning\\cc-harness")
    text = buf.getvalue()
    assert "观察:" in text
    assert "Allowed directories" in text
    assert "\x1b[" not in text


def test_print_observation_indents_multiline_content():
    """Multi-line tool results (e.g. file contents) should be indented under
    the '观察:' label, one line per row."""
    c, buf = _make_console()
    print_observation(c, "line1\nline2\nline3")
    text = buf.getvalue()
    assert "观察:" in text
    assert "  line1" in text
    assert "  line2" in text
    assert "  line3" in text


def test_print_observation_handles_empty_text():
    c, buf = _make_console()
    print_observation(c, "")
    text = buf.getvalue()
    assert "观察:" in text


def test_print_observation_preceded_by_blank_line():
    c, buf = _make_console()
    print_observation(c, "some result")
    text = buf.getvalue()
    lines = text.splitlines()
    assert lines[0] == ""


# --- 结果 (Result) ---

def test_print_result_shows_result_label_and_full_text():
    c, buf = _make_console()
    print_result(c, "the final answer is 42")
    text = buf.getvalue()
    assert "结果:" in text
    assert "the final answer is 42" in text
    assert "\x1b[" not in text


def test_print_result_full_text_no_truncation():
    c, buf = _make_console()
    long_text = "final answer. " * 50
    print_result(c, long_text)
    text = buf.getvalue()
    # Strip whitespace to handle Rich's line-wrapping
    plain_compact = "".join(text.split())
    expected_compact = "".join(long_text.split())
    assert expected_compact in plain_compact


def test_print_result_preceded_by_blank_line():
    c, buf = _make_console()
    print_result(c, "done")
    text = buf.getvalue()
    lines = text.splitlines()
    assert lines[0] == ""


# --- 4-phase layout: blank lines between blocks ---

def test_four_phase_layout_has_blank_between_each_phase():
    """The full ReAct cycle: 思考 → 行动 → 观察 → (next) 思考
    Each block separated by a blank line."""
    c, buf = _make_console()
    print_thought(c, "thought 1")
    print_action(c, "tool1", {"arg": "val"})
    print_observation(c, "result 1")
    print_thought(c, "thought 2")
    print_action(c, "tool2", {})
    print_observation(c, "result 2")
    text = buf.getvalue()
    lines = text.splitlines()
    # All four phase labels should appear in order
    assert "思考: thought 1" in lines
    assert "行动: tool1" in lines
    assert "观察:" in lines
    assert "思考: thought 2" in lines
    assert "行动: tool2" in lines
    # Verify the order
    i_t1 = next(i for i, ln in enumerate(lines) if "思考: thought 1" in ln)
    i_a1 = next(i for i, ln in enumerate(lines) if "行动: tool1" in ln)
    i_o1 = next(i for i, ln in enumerate(lines) if ln.strip() == "观察:")
    i_t2 = next(i for i, ln in enumerate(lines) if "思考: thought 2" in ln)
    assert i_t1 < i_a1 < i_o1 < i_t2, f"4-phase order broken: {i_t1} < {i_a1} < {i_o1} < {i_t2}"


# --- warn / error / info ---

def test_print_warn_uses_warning_glyph():
    c, buf = _make_console()
    print_warn(c, "careful")
    text = buf.getvalue()
    assert "⚠" in text
    assert "careful" in text
    assert "\x1b[" not in text


def test_print_error_uses_error_glyph():
    c, buf = _make_console()
    print_error(c, "boom")
    text = buf.getvalue()
    assert "✗" in text
    assert "boom" in text
    assert "\x1b[" not in text


def test_print_info_plain():
    c, buf = _make_console()
    print_info(c, "ready")
    text = buf.getvalue()
    assert "ready" in text
    assert "\x1b[" not in text


# --- compaction summary (Task 9) ---

def test_print_compaction_summary_no_op_on_none_tier(capfd):
    from cc_harness.render import print_compaction_summary
    from cc_harness.context import CompactionTier, CompactionStats
    from rich.console import Console
    console = Console(file=None, force_terminal=False)
    stats = CompactionStats(tier=CompactionTier.NONE, before_tokens=0, after_tokens=0, ratio_before=0.0, ratio_after=0.0)
    print_compaction_summary(console, "本轮", stats)
    out = capfd.readouterr().out
    assert "上下文压缩" not in out


def test_print_compaction_summary_prints_tier_and_ratio(capfd):
    from cc_harness.render import print_compaction_summary
    from cc_harness.context import CompactionTier, CompactionStats
    from rich.console import Console
    console = Console(file=None, force_terminal=False)
    stats = CompactionStats(
        tier=CompactionTier.SNIP, before_tokens=1000, after_tokens=500,
        ratio_before=0.7, ratio_after=0.35, messages_snip=3,
    )
    print_compaction_summary(console, "本轮", stats)
    out = capfd.readouterr().out
    assert "上下文压缩" in out
    assert "tier 1" in out
    assert "70%" in out
    assert "35%" in out
    assert "snip 3" in out


def test_print_compaction_summary_prints_error_line(capfd):
    from cc_harness.render import print_compaction_summary
    from cc_harness.context import CompactionTier, CompactionStats
    from rich.console import Console
    console = Console(file=None, force_terminal=False)
    stats = CompactionStats(
        tier=CompactionTier.SUMMARIZE, before_tokens=2000, after_tokens=2000,
        ratio_before=0.95, ratio_after=0.95, summarized=False, error="LLM down",
    )
    print_compaction_summary(console, "本轮", stats)
    out = capfd.readouterr().out
    assert "压缩失败" in out
    assert "LLM down" in out
