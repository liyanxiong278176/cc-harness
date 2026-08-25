import os
from collections import deque
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from prompt_toolkit.styles import merge_styles
from prompt_toolkit.styles.defaults import default_ui_style
from rich.console import Console
from wcwidth import wcswidth

from cc_harness.project_instructions import initialize_project_instructions
from cc_harness.terminal.app import InlineTerminalApp, _InlinePromptSession
from cc_harness.terminal.renderer import TerminalRenderer
from cc_harness.terminal.settings import TerminalUISettings, load_terminal_settings
from cc_harness.tokens import TokenCounter


def _runtime(tmp_path: Path):
    state = SimpleNamespace(
        context_config=SimpleNamespace(context_window=1_000_000),
        token_counter=TokenCounter(),
        messages=[],
        started_at=datetime.now(UTC).isoformat(),
        mode="coding",
        todo_service=object(),
        session_id="session-test",
    )
    return SimpleNamespace(
        llm=SimpleNamespace(
            model="MiniMax-M3",
            reasoning_effort="high",
            reasoning_effort_supported=True,
        ),
        cwd=tmp_path,
        additional_dirs=(),
        state=state,
        session_store=SimpleNamespace(attachments_root=tmp_path / ".cc-harness" / "attachments"),
        warnings=[],
    )


@pytest.mark.parametrize("width", [146, 120, 100, 79])
def test_startup_panel_is_responsive_and_never_exceeds_terminal(tmp_path, width):
    output = StringIO()
    runtime = _runtime(tmp_path)
    renderer = TerminalRenderer(Console(file=output, force_terminal=False, width=width))
    renderer.startup(runtime, "0.1.0")
    rendered = output.getvalue()

    assert "cc-harness v0.1.0" in rendered
    assert "Welcome back!" in rendered
    assert "▀" in rendered or "▄" in rendered
    assert "Tips for getting started" in rendered
    assert "What's new" in rendered
    assert "/release-notes for more" in rendered
    for line in rendered.splitlines():
        assert wcswidth(line) <= width
    if width >= 80:
        assert "│" in rendered
    else:
        assert "Working directory:" in rendered


def test_status_area_matches_reference_layering(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "cc_harness.terminal.app.shutil.get_terminal_size",
        lambda fallback: os.terminal_size((146, 40)),
    )
    output = StringIO()
    runtime = _runtime(tmp_path)
    runtime.state.manifest = SimpleNamespace(name="eval")
    app = InlineTerminalApp.__new__(InlineTerminalApp)
    app.runtime = runtime
    app.permission_mode = "bypass-prompts"
    app.console = Console(file=output, force_terminal=False, width=146)
    app.renderer = TerminalRenderer(app.console)
    app.renderer.git_branch = lambda cwd: "master*"
    app.terminal_settings = TerminalUISettings()
    app._session_name = "mossy-kindling-flask"
    app._session_duration = lambda: 76 * 3_600 + 27 * 60
    app.queue = deque()
    app._active_task = None

    plain = "".join(text for _style, text, *_ in app._bottom_toolbar())
    lines = plain.splitlines()
    assert len(lines) == 5
    assert set(lines[0]) == {"─"}
    assert "[MiniMax-M3[1m]]" in lines[1]
    assert "eval git:(master*)" in lines[1]
    assert "mossy-kindling-flask" in lines[1]
    assert "⏱  76h 27m" in lines[1]
    assert "冲鸭" in lines[1]
    assert "● high · /effort" in lines[1]
    assert "Context" in lines[2]
    assert "0%" in lines[2]
    assert "API usage  unavailable" in lines[3]
    assert "bypass permissions on" in lines[4]
    assert "shift+tab to cycle" in lines[4]
    # A todo service alone is not evidence that subagents exist.
    assert "← for agents" not in lines[4]
    assert all(wcswidth(line) <= 146 for line in lines)


