from cc_harness.terminal.transcript import TranscriptState
from cc_harness.terminal.transcript_render import TranscriptRichRenderer, sanitize_terminal_text


def test_render_matches_committed_prompt_thought_tool_answer_and_completion():
    state = TranscriptState("render")
    state.start_turn("你好", ts=0.0)
    state.apply({"type": "action", "name": "Read", "args": {"path": "a.py"}, "ts": 2.0})
    state.apply({"type": "observation", "text": "Read 12 lines", "ts": 2.1})
    state.apply({"type": "result", "text": "完成。", "ts": 3.0})
    rendered = TranscriptRichRenderer().render(state, width=80, color=False)

    assert "❯ 你好" in rendered
    assert "Thought for 2s" in rendered
    assert "● Read" in rendered
    assert "⎿ Read 12 lines" in rendered
    assert "● 完成。" in rendered
    assert "for 3s" in rendered
    assert rendered.count("完成。") == 1


def test_control_sequences_are_removed_before_markdown_projection():
    hostile = "safe\x1b]0;owned\x07\x1b[2Jtext"
    assert sanitize_terminal_text(hostile) == "safetext"


def test_active_thinking_appears_only_after_threshold():
    state = TranscriptState("thinking", clock=lambda: 0.2)
    state.start_turn("wait", ts=0.0)
    renderer = TranscriptRichRenderer()
    assert "Thinking" not in renderer.render(state, width=60, color=False, now=0.2)
    assert "Thinking" in renderer.render(state, width=60, color=False, now=0.4)
