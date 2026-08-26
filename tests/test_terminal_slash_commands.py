import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from cc_harness.session_store import SessionRecord
from cc_harness.terminal.app import InlineTerminalApp
from cc_harness.tokens import SessionTokenStats, TokenCounter


class _Store:
    def __init__(self, root: Path):
        self.attachments_root = root / ".cc-harness" / "attachments"
        self.sessions = {
            "session-test": [{"role": "system", "content": "system"}],
            "old-session": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "old"},
            ],
        }
        self.events: dict[str, list[dict]] = {"session-test": []}
        self.renames: dict[str, str] = {}

    async def save(self, session_id, messages, *, mode, status="active"):
        self.sessions[session_id] = [dict(message) for message in messages]

    async def load(self, session_id):
        return [dict(message) for message in self.sessions[session_id]]

    async def list_recent(self, limit=20):
        return [
            SessionRecord(
                "old-session", Path("."), "coding", "Old session", 0, 0, "closed"
            )
        ][:limit]

    async def rename(self, session_id, title):
        self.renames[session_id] = title

    async def save_events(self, session_id, events):
        self.events[session_id] = list(events)

    async def load_events(self, session_id):
        return list(self.events.get(session_id, []))


class _Mcp:
    async def list_tools(self):
        return [{"function": {"name": "read_ref"}}]


def _runtime(tmp_path: Path):
    store = _Store(tmp_path)
    state = SimpleNamespace(
        context_config=SimpleNamespace(context_window=10_000),
        token_counter=TokenCounter(),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old user"},
            {"role": "assistant", "content": "old answer"},
        ],
        session_stats=SessionTokenStats(
            turns=2,
            turns_with_usage=2,
            api_prompt_tokens=5_113,
            api_completion_tokens=88,
            api_cache_read_prompt_tokens=4_992,
            api_total_tokens=5_201,
        ),
        started_at=datetime.now(UTC).isoformat(),
        mode="coding",
        todo_service=None,
        session_id="session-test",
        last_turn_text="old answer",
        todo_hints=["old hint"],
    )

    async def save():
        await store.save(state.session_id, state.messages, mode=state.mode)

    runtime = SimpleNamespace(
        llm=SimpleNamespace(
            model="test-model",
            reasoning_effort="high",
            reasoning_effort_supported=True,
        ),
        cwd=tmp_path,
        additional_dirs=(),
        state=state,
        session_store=store,
        warnings=[],
        mcp=_Mcp(),
        save=save,
    )
    return runtime, store


def _app(tmp_path: Path):
    runtime, store = _runtime(tmp_path)
    app = InlineTerminalApp(runtime, create_prompt_session=False)
    output = __import__("io").StringIO()
    app.console = Console(file=output, force_terminal=False, width=120)
    app.renderer.console = app.console
    return app, runtime, store, output


@pytest.mark.asyncio
async def test_clear_resets_messages_usage_and_transient_state(tmp_path):
    app, runtime, store, output = _app(tmp_path)
    app.queue.append(SimpleNamespace(text="queued"))
    app._checkpoints.append(("old", []))
    await app._handle_command("/clear")

    assert runtime.state.messages == [{"role": "system", "content": "system"}]
    assert runtime.state.session_stats.turns == 0
    assert runtime.state.session_stats.api_prompt_tokens == 0
    assert runtime.state.last_turn_text == ""
    assert runtime.state.todo_hints == []
    assert not app.queue
    assert not app._checkpoints
    assert store.events["session-test"] == []
    assert "API" in output.getvalue()
    toolbar = "".join(text for _style, text in app._context_status(120))
    assert "conversation 0%" in toolbar
    assert "system baseline" in toolbar


@pytest.mark.asyncio
async def test_all_inline_command_handlers_execute_without_provider_calls(tmp_path, monkeypatch):
    app, runtime, store, _output = _app(tmp_path)
    answers = iter(["new-name", "1"])
    app._ask = lambda _question: asyncio.sleep(0, result=next(answers))
    app._checkpoints.append(("before turn", [{"role": "system", "content": "system"}]))
    monkeypatch.setattr(
        "cc_harness.context.maybe_compact",
        lambda *args, **kwargs: asyncio.sleep(0, result=SimpleNamespace(tier=0)),
    )

    commands = (
        "/help", "/init", "/release-notes", "/status", "/mode", "/model test-model-2",
        "/coding", "/plan", "/design", "/chat",
        "/effort low", "/permissions default", "/verbose off", "/context", "/usage",
        "/tools", "/mcp", "/focus", "/diff", "/tasks", "/agents", "/inspector",
        "/tui default", "/tui fullscreen",
        "/rename renamed", "/branch branch-name", "/rewind", "/compact", "/clear", "/exit",
    )
    for command in commands:
        await app._handle_command(command)

    assert store.renames
    assert (tmp_path / "CC-HARNESS.md").is_file()
    assert "terminal renderer" in _output.getvalue().lower() or "终端界面" in _output.getvalue()


@pytest.mark.asyncio
async def test_resume_loads_session_and_starts_fresh_usage_window(tmp_path):
    app, runtime, _store, _output = _app(tmp_path)
    app._ask = lambda _question: asyncio.sleep(0, result="1")
    await app._handle_command("/resume")

    assert runtime.state.session_id == "old-session"
    assert runtime.state.messages[-1]["content"] == "old"
    assert runtime.state.session_stats.turns == 0