def test_status_area_shows_elapsed_async_command(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "cc_harness.terminal.app.shutil.get_terminal_size",
        lambda fallback: os.terminal_size((146, 40)),
    )
    runtime = _runtime(tmp_path)
    runtime.state.manifest = SimpleNamespace(name="eval")
    app = InlineTerminalApp.__new__(InlineTerminalApp)
    app.runtime = runtime
    app.permission_mode = "default"
    app.console = Console(force_terminal=False, width=146)
    app.renderer = TerminalRenderer(app.console)
    app.terminal_settings = TerminalUISettings()
    app._session_name = ""
    app._session_duration = lambda: 0
    app.queue = deque()
    app._active_task = None
    app._command_task = SimpleNamespace(done=lambda: False)
    app._command_label = "/compact"
    app._command_started = 0.0

    plain = "".join(text for _style, text, *_ in app._bottom_toolbar())

    assert "执行中 /compact" in plain


def test_prompt_frame_places_full_width_rule_above_prompt(monkeypatch):
    monkeypatch.setattr(
        "cc_harness.terminal.app.shutil.get_terminal_size",
        lambda fallback: os.terminal_size((146, 40)),
    )
    plain = "".join(text for _style, text in InlineTerminalApp._prompt_message())
    lines = plain.splitlines()
    assert len(lines) == 2
    assert lines[0] == "─" * 146
    assert lines[1] == "> "


def test_inline_prompt_height_tracks_input_instead_of_terminal_bottom(monkeypatch):
    monkeypatch.setattr(
        "cc_harness.terminal.app.shutil.get_terminal_size",
        lambda fallback: os.terminal_size((80, 40)),
    )
    session = SimpleNamespace(
        default_buffer=SimpleNamespace(text="first\nsecond", complete_state=None)
    )
    height = _InlinePromptSession._get_default_buffer_control_height(session)
    assert height.min == 2
    assert height.max == 2


def test_prompt_and_status_styles_do_not_paint_a_background():
    prompt_style = InlineTerminalApp._prompt_style()
    rules = dict(prompt_style.style_rules)
    for selector in ("", "bottom-toolbar", "bottom-toolbar.text"):
        assert "bg:ansidefault" in rules[selector]
    assert "noreverse" in rules[selector]
    assert rules["completion-menu.completion.current"] == "bg:#3b4252 #ffffff bold"
    assert rules["completion-menu.meta.completion"] == "bg:#20242b #e5e9f0"
    assert rules["completion-menu.meta.completion.current"] == "bg:#3b4252 #ffffff bold"
    merged = merge_styles([default_ui_style(), prompt_style])
    attrs = merged.get_attrs_for_style_str(
        "class:bottom-toolbar class:bottom-toolbar.text class:status.model"
    )
    assert attrs.bgcolor == "ansidefault"
    assert attrs.reverse is False


def test_init_creates_owned_project_instruction_contract(tmp_path):
    path, created = initialize_project_instructions(tmp_path)
    assert created is True
    assert path.name == "CC-HARNESS.md"
    assert "Project instructions for cc-harness" in path.read_text(encoding="utf-8")
    _, created_again = initialize_project_instructions(tmp_path)
    assert created_again is False


def test_project_terminal_settings_override_user_settings(tmp_path):
    user_root = tmp_path / "user"
    project_root = tmp_path / "project"
    user_root.mkdir()
    (project_root / ".cc-harness").mkdir(parents=True)
    (user_root / "settings.json").write_text(
        '{"ui":{"custom_line":"user","show_git":false}}', encoding="utf-8"
    )
    (project_root / ".cc-harness" / "settings.json").write_text(
        '{"ui":{"custom_line":"project"}}', encoding="utf-8"
    )

    settings = load_terminal_settings(project_root, user_root=user_root)
    assert settings.custom_line == "project"
    assert settings.show_git is False


def test_release_notes_are_real_renderer_content():
    output = StringIO()
    renderer = TerminalRenderer(Console(file=output, force_terminal=False, width=100))
    renderer.show_release_notes()
    rendered = output.getvalue()
    assert "cc-harness v0.1.0" in rendered
    assert "classic inline terminal shell" in rendered


def test_mascot_retains_reference_image_palette():
    mascot = TerminalRenderer._mascot()
    styles = {str(span.style) for span in mascot.spans}
    assert any("#d8c3a0" in style for style in styles)  # tan head
    assert any("#7a4333" in style for style in styles)  # brown outline/paw
    assert any("#2f65b7" in style for style in styles)  # blue eyes
    assert any("#f3f3ef" in style for style in styles)  # white body
