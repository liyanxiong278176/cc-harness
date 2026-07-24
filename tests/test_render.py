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
import pathlib
from unittest.mock import MagicMock
from rich.console import Console as C
from cc_harness.render import (
    print_thought, print_action, print_observation, print_result,
    print_warn, print_error, print_info,
    print_token_summary, print_compaction_summary,
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


# --- print_compaction_summary (Plan3 Task7) ---

def test_print_compaction_summary_none_stats_no_output():
    """stats=None → 完全不打印(无压缩发生)。"""
    c, buf = _make_console()
    print_compaction_summary(c, "本轮", None)
    assert buf.getvalue() == ""


def test_print_compaction_summary_none_tier_no_output():
    """tier==NONE → 不打印(未触发压缩)。"""
    from cc_harness.context import CompactionTier, CompactionStats
    c, buf = _make_console()
    stats = CompactionStats(
        tier=CompactionTier.NONE, before_tokens=100, after_tokens=100,
        ratio_before=0.1, ratio_after=0.1,
    )
    print_compaction_summary(c, "本轮", stats)
    assert buf.getvalue() == ""


def test_print_compaction_summary_snip_prints_line():
    """tier=SNIP → 单行含 label + tier + ratio + snip 计数。"""
    from cc_harness.context import CompactionTier, CompactionStats
    c, buf = _make_console()
    stats = CompactionStats(
        tier=CompactionTier.SNIP, before_tokens=1000, after_tokens=800,
        ratio_before=0.9, ratio_after=0.7, messages_snip=3,
    )
    print_compaction_summary(c, "本轮", stats)
    text = buf.getvalue()
    assert "上下文压缩" in text
    assert "本轮" in text
    assert "90%" in text               # ratio_before as pct
    assert "70%" in text               # ratio_after as pct
    assert "snip" in text.lower()
    assert "3" in text                  # messages_snip count


def test_print_compaction_summary_summarize_shows_summary_index():
    """tier=SUMMARIZE + summarized → 含 [summary 插入 #idx]。"""
    from cc_harness.context import CompactionTier, CompactionStats
    c, buf = _make_console()
    stats = CompactionStats(
        tier=CompactionTier.SUMMARIZE, before_tokens=10000, after_tokens=5000,
        ratio_before=0.99, ratio_after=0.5, summarized=True, summary_index=1,
    )
    print_compaction_summary(c, "本轮", stats)
    text = buf.getvalue()
    assert "summary" in text.lower()
    assert "#1" in text                  # summary_index


def test_print_compaction_summary_error_appends_warning():
    """error 非空 → 追加 ⚠ 行。"""
    from cc_harness.context import CompactionTier, CompactionStats
    c, buf = _make_console()
    stats = CompactionStats(
        tier=CompactionTier.SUMMARIZE, before_tokens=1000, after_tokens=1000,
        ratio_before=0.95, ratio_after=0.95, error="LLM timeout",
    )
    print_compaction_summary(c, "本轮", stats)
    text = buf.getvalue()
    assert "⚠" in text
    assert "LLM timeout" in text


# --- print_token_summary summary bucket (Plan3 Task7) ---

def test_print_token_summary_summary_bucket_shown_when_positive():
    """summary>0 → '摘要 N' 出现在 LLM 输出 之后。"""
    from cc_harness.tokens import TurnTokenStats
    c, buf = _make_console()
    stats = TurnTokenStats(
        user_input=10, tool_calls=20, llm_output=30, system_prompt=40,
        summary=50, tool_definitions=5, api_total_tokens=0,
    )
    print_token_summary(c, "本轮", stats)
    text = buf.getvalue()
    assert "摘要" in text
    assert "50" in text
    # summary bucket appears after LLM 输出
    assert text.index("LLM 输出") < text.index("摘要")


def test_print_token_summary_no_summary_bucket_when_zero():
    """summary==0 → 不出现 '摘要'(backward-compat:旧 5 桶格式不变)。"""
    from cc_harness.tokens import TurnTokenStats
    c, buf = _make_console()
    stats = TurnTokenStats(
        user_input=10, tool_calls=20, llm_output=30, system_prompt=40,
        summary=0, tool_definitions=5, api_total_tokens=0,
    )
    print_token_summary(c, "本轮", stats)
    text = buf.getvalue()
    assert "摘要" not in text


# --- print_cross_session_summary (E3 T5) ---

def test_print_cross_session_summary_no_diff():
    """E3 D4: 无 tool 变更 + 无 in-progress subagent → 简洁摘要。"""
    from cc_harness.render import print_cross_session_summary
    from cc_harness.memory.checkpoint import CheckpointRecord

    console = MagicMock()
    candidate = CheckpointRecord(
        session_id="old1", project_root=pathlib.Path("/tmp"),
        mode="coding", turn_counter=3,
        started_at="2026-07-24T09:00:00",
        ended_at="2026-07-24T09:05:00",
        cross_session_mode="last_only", extra={},
    )
    print_cross_session_summary(console, candidate, tool_diff=[], in_progress_subagents=[])
    call_str = " ".join(str(c) for c in console.print.call_args_list)
    assert "续接上次 session" in call_str
    assert "coding" in call_str
    assert "工具变更" not in call_str


def test_print_cross_session_summary_with_diff_and_subagents():
    """E3 D6/D7: 有 tool 变更 + 有 cancelled subagent → 完整摘要。"""
    from cc_harness.render import print_cross_session_summary
    from cc_harness.memory.checkpoint import CheckpointRecord

    console = MagicMock()
    candidate = CheckpointRecord(
        session_id="old2", project_root=pathlib.Path("/p"),
        mode="coding", turn_counter=5,
        started_at="2026-07-24T10:00:00",
        ended_at="2026-07-24T10:10:00",
        cross_session_mode="last_only", extra={},
    )
    print_cross_session_summary(
        console, candidate,
        tool_diff=["+newtool", "-oldtool"],
        in_progress_subagents=["sa1", "sa2"],
    )
    call_str = " ".join(str(c) for c in console.print.call_args_list)
    assert "工具变更" in call_str
    assert "cancelled" in call_str or "取消" in call_str
    assert "2" in call_str  # 2 个 subagent
