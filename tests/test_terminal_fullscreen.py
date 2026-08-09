import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from prompt_toolkit.data_structures import Point
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.layout.controls import UIContent
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput

from cc_harness.terminal.fullscreen import FullscreenTerminalApp
from cc_harness.tokens import TokenCounter


class FakeSessionStore:
    def __init__(self, root: Path):
        self.attachments_root = root / ".cc-harness" / "attachments"
        self.saved_events = []

    async def save_events(self, session_id, events):
        self.saved_events = list(events)


class FakeCheckpoint:
    def __init__(self, index: int, label: str):
        self.checkpoint_index = index
        self.label = label


def make_runtime(tmp_path: Path):
    async def save():
        return None

    state = SimpleNamespace(
        context_config=SimpleNamespace(context_window=1_000_000),
        token_counter=TokenCounter(),
        messages=[],
        started_at=datetime.now(UTC).isoformat(),
        mode="coding",
        todo_service=None,
        session_id="session-fullscreen",
    )
    return SimpleNamespace(
        llm=SimpleNamespace(model="test-model", reasoning_effort="high"),
        cwd=tmp_path,
        additional_dirs=(),
        state=state,
        session_store=FakeSessionStore(tmp_path),
        warnings=[],
        save=save,
    )


@pytest.fixture
def fullscreen_app(tmp_path):
    with create_pipe_input() as pipe_input:
        app = FullscreenTerminalApp(
            make_runtime(tmp_path), input=pipe_input, output=DummyOutput(),
        )
        yield app


def test_fullscreen_owns_alternate_screen_and_real_input_buffer(fullscreen_app):
    assert fullscreen_app._application.full_screen is True
    assert fullscreen_app.input_buffer.multiline()
    assert fullscreen_app._application.layout.current_control.buffer is fullscreen_app.input_buffer


@pytest.mark.asyncio
async def test_busy_submit_queues_real_prompt(fullscreen_app):
    fullscreen_app._active_task = asyncio.Future()
    fullscreen_app.input_buffer.text = "follow up"
    fullscreen_app.input_buffer.validate_and_handle()
    assert [item.text for item in fullscreen_app.queue] == ["follow up"]
    assert fullscreen_app.input_buffer.text == ""
    assert fullscreen_app.transcript.pending_notices[-1][1] == "Message queued (1)"


@pytest.mark.asyncio
async def test_permission_card_resolves_inside_fullscreen(fullscreen_app):
    waiter = asyncio.create_task(fullscreen_app._confirm_tool(
        "Bash", {"command": "pytest"}, "run tests",
    ))
    await asyncio.sleep(0)
    assert fullscreen_app._modal is not None
    text = "".join(fragment[1] for fragment in fullscreen_app._modal_fragments())
    assert "Allow Bash?" in text
    assert "pytest" in text
    fullscreen_app._resolve_modal("yes")
    assert await waiter == "yes"
    assert fullscreen_app._modal is None


def test_scroll_away_counts_real_message_boundaries(fullscreen_app):
    fullscreen_app._auto_follow = False
    fullscreen_app._new_content_arrived(count=False)
    assert fullscreen_app._unseen_messages == 0


def test_conversation_window_uses_scrollable_prewrapped_lines(fullscreen_app):
    assert fullscreen_app._conversation_window.wrap_lines() is False
    assert fullscreen_app._conversation_window.get_vertical_scroll is not None


def test_scroll_changes_viewport_and_hidden_cursor(fullscreen_app, monkeypatch):
    monkeypatch.setattr(fullscreen_app, "_viewport_height", lambda: 20)
    fullscreen_app._rendered_lines = 100
    fullscreen_app._jump_bottom()
    assert fullscreen_app._vertical_scroll() == 80

    fullscreen_app._scroll_by(-12)
    assert fullscreen_app._auto_follow is False
    assert fullscreen_app._vertical_scroll() == 68
    assert fullscreen_app._conversation_cursor_position() == Point(x=0, y=87)


