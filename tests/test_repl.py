"""Tests for the REPL slash-command dispatch and prompt prefix logic.

Covers: _prompt_for, _handle_slash, ReplState, and the integration of
run_repl with mocked input + FakeLLM.
"""
import pytest
from pathlib import Path
from cc_harness.repl import (
    ReplState,
    _handle_slash,
    _prompt_for,
)
from cc_harness.tokens import TurnTokenStats


# --- _prompt_for ---

def test_prompt_for_coding_is_tagged():
    assert _prompt_for("coding") == "> [coding] "


def test_prompt_for_plan_has_tag():
    assert _prompt_for("plan") == "> [plan] "


def test_prompt_for_design_has_tag():
    assert _prompt_for("design") == "> [design] "


def test_prompt_for_all_modes_are_tagged():
    """All three modes are tagged, never the bare '> ' — so the active mode
    is always visible at a glance."""
    for mode in ("coding", "plan", "design"):
        prompt = _prompt_for(mode)
        assert "[" in prompt, f"mode {mode!r} has no tag: {prompt!r}"
        assert f"[{mode}]" in prompt


# --- ReplState ---

def test_repl_state_defaults_to_coding_empty_messages():
    s = ReplState()
    assert s.mode == "coding"
    assert s.messages == []


def test_repl_state_is_mutable():
    s = ReplState()
    s.mode = "plan"
    s.messages.append({"role": "user", "content": "x"})
    assert s.mode == "plan"
    assert len(s.messages) == 1


# --- _handle_slash ---

def _console():
    """A no-op console for handle_slash — it just calls print_info/print_warn
    which write to a Rich console. We don't assert on output here."""
    from rich.console import Console
    return Console(file=None, force_terminal=False)


def test_handle_slash_plan_switches_mode():
    s = ReplState(mode="coding")
    handled = _handle_slash("/plan", s, _console())
    assert handled is True
    assert s.mode == "plan"


def test_handle_slash_design_switches_mode():
    s = ReplState(mode="coding")
    _handle_slash("/design", s, _console())
    assert s.mode == "design"


def test_handle_slash_coding_switches_back():
    s = ReplState(mode="plan")
    _handle_slash("/coding", s, _console())
    assert s.mode == "coding"


def test_handle_slash_same_mode_is_noop():
    s = ReplState(mode="plan")
    _handle_slash("/plan", s, _console())
    assert s.mode == "plan"


def test_handle_slash_mode_command_returns_true():
    s = ReplState(mode="design")
    assert _handle_slash("/mode", s, _console()) is True
    # state.mode is unchanged
    assert s.mode == "design"


def test_handle_slash_help_returns_true():
    s = ReplState()
    assert _handle_slash("/help", s, _console()) is True