def test_mouse_wheel_scrolls_conversation(fullscreen_app, monkeypatch):
    monkeypatch.setattr(fullscreen_app, "_viewport_height", lambda: 10)
    fullscreen_app._rendered_lines = 60
    fullscreen_app._jump_bottom()
    event = MouseEvent(
        position=Point(x=2, y=2),
        event_type=MouseEventType.SCROLL_UP,
        button=MouseButton.NONE,
        modifiers=frozenset(),
    )

    fullscreen_app._conversation_control.mouse_handler(event)

    assert fullscreen_app._vertical_scroll() == 47
    assert fullscreen_app._auto_follow is False


def test_prompt_toolkit_window_applies_requested_scroll(fullscreen_app, monkeypatch):
    monkeypatch.setattr(fullscreen_app, "_viewport_height", lambda: 10)
    fullscreen_app._rendered_lines = 60
    fullscreen_app._jump_bottom()

    def content():
        return UIContent(
            get_line=lambda _line: [("", "line")],
            line_count=60,
            cursor_position=fullscreen_app._conversation_cursor_position(),
            show_cursor=False,
        )

    fullscreen_app._conversation_window._scroll(content(), width=79, height=10)
    assert fullscreen_app._conversation_window.vertical_scroll == 50

    fullscreen_app._scroll_by(-7)
    fullscreen_app._conversation_window._scroll(content(), width=79, height=10)
    assert fullscreen_app._conversation_window.vertical_scroll == 43
    fullscreen_app._new_content_arrived(count=True)
    assert fullscreen_app._unseen_messages == 1
    fullscreen_app._jump_bottom()
    assert fullscreen_app._auto_follow is True
    assert fullscreen_app._unseen_messages == 0


def test_conversation_projection_switches_to_compact_header_after_submit(fullscreen_app):
    empty = fullscreen_app._startup_ansi(100, compact=False)
    compact = fullscreen_app._startup_ansi(100, compact=True)
    assert "Tips for getting started" in empty
    assert "Tips for getting started" not in compact
    assert "cc-harness" in compact


def test_rewind_overlay_is_an_in_app_card(fullscreen_app):
    loop = asyncio.new_event_loop()
    try:
        future = loop.create_future()
        fullscreen_app._rewind_modal = SimpleNamespace(
            checkpoints=[FakeCheckpoint(2, "change parser")],
            selected=0,
            future=future,
        )
        text = "".join(fragment[1] for fragment in fullscreen_app._overlay_fragments())
        assert "Rewind to a checkpoint" in text
        assert "change parser" in text
        assert "Enter to restore" in text
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_mutating_action_snapshots_real_target(fullscreen_app):
    calls = []

    async def snapshot(session_id, checkpoint, path):
        calls.append((session_id, checkpoint, path))

    fullscreen_app.runtime.session_store.snapshot_checkpoint_file = snapshot
    fullscreen_app._active_checkpoint_index = 7
    await fullscreen_app._snapshot_file_action({
        "type": "action", "name": "Edit", "args": {"file_path": "module.py"},
    })
    assert calls == [("session-fullscreen", 7, Path("module.py"))]


@pytest.mark.asyncio
async def test_non_mutating_action_does_not_snapshot(fullscreen_app):
    calls = []

    async def snapshot(*args):
        calls.append(args)

    fullscreen_app.runtime.session_store.snapshot_checkpoint_file = snapshot
    fullscreen_app._active_checkpoint_index = 7
    await fullscreen_app._snapshot_file_action({
        "type": "action", "name": "Read", "args": {"file_path": "module.py"},
    })
    assert calls == []


@pytest.mark.asyncio
async def test_shell_mode_streams_into_transcript_without_stdout(fullscreen_app):
    await fullscreen_app._execute_shell("! echo shell-ok")
    turn = fullscreen_app.transcript.turns[-1]
    tools = [item for item in turn.items if getattr(item, "name", "") == "Shell"]
    assert turn.status == "success"
    assert len(tools) == 1
    assert "shell-ok" in tools[0].output
    assert fullscreen_app.runtime.state.messages[-2]["content"].startswith("[Shell command]")