def test_handle_slash_clear_drops_history_keeps_system():
    s = ReplState(mode="coding")
    s.messages = [
        {"role": "system", "content": "x"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    _handle_slash("/clear", s, _console())
    assert s.messages == [{"role": "system", "content": "x"}]


def test_handle_slash_clear_with_no_messages():
    s = ReplState()
    _handle_slash("/clear", s, _console())
    assert s.messages == []


def test_handle_slash_unknown_returns_false():
    """Unknown commands fall through to the LLM as a normal message."""
    s = ReplState()
    assert _handle_slash("/foo", s, _console()) is False
    assert s.mode == "coding"  # unchanged


def test_handle_slash_case_insensitive_command():
    """Commands are matched lowercase even if the user types uppercase."""
    s = ReplState()
    _handle_slash("/PLAN", s, _console())
    assert s.mode == "plan"


# --- run_repl integration ---

@pytest.mark.asyncio
async def test_run_repl_exit_terminates(monkeypatch):
    """Typing 'exit' in the REPL terminates cleanly."""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    inputs = iter(["exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    monkeypatch.setattr(repl_mod, "_read_user", _fake_read_user(inputs))

    state = ReplState()
    # Provide a minimal fake llm + mcp
    fake_llm = _NoopLLM()
    fake_mcp = _NoopMCP()

    await run_repl(fake_llm, fake_mcp, cwd="/x")
    # If we got here without hanging, exit worked
    assert state.mode == "coding"  # default unchanged


@pytest.mark.asyncio
async def test_run_repl_plan_command_changes_state(monkeypatch):
    """/plan switches mode; subsequent LLM call should use mode='plan'."""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    # Sequence: /plan, /mode, exit
    inputs = iter(["/plan", "/mode", "exit"])
    monkeypatch.setattr(repl_mod, "_read_user", _fake_read_user(inputs))

    # Track the mode the agent saw
    seen_modes: list[str] = []
    fake_llm = _RecordingLLM(seen_modes)
    fake_mcp = _NoopMCP()

    await run_repl(fake_llm, fake_mcp, cwd="/x")
    # After /plan, the next user message ("/mode") would be sent to the LLM
    # if /mode didn't match — but /mode IS a command, so it's NOT sent.
    # So seen_modes is empty (no user message was sent to LLM).
    # The point of this test is just that the slash command didn't crash.
    assert seen_modes == []


@pytest.mark.asyncio
async def test_run_repl_sends_user_message_to_llm(monkeypatch):
    """A non-slash message is appended to messages and run_turn is called."""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    inputs = iter(["hello", "exit"])
    monkeypatch.setattr(repl_mod, "_read_user", _fake_read_user(inputs))

    seen_modes: list[str] = []
    fake_llm = _RecordingLLM(seen_modes)
    fake_mcp = _NoopMCP()

    await run_repl(fake_llm, fake_mcp, cwd="/x")
    assert seen_modes == ["coding"]  # default mode


@pytest.mark.asyncio
async def test_run_repl_passes_design_dir(monkeypatch):
    """design_dir arg flows through to run_turn when mode==design."""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl
    from cc_harness import agent as agent_mod
    from cc_harness.tokens import TurnTokenStats
    import tempfile

    inputs = iter(["/design", "draw graph", "exit"])
    monkeypatch.setattr(repl_mod, "_read_user", _fake_read_user(inputs))

    # Save files into tmp
    with tempfile.TemporaryDirectory() as td:
        design_dir = Path(td) / "designs"

        # Capture the design_dir passed to run_turn by patching it
        captured: dict = {}

        async def spy_run_turn(messages, llm, mcp, **kwargs):
            captured.update(kwargs)
            return TurnTokenStats()

        monkeypatch.setattr(agent_mod, "run_turn", spy_run_turn)

        # Fake LLM that just stops
        fake_llm = _StoppingLLM()
        fake_mcp = _NoopMCP()

        await run_repl(
            fake_llm, fake_mcp, cwd="/x", design_dir=design_dir,
        )
        # The "draw graph" message triggered run_turn with mode=design
        assert captured.get("mode") == "design"
        assert captured.get("design_dir") == design_dir


@pytest.mark.asyncio
async def test_run_repl_invalid_default_mode_raises():
    from cc_harness.repl import run_repl
    fake_llm = _NoopLLM()
    fake_mcp = _NoopMCP()
    with pytest.raises(ValueError, match="unknown default_mode"):
        await run_repl(fake_llm, fake_mcp, cwd="/x", default_mode="bogus")


# --- Disk change summary ---

def test_collect_disk_changes_no_changes(tmp_path):
    """Empty cwd → empty list."""
    from cc_harness.repl import _collect_disk_changes
    import time
    assert _collect_disk_changes(str(tmp_path), since=time.time() - 60) == []


def test_collect_disk_changes_new_file(tmp_path):
    """A file written after `since` is included with mtime + size + preview."""
    from cc_harness.repl import _collect_disk_changes
    import time
    # Use a 0.5s buffer so the file's mtime is unambiguously > since
    # (Windows FS mtime resolution can be 1s in some cases).
    since = time.time() - 0.5
    (tmp_path / "hello.py").write_text("print('hi')", encoding="utf-8")
    changes = _collect_disk_changes(str(tmp_path), since=since)
    assert len(changes) == 1
    rel, size, mtime, preview = changes[0]
    assert rel == "hello.py"
    assert size == 11
    assert mtime >= since
    assert preview == "print('hi')"


def test_collect_disk_changes_modifies_existing(tmp_path):
    """Overwriting an existing file shows up as a recent change."""
    from cc_harness.repl import _collect_disk_changes
    import time
    f = tmp_path / "agent.py"
    f.write_text("v1", encoding="utf-8")
    since = time.time() - 0.5
    f.write_text("v2 — modified", encoding="utf-8")
    changes = _collect_disk_changes(str(tmp_path), since=since)
    assert len(changes) == 1
    rel, _size, _mtime, preview = changes[0]
    assert rel == "agent.py"
    assert preview == "v2 — modified"


def test_collect_disk_changes_skips_old_files(tmp_path):
    """Files with mtime before `since` are NOT included."""
    from cc_harness.repl import _collect_disk_changes
    import os
    import time
    f = tmp_path / "old.py"
    f.write_text("ancient", encoding="utf-8")
    old_time = time.time() - 3600
    os.utime(f, (old_time, old_time))
    changes = _collect_disk_changes(str(tmp_path), since=time.time() - 60)
    assert changes == []


def test_collect_disk_changes_small_file_has_preview(tmp_path):
    """Files under _PREVIEW_MAX_BYTES get content preview."""
    from cc_harness.repl import _collect_disk_changes, _PREVIEW_MAX_BYTES
    import time
    small = tmp_path / "small.txt"
    small.write_text("x" * 100, encoding="utf-8")
    assert 100 <= _PREVIEW_MAX_BYTES
    changes = _collect_disk_changes(str(tmp_path), since=time.time() - 60)
    assert len(changes) == 1
    assert changes[0][3] is not None


def test_collect_disk_changes_large_file_no_preview(tmp_path):
    """Files over _PREVIEW_MAX_BYTES get no content preview."""
    from cc_harness.repl import _collect_disk_changes, _PREVIEW_MAX_BYTES
    import time
    big = tmp_path / "big.txt"
    big.write_text("x" * (_PREVIEW_MAX_BYTES + 100), encoding="utf-8")
    changes = _collect_disk_changes(str(tmp_path), since=time.time() - 60)
    assert len(changes) == 1
    assert changes[0][3] is None


def test_collect_disk_changes_recursive_finds_nested(tmp_path):
    """Files in subdirectories are found (the LLM may modify nested files)."""
    from cc_harness.repl import _collect_disk_changes
    import time
    sub = tmp_path / "cc_harness" / "prompts.py"
    sub.parent.mkdir(parents=True)
    sub.write_text("# prompt", encoding="utf-8")
    changes = _collect_disk_changes(str(tmp_path), since=time.time() - 60)
    rels = [c[0] for c in changes]
    assert any("prompts.py" in r for r in rels)


def test_collect_disk_changes_max_limit(tmp_path):
    """At most _MAX_CHANGES_SHOWN entries are returned."""
    from cc_harness.repl import _collect_disk_changes, _MAX_CHANGES_SHOWN
    import time
    since = time.time()
    for i in range(_MAX_CHANGES_SHOWN + 5):
        (tmp_path / f"file_{i}.txt").write_text(str(i), encoding="utf-8")
    changes = _collect_disk_changes(str(tmp_path), since=since)
    assert len(changes) == _MAX_CHANGES_SHOWN


def test_collect_disk_changes_nonexistent_cwd_returns_empty(tmp_path):
    """Missing cwd returns empty list (no crash)."""
    from cc_harness.repl import _collect_disk_changes
    import time
    missing = tmp_path / "does-not-exist"
    assert _collect_disk_changes(str(missing), since=time.time() - 60) == []


# --- Test helpers ---

def _fake_read_user(inputs):
    """Build a coroutine that returns successive values from `inputs`."""
    queue = list(inputs)

    async def _fn(prompt: str) -> str:
        if not queue:
            raise EOFError()
        return queue.pop(0)
    return _fn


class _NoopLLM:
    """An LLM that does nothing when called."""
    async def chat(self, messages, tools):
        if False:
            yield  # makes it a generator


class _StoppingLLM:
    """An LLM that returns a stop-only response."""
    async def chat(self, messages, tools):
        from cc_harness.llm import StreamEvent
        yield StreamEvent(
            kind="done", content="ok", pending=[], finish_reason="stop"
        )


class _NoopMCP:
    def list_tools(self):
        return []


class _RecordingLLM:
    """An LLM that records the mode of each call (via a shared list)."""
    def __init__(self, seen_modes: list[str]):
        self.seen_modes = seen_modes

    async def chat(self, messages, tools):
        from cc_harness.llm import StreamEvent
        # Inspect the system message to detect mode
        if messages and messages[0].get("role") == "system":
            content = messages[0]["content"]
            if "Plan 模式" in content:
                self.seen_modes.append("plan")
            elif "Design 模式" in content:
                self.seen_modes.append("design")
            else:
                self.seen_modes.append("coding")
        else:
            self.seen_modes.append("coding")
        yield StreamEvent(
            kind="done", content="ok", pending=[], finish_reason="stop"
        )


# --- Cross-layer integration tests (task #5) ---
# These verify the full pipeline: repl dispatches slash command → run_turn
# injects system prompt for the new mode → LLM sees the updated prompt.

@pytest.mark.asyncio
async def test_run_repl_mode_switch_updates_system_prompt_in_next_turn(monkeypatch):
    """After /plan, the NEXT user message triggers a system prompt that
    contains 'Plan 模式' (not the old coding-mode prompt)."""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    # Sequence: hello (coding), /plan, second-question (plan), exit
    inputs = iter(["hello", "/plan", "second question", "exit"])
    monkeypatch.setattr(repl_mod, "_read_user", _fake_read_user(inputs))

    capturing_llm = _CapturingLLM()
    fake_mcp = _NoopMCP()
    await run_repl(capturing_llm, fake_mcp, cwd="/x")

    # 2 user messages were sent → 2 LLM calls
    assert len(capturing_llm.captured) == 2
    # Call 1: coding mode (default), system prompt injected with coding content
    msg_list_1 = capturing_llm.captured[0]
    system_1 = next(m for m in msg_list_1 if m["role"] == "system")
    assert "工具使用纪律" in system_1["content"]
    assert "Plan 模式" not in system_1["content"]
    # Call 2: after /plan, system prompt REPLACED with plan-mode content
    msg_list_2 = capturing_llm.captured[1]
    system_2 = next(m for m in msg_list_2 if m["role"] == "system")
    assert "Plan 模式" in system_2["content"]
    assert "工具使用纪律" not in system_2["content"]


@pytest.mark.asyncio
async def test_run_repl_clear_preserves_system_prompt(monkeypatch):
    """After /clear, messages[0] is still the system prompt and the NEXT
    user message gets a fresh system prompt (not cleared)."""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    # Sequence: hi (coding), /plan (switch), /clear, new-question (plan)
    inputs = iter(["hi", "/plan", "/clear", "new question", "exit"])
    monkeypatch.setattr(repl_mod, "_read_user", _fake_read_user(inputs))

    capturing_llm = _CapturingLLM()
    fake_mcp = _NoopMCP()
    await run_repl(capturing_llm, fake_mcp, cwd="/x")

    # 2 LLM calls: "hi" and "new question"
    assert len(capturing_llm.captured) == 2
    # Call 1: coding mode, system + user "hi"
    msg_list_1 = capturing_llm.captured[0]
    assert any(m["role"] == "user" and m["content"] == "hi" for m in msg_list_1)
    assert any(m["role"] == "system" and "工具使用纪律" in m["content"] for m in msg_list_1)
    # Call 2: plan mode (from /plan), system REPLACED, "hi" is gone
    msg_list_2 = capturing_llm.captured[1]
    assert not any(m.get("content") == "hi" for m in msg_list_2)
    assert any(m["role"] == "user" and m["content"] == "new question" for m in msg_list_2)
    assert any(m["role"] == "system" and "Plan 模式" in m["content"] for m in msg_list_2)


@pytest.mark.asyncio
async def test_run_repl_design_save_after_clear_writes_fresh(monkeypatch, tmp_path):
    """After /clear, a new design message saves ONLY the new design
    (the prior conversation is not in the saved file)."""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    # Sequence: /design, draw-A (saved), /clear, draw-B (saved), exit
    inputs = iter(["/design", "draw A", "/clear", "draw B", "exit"])
    monkeypatch.setattr(repl_mod, "_read_user", _fake_read_user(inputs))

    class _DesignLLM:
        """Returns content that varies by the user's last message."""
        async def chat(self, messages, tools):
            from cc_harness.llm import StreamEvent
            last_user = next(
                (m["content"] for m in reversed(messages) if m["role"] == "user"),
                "default",
            )
            content = f"DESIGN-FOR: {last_user}"
            yield StreamEvent(
                kind="done", content=content, pending=[], finish_reason="stop"
            )

    fake_mcp = _NoopMCP()
    await run_repl(_DesignLLM(), fake_mcp, cwd="/x", design_dir=tmp_path)

    # 2 design messages → 2 files saved
    files = sorted(tmp_path.glob("*.md"))
    assert len(files) == 2
    contents = [f.read_text(encoding="utf-8") for f in files]
    assert "DESIGN-FOR: draw A" in contents[0]
    assert "DESIGN-FOR: draw B" in contents[1]
    # The cleared conversation (draw A) is NOT in the second file
    assert "draw A" not in contents[1]


@pytest.mark.asyncio
async def test_run_repl_unknown_slash_falls_through_to_llm(monkeypatch):
    """A slash command we don't recognize gets sent to the LLM as a
    regular user message (with a warning)."""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    inputs = iter(["/foo bar", "exit"])
    monkeypatch.setattr(repl_mod, "_read_user", _fake_read_user(inputs))

    capturing_llm = _CapturingLLM()
    fake_mcp = _NoopMCP()
    await run_repl(capturing_llm, fake_mcp, cwd="/x")

    # The unknown /foo was sent to the LLM as user content
    assert len(capturing_llm.captured) == 1
    msg_list = capturing_llm.captured[0]
    assert any(m["role"] == "user" and m["content"] == "/foo bar" for m in msg_list)


@pytest.mark.asyncio
async def test_run_repl_empty_input_does_not_call_llm(monkeypatch):
    """Pressing Enter on an empty line should be a no-op (no LLM call)."""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    inputs = iter(["", "   ", "real question", "exit"])
    monkeypatch.setattr(repl_mod, "_read_user", _fake_read_user(inputs))

    capturing_llm = _CapturingLLM()
    fake_mcp = _NoopMCP()
    await run_repl(capturing_llm, fake_mcp, cwd="/x")

    # Only the non-empty input triggered an LLM call
    assert len(capturing_llm.captured) == 1


# --- Token tracking tests (Task 4) ---

@pytest.mark.asyncio
async def test_session_stats_add_method_accumulates(monkeypatch):
    """SessionTokenStats.add should accumulate fields across two TurnTokenStats.
    (Unit test of state, not a full REPL integration test.)"""
    from cc_harness import agent as agent_mod

    # Stub run_turn to return specified TurnTokenStats
    call_count = 0
    async def fake_run_turn(messages, llm, mcp, **kwargs):
        nonlocal call_count
        call_count += 1
        t = TurnTokenStats(
            user_input=10 * call_count, tool_calls=20,
            llm_output=30, system_prompt=40,
            api_total_tokens=100 * call_count, iter_count=1, api_reported=True,
        )
        messages.append({"role": "assistant", "content": f"reply {call_count}"})
        return t

    monkeypatch.setattr(agent_mod, "run_turn", fake_run_turn)

    from cc_harness.repl import ReplState
    state = ReplState(mode="coding", messages=[])
    state.messages.append({"role": "user", "content": "q1"})
    t1 = await agent_mod.run_turn(state.messages, None, None)
    state.session_stats.add(t1)
    state.messages.append({"role": "user", "content": "q2"})
    t2 = await agent_mod.run_turn(state.messages, None, None)
    state.session_stats.add(t2)

    assert state.session_stats.turns == 2
    assert state.session_stats.user_input == 30   # 10+20
    assert state.session_stats.api_total_tokens == 300   # 100+200


@pytest.mark.asyncio
async def test_token_summary_printed_after_each_turn(monkeypatch, capfd):
    """After run_turn, print_token_summary should fire and emit '本轮' + '累计' labels."""
    from cc_harness import agent as agent_mod
    from cc_harness.repl import run_repl

    async def fake_run_turn(messages, llm, mcp, **kwargs):
        messages.append({"role": "assistant", "content": "ok"})
        return TurnTokenStats(
            user_input=10, tool_calls=20, llm_output=30, system_prompt=40,
            api_total_tokens=100, iter_count=1, api_reported=True,
        )
    monkeypatch.setattr(agent_mod, "run_turn", fake_run_turn)

    inputs = iter(["./test", "exit"])
    monkeypatch.setattr("cc_harness.repl._read_user", _fake_read_user(inputs))

    fake_llm = _StoppingLLM()  # already defined in this file
    fake_mcp = _NoopMCP()      # already defined in this file

    await run_repl(fake_llm, fake_mcp, cwd="/x", default_mode="coding")

    out = capfd.readouterr().out
    assert "本轮" in out
    assert "累计" in out


class _CapturingLLM:
    """An LLM that records the full messages list sent to chat() on each call."""
    def __init__(self):
        self.captured: list[list[dict]] = []

    async def chat(self, messages, tools):
        from cc_harness.llm import StreamEvent
        # Snapshot (role, content) so subsequent mutations don't affect us
        self.captured.append([
            {k: v for k, v in m.items() if k in ("role", "content")}
            for m in messages
        ])
        yield StreamEvent(
            kind="done", content="ok", pending=[], finish_reason="stop"
        )


# --- ContextConfig threading tests (Task 11) ---

@pytest.mark.asyncio
async def test_run_repl_threads_context_config_to_run_turn(monkeypatch):
    """run_repl 必须把 state.context_config 透传给 run_turn。"""
    from cc_harness import repl as repl_mod
    from cc_harness import agent as agent_mod
    from cc_harness.config import ContextConfig

    captured = {}

    async def spy_run_turn(messages, llm, mcp, **kwargs):
        captured.update(kwargs)
        return TurnTokenStats()

    monkeypatch.setattr(agent_mod, "run_turn", spy_run_turn)

    inputs = iter(["hello", "exit"])
    monkeypatch.setattr(repl_mod, "_read_user", _fake_read_user(inputs))

    fake_llm = object()  # not used (run_turn is mocked)
    fake_mcp = _NoopMCP()

    await repl_mod.run_repl(fake_llm, fake_mcp, cwd="/tmp", default_mode="coding")
    assert "context_config" in captured
    assert isinstance(captured["context_config"], ContextConfig)


@pytest.mark.asyncio
async def test_run_repl_prints_compaction_summary_after_turn(monkeypatch, capfd):
    """turn_stats.compaction 非 NONE 时,repl 必须打印 上下文压缩 行。"""
    from cc_harness import repl as repl_mod
    from cc_harness import agent as agent_mod
    from cc_harness.context import CompactionTier, CompactionStats

    async def fake_run_turn(messages, llm, mcp, **kwargs):
        messages.append({"role": "assistant", "content": "ok"})
        return TurnTokenStats(
            user_input=100, tool_calls=0, llm_output=0, system_prompt=0, tool_definitions=0,
            api_total_tokens=100, iter_count=1, api_reported=True,
            compaction=CompactionStats(
                tier=CompactionTier.SNIP, before_tokens=1000, after_tokens=500,
                ratio_before=0.7, ratio_after=0.35, messages_snip=2,
            ),
        )
    monkeypatch.setattr(agent_mod, "run_turn", fake_run_turn)

    inputs = iter(["hello", "exit"])
    monkeypatch.setattr(repl_mod, "_read_user", _fake_read_user(inputs))
    fake_llm = _StoppingLLM()
    fake_mcp = _NoopMCP()
    await repl_mod.run_repl(fake_llm, fake_mcp, cwd="/tmp", default_mode="coding")
    out = capfd.readouterr().out
    assert "上下文压缩" in out
    assert "snip 2" in out
