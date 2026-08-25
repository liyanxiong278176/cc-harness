"""Fullscreen alternate-screen terminal application.

This is intentionally a terminal application, not a GUI and not a spawned
terminal window.  prompt_toolkit owns the current terminal's alternate screen
while the renderer projects the same transcript events used by classic mode.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import shutil
import time
import uuid
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, FormattedText, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Dimension, HSplit, Layout, Window
from prompt_toolkit.layout.containers import ConditionalContainer, Float, FloatContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from rich.align import Align
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from cc_harness.terminal.app import InlineTerminalApp, QueuedInput
from cc_harness.terminal.completion import TerminalCompleter
from cc_harness.terminal.renderer import TerminalRenderer, _context_label
from cc_harness.terminal.settings import save_project_terminal_setting
from cc_harness.terminal.transcript import TranscriptState
from cc_harness.terminal.transcript_render import TranscriptRichRenderer


@dataclass
class _PermissionModal:
    tool: str
    args: dict
    reason: str
    future: asyncio.Future[str]
    selected: int = 0
    explain: bool = False


@dataclass
class _RewindModal:
    checkpoints: list
    future: asyncio.Future[int | None]
    selected: int = 0


@dataclass
class _ResumeModal:
    sessions: list
    future: asyncio.Future[str | None]
    selected: int = 0


@dataclass
class _InspectorState:
    tab: int = 0


_INSPECTOR_TABS = ("Overview", "Timeline", "Token", "Context", "Files", "Errors")


class _MouseTranscriptControl(FormattedTextControl):
    def __init__(self, owner: FullscreenTerminalApp, *args, **kwargs) -> None:
        self.owner = owner
        super().__init__(*args, **kwargs)

    def mouse_handler(self, mouse_event):
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self.owner._scroll_by(-3)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self.owner._scroll_by(3)
            return None
        return super().mouse_handler(mouse_event)


class FullscreenTerminalApp(InlineTerminalApp):
    """Claude Code-style terminal viewport with a fixed prompt/status stack."""

    def __init__(
        self, *args, version: str = "0.1.0", input=None, output=None, **kwargs,
    ) -> None:
        super().__init__(*args, create_prompt_session=False, **kwargs)
        self.version = version
        self.transcript = TranscriptState(self.runtime.state.session_id)
        self.transcript_renderer = TranscriptRichRenderer()
        self._application: Application | None = None
        self._modal: _PermissionModal | None = None
        self._rewind_modal: _RewindModal | None = None
        self._resume_modal: _ResumeModal | None = None
        self._inspector: _InspectorState | None = None
        self._active_checkpoint_index: int | None = None
        self._detailed = self.verbose
        self._focus = False
        self._auto_follow = True
        self._vertical_scroll_value = 0
        self._unseen_messages = 0
        self._rendered_lines = 0
        self._last_ctrl_l = 0.0
        self._driver_task: asyncio.Task | None = None
        self._task_cache: list = []
        self._show_task_list = False
        self._next_task_refresh = 0.0
        self._pt_input = input
        self._pt_output = output
        self.open_resume_on_start = False
        self._build_fullscreen_application()

    async def run(self, *, version: str = "0.1.0") -> int:
        self.version = version
        events = await self.runtime.session_store.load_events(self.runtime.state.session_id)
        if events:
            self.transcript.replay(events)
        elif self.runtime.state.messages:
            self.transcript = TranscriptState.from_messages(
                self.runtime.state.session_id, self.runtime.state.messages,
            )
        for warning in self.runtime.warnings:
            self.transcript.add_notice(warning.message, "warning")
        # session_event has a foreign key to session.  Create the session row
        # before the first user event, including on a brand-new empty session.
        await self.runtime.session_store.save(
            self.runtime.state.session_id,
            self.runtime.state.messages,
            mode=self.runtime.state.mode,
        )

        assert self._application is not None

        def start_driver() -> None:
            self._driver_task = self._application.create_background_task(self._driver())
            if self.open_resume_on_start:
                self._application.create_background_task(self._resume_card())

        try:
            await self._application.run_async(pre_run=start_driver)
        finally:
            if self._driver_task is not None and not self._driver_task.done():
                self._driver_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._driver_task
            if self._active_task is not None and not self._active_task.done():
                self._active_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._active_task
            await self._persist_transcript()
            await self.runtime.close()
        return 0

    def _build_fullscreen_application(self) -> None:
        state_dir = self.runtime.cwd / ".cc-harness"
        state_dir.mkdir(parents=True, exist_ok=True)
        self.input_buffer = Buffer(
            history=FileHistory(str(state_dir / "input-history")),
            auto_suggest=AutoSuggestFromHistory(),
            completer=TerminalCompleter(self.runtime.cwd),
            complete_while_typing=False,
            multiline=True,
            accept_handler=self._accept_input,
            read_only=Condition(lambda: self._overlay_active()),
        )
        input_control = BufferControl(
            buffer=self.input_buffer,
            input_processors=[BeforeInput(lambda: FormattedText([
                ("class:input.prompt", "❯ "),
            ]))],
            focus_on_click=True,
        )
        self._conversation_control = _MouseTranscriptControl(
            self,
            text=self._conversation_fragments,
            focusable=False,
            show_cursor=False,
            get_cursor_position=self._conversation_cursor_position,
        )
        self._conversation_window = Window(
            self._conversation_control,
            height=Dimension(min=1, weight=1),
            # Rich already wraps the transcript to the viewport width. prompt_toolkit
            # ignores get_vertical_scroll while wrap_lines=True.
            wrap_lines=False,
            always_hide_cursor=True,
            right_margins=[ScrollbarMargin(display_arrows=False)],
            get_vertical_scroll=lambda _window: self._vertical_scroll(),
        )
        self._input_window = Window(
            input_control,
            height=self._input_height,
            wrap_lines=True,
        )
        self._queue_container = ConditionalContainer(
            Window(
                FormattedTextControl(self._queue_fragments),
                height=lambda: Dimension.exact(min(5, max(1, len(self.queue)))),
                wrap_lines=True,
            ),
            filter=Condition(
                lambda: bool(self.queue)
                and not self._overlay_active()
            ),
        )
        self._task_container = ConditionalContainer(
            Window(
                FormattedTextControl(self._task_fragments),
                height=lambda: Dimension.exact(min(6, max(1, len(self._task_cache) + 1))),
                wrap_lines=False,
            ),
            filter=Condition(lambda: self._show_task_list and bool(self._task_cache)),
        )
        self._modal_container = ConditionalContainer(
            Window(
                FormattedTextControl(self._overlay_fragments),
                height=lambda: Dimension.exact(self._modal_height()),
                wrap_lines=True,
                style="class:modal",
            ),
            filter=Condition(
                lambda: self._overlay_active()
            ),
        )
        body = HSplit([
            self._conversation_window,
            self._task_container,
            self._queue_container,
            self._modal_container,
            Window(height=1, char="─", style="class:input.border"),
            self._input_window,
            Window(
                FormattedTextControl(self._status_fragments),
                height=5,
                dont_extend_height=True,
            ),
        ])
        root = FloatContainer(
            body,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=8, scroll_offset=1),
                ),
                Float(
                    right=4,
                    bottom=6,
                    width=self._jump_width,
                    height=1,
                    content=ConditionalContainer(
                        Window(
                            FormattedTextControl(self._jump_fragments, focusable=True),
                            height=1,
                            style="class:jump",
                        ),
                        filter=Condition(lambda: not self._auto_follow),
                    ),
                ),
            ],
        )
        self._application = Application(
            layout=Layout(root, focused_element=self._input_window),
            style=self._fullscreen_style(),
            key_bindings=self._build_fullscreen_key_bindings(),
            full_screen=True,
            mouse_support=True,
            refresh_interval=0.1,
            min_redraw_interval=0.03,
            paste_mode=False,
            input=self._pt_input,
            output=self._pt_output,
        )

    async def _driver(self) -> None:
        while not self._stop:
            now = time.monotonic()
            if now >= self._next_task_refresh:
                await self._refresh_task_cache()
                self._next_task_refresh = now + 1.0
            if self._active_task is None and self.queue:
                item = self.queue.popleft()
                self._active_task = asyncio.create_task(self._execute_queue_item(item))
            if self._active_task is not None and self._active_task.done():
                task = self._active_task
                self._active_task = None
                try:
                    await task
                except asyncio.CancelledError:
                    if self.transcript.active_turn is not None:
                        self.transcript.interrupt()
                        await self._persist_transcript()
                except Exception as exc:  # noqa: BLE001 - UI boundary
                    if self.transcript.active_turn is not None:
                        self.transcript.fail(str(exc))
                    else:
                        self.transcript.add_notice(str(exc), "error")
                    await self._persist_transcript()
            if self._application is not None:
                self._application.invalidate()
            await asyncio.sleep(0.05)
        if self._application is not None and not self._application.is_done:
            self._application.exit()

    def _accept_input(self, buffer: Buffer) -> bool:
        raw = buffer.text
        if not raw.strip():
            return False
        if raw.startswith("/"):
            assert self._application is not None
            self._application.create_background_task(self._dispatch_command(raw))
            return False
        item = QueuedInput(raw, attachments=list(self._pending_clipboard))
        self._pending_clipboard = []
        if self._active_task is not None and not self._active_task.done():
            self.queue.append(item)
            self.transcript.add_notice(f"Message queued ({len(self.queue)})", "info")
        else:
            self._active_task = asyncio.create_task(self._execute_queue_item(item))
        return False

    async def _execute_queue_item(self, item: QueuedInput) -> None:
        if item.text.lstrip().startswith("!"):
            await self._execute_shell(item.text)
        else:
            await self._execute_fullscreen(item)

    async def _execute_shell(self, raw: str) -> None:
        command = raw.lstrip()[1:].strip()
        if not command:
            self.transcript.add_notice("Type a command after !", "warning")
            return
        self.transcript.start_turn(raw)
        self.transcript.apply({
            "type": "action", "name": "Shell", "args": {"command": command},
            "ts": time.time(),
        })
        self._new_content_arrived(count=True)
        self._invalidate()
        started = time.monotonic()
        chunks: list[str] = []
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.runtime.cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert process.stdout is not None
            while True:
                data = await process.stdout.read(4096)
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                chunks.append(text)
                self.transcript.apply({
                    "type": "tool_output_delta", "text": text, "ts": time.time(),
                })
                self._invalidate()
            return_code = await process.wait()
            output = "".join(chunks)
            self.transcript.apply({
                "type": "observation",
                "text": output or f"Process exited with code {return_code}",
                "is_error": return_code != 0,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "ts": time.time(),
            })
            self.transcript.apply({"type": "result", "text": "", "ts": time.time()})
            self.runtime.state.messages.extend([
                {"role": "user", "content": f"[Shell command]\n{command}"},
                {"role": "assistant", "content": f"[Shell output]\n{output}"},
            ])
            await self.runtime.save()
            await self._persist_transcript()
        except Exception as exc:  # noqa: BLE001 - subprocess boundary
            self.transcript.fail(str(exc))
            await self._persist_transcript()

    async def _dispatch_command(self, raw: str) -> None:
        if raw.strip().lower() == "/focus":
            self._focus = not self._focus
            self.transcript.add_notice(f"focus: {'on' if self._focus else 'off'}")
        elif raw.strip().lower() == "/tui":
            self.transcript.add_notice("tui: fullscreen")
        elif raw.strip().lower() == "/tui default":
            save_project_terminal_setting(self.runtime.cwd, "tui", "default")
            self.transcript.add_notice(
                "Classic renderer selected for the next launch.", "info",
            )
        elif raw.strip().lower() == "/tui fullscreen":
            save_project_terminal_setting(self.runtime.cwd, "tui", "fullscreen")
            self.transcript.add_notice("Fullscreen renderer selected.", "info")
        elif raw.strip().lower() == "/rewind":
            await self._rewind_card()
        elif raw.strip().lower() == "/resume":
            await self._resume_card()
        elif raw.strip().lower().startswith("/branch"):
            await self._branch_session(raw)
        elif raw.strip().lower().startswith("/rename"):
            await self._rename_session(raw)
        elif raw.strip().lower() == "/clear":
            self.runtime.state.messages = [
                message for message in self.runtime.state.messages
                if message.get("role") == "system"
            ]
            self.transcript = TranscriptState(self.runtime.state.session_id)
            await self.runtime.save()
            await self._persist_transcript()
            self.transcript.add_notice("Conversation context cleared.", "info")
        elif raw.strip().lower() == "/tasks":
            await self._show_tasks(agents_only=False)
        elif raw.strip().lower() == "/agents":
            await self._show_tasks(agents_only=True)
        elif raw.strip().lower() == "/diff":
            self.transcript.add_notice(await self._session_diff(), "info")
        elif raw.strip().lower() in {"/inspector", "/run-inspector"}:
            self._toggle_inspector()
        else:
            # Inline command handlers intentionally print permanent output.
            # Capture that projection and commit it as a fullscreen notice so
            # stdout never corrupts the alternate-screen renderer.
            capture = StringIO()
            previous_console, previous_renderer = self.console, self.renderer
            self.console = Console(file=capture, force_terminal=False, width=self._terminal_width())
            self.renderer = TerminalRenderer(self.console, verbose=self.verbose, lang=self.lang)
            try:
                stop = await self._handle_command(raw)
            finally:
                self.console, self.renderer = previous_console, previous_renderer
            command_output = capture.getvalue().strip()
            if command_output:
                self.transcript.add_notice(command_output, "info")
            if stop:
                self._stop = True
        self._invalidate()

    async def _refresh_task_cache(self) -> None:
        service = getattr(self.runtime.state, "todo_service", None)
        if service is None or not hasattr(service, "list"):
            self._task_cache = []
            return
        try:
            self._task_cache = list(await service.list(include_done=True))
        except Exception:  # noqa: BLE001 - status UI must never stop a turn
            self._task_cache = []

    async def _show_tasks(self, *, agents_only: bool) -> None:
        await self._refresh_task_cache()
        tasks = self._agent_tasks() if agents_only else self._task_cache
        title = "Agents" if agents_only else "Tasks"
        if not tasks:
            self.transcript.add_notice(f"{title}: none", "info")
            return
        icons = {
            "done": "✓", "in_progress": "●", "pending": "○",
            "blocked": "!", "cancelled": "×",
        }
        lines = [title]
        for task in tasks[:30]:
            status = str(getattr(task, "status", "unknown"))
            task_id = str(getattr(task, "id", "?"))
            task_title = str(getattr(task, "title", "Untitled"))
            assignee = getattr(task, "assigned_to", None)
            suffix = f" · {assignee}" if assignee else ""
            lines.append(f"{icons.get(status, '·')} {task_title} [{task_id}] · {status}{suffix}")
        self.transcript.add_notice("\n".join(lines), "info")

    def _agent_tasks(self) -> list:
        return [
            task for task in self._task_cache
            if getattr(task, "parent_task", None) is not None
            or getattr(task, "assigned_to", None)
        ]

    async def _session_diff(self) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                "git", "-C", str(self.runtime.cwd), "diff", "--no-ext-diff", "--",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
        except OSError as exc:
            return f"Diff unavailable: {exc}"
        if process.returncode != 0:
            return "Diff unavailable: " + stderr.decode(errors="replace").strip()
        value = stdout.decode("utf-8", errors="replace").strip()
        if not value:
            return "No tracked file changes."
        limit = 16_000
        return value if len(value) <= limit else value[:limit] + "\n… diff truncated"

    async def _execute_fullscreen(self, queued: QueuedInput | str) -> None:
        item = queued if isinstance(queued, QueuedInput) else QueuedInput(queued)
        text = item.text
        display_text = text
        expanded = self._expand_pastes(text)
        if not self._session_name:
            self._session_name = self._compact_title(expanded)
        self._checkpoints.append((display_text[:80], copy.deepcopy(self.runtime.state.messages)))
        self._checkpoints = self._checkpoints[-100:]
        create_checkpoint = getattr(self.runtime.session_store, "create_checkpoint", None)
        if create_checkpoint is not None:
            self._active_checkpoint_index = await create_checkpoint(
                self.runtime.state.session_id,
                display_text[:80],
                copy.deepcopy(self.runtime.state.messages),
                event_count=len(self.transcript.events),
            )
        content, attachments = await self.attachments.prepare(
            expanded, confirm_outside=self._confirm_outside,
        )
        queued_attachments = [*item.attachments, *self._pending_clipboard]
        if queued_attachments:
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            content.extend(attachment.message_part for attachment in queued_attachments)
            attachments.extend(queued_attachments)
            self._pending_clipboard = []
        labels = [
            (f"Image #{index}" if attachment.kind == "image" else attachment.display_name)
            for index, attachment in enumerate(attachments, 1)
        ]
        self.transcript.start_turn(display_text, labels)
        self._new_content_arrived(count=True)
        await self._persist_transcript()
        try:
            await self.runtime.run_user_turn(
                expanded,
                event_emitter=self._on_agent_event,
                confirm_handler=self._confirm_tool,
                message_content=content,
            )
        except asyncio.CancelledError:
            if self.transcript.active_turn is not None:
                self.transcript.interrupt()
                await self._persist_transcript()
            raise
        except Exception as exc:
            if self.transcript.active_turn is not None:
                self.transcript.fail(str(exc))
                await self._persist_transcript()
            raise
        finally:
            self._active_checkpoint_index = None

    async def _on_agent_event(self, event: dict) -> None:
        if event.get("type") == "action":
            await self._snapshot_file_action(event)
        had_visible_text = bool(
            self.transcript.active_turn
            and (self.transcript.active_turn.items or self.transcript.active_turn.stream_text)
        )
        self.transcript.apply(event)
        kind = event.get("type")
        count = kind in ("action", "subagent_progress", "result", "failed", "error")
        if kind == "content_delta" and not had_visible_text:
            count = True
        self._new_content_arrived(count=count)
        self._invalidate()
        if event.get("type") in ("result", "failed", "error"):
            await self._persist_transcript()

    async def _snapshot_file_action(self, event: dict) -> None:
        """Snapshot a mutating tool's target before the tool is dispatched."""
        if self._active_checkpoint_index is None:
            return
        name = str(event.get("name", "")).lower()
        if not any(token in name for token in ("edit", "write", "create_file", "patch")):
            return
        args = event.get("args")
        if not isinstance(args, dict):
            return
        raw_path = next(
            (
                args.get(key)
                for key in ("path", "file_path", "filePath", "filename")
                if isinstance(args.get(key), str)
            ),
            None,
        )
        snapshot = getattr(self.runtime.session_store, "snapshot_checkpoint_file", None)
        if raw_path is not None and snapshot is not None:
            await snapshot(
                self.runtime.state.session_id,
                self._active_checkpoint_index,
                Path(raw_path),
            )

    async def _persist_transcript(self) -> None:
        if self.runtime.session_store is not None:
            await self.runtime.session_store.save_events(
                self.runtime.state.session_id,
                self.transcript.to_jsonable(),
            )

    async def _confirm_tool(self, tool: str, args: dict, reason: str) -> str:
        if self.permission_mode == "bypass-prompts":
            return "yes"
        if self.permission_mode == "auto-edit" and self._looks_like_project_edit(tool, args):
            return "yes"
        if self._application is None:
            return await super()._confirm_tool(tool, args, reason)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._modal = _PermissionModal(tool, args, reason, future)
        self._application.invalidate()
        try:
            return await future
        finally:
            self._modal = None
            self._application.layout.focus(self._input_window)
            self._application.invalidate()

    async def _confirm_outside(self, path: Path) -> bool:
        result = await self._confirm_tool(
            "Read", {"path": str(path)}, "Attachment is outside the workspace",
        )
        return result in ("yes", "always")

    def _resolve_modal(self, value: str) -> None:
        modal = self._modal
        if modal is not None and not modal.future.done():
            modal.future.set_result(value)

    def _conversation_fragments(self):
        # The scrollbar occupies one column; Rich owns wrapping for this window.
        width = max(12, self._terminal_width() - 1)
        startup = self._startup_ansi(width, compact=bool(self.transcript.turns))
        rendered = self.transcript_renderer.render(
            self.transcript,
            width=width,
            color=True,
            detailed=self._detailed,
            focus=self._focus,
            now=time.time(),
        )
        combined = startup + rendered
        fragments = to_formatted_text(ANSI(combined))
        self._rendered_lines = max(1, len(list(split_lines(fragments))))
        return fragments

    def _startup_ansi(self, width: int, *, compact: bool) -> str:
        output = StringIO()
        console = Console(
            file=output, force_terminal=True, color_system="truecolor",
            width=width, highlight=False, legacy_windows=False,
        )
        renderer = TerminalRenderer(console, lang=self.lang)
        if not compact:
            renderer.startup(self.runtime, self.version)
            return output.getvalue()
        mascot = renderer._mascot(compact=True)
        model = self.runtime.llm.model if self.runtime.llm else "?"
        context = self.runtime.state.context_config.context_window
        model_display = f"{model}[{_context_label(context)}]" if context else model
        details = Group(
            Text.assemble(("cc-harness ", "bold color(209)"), (f"v{self.version}", "dim")),
            Text(f"{model_display} · API Usage Billing", style="dim"),
            Text(str(self.runtime.cwd), style="dim"),
        )
        grid = Table.grid(padding=(0, 2))
        grid.add_column(width=16)
        grid.add_column(ratio=1)
        grid.add_row(Align.center(mascot), details)
        console.print(grid)
        console.print()
        return output.getvalue()

    def _status_fragments(self):
        return self._bottom_toolbar()

    def _queue_fragments(self):
        fragments: list[tuple[str, str]] = []
        for index, item in enumerate(self.queue, 1):
            preview = " ".join(item.text.split())
            if len(preview) > 100:
                preview = preview[:99] + "…"
            fragments.append(("class:queue", f"  {index}. queued · {preview}\n"))
        return FormattedText(fragments)

    def _task_fragments(self):
        icons = {
            "done": "✓", "in_progress": "●", "pending": "○",
            "blocked": "!", "cancelled": "×",
        }
        fragments: list[tuple[str, str]] = [("class:task.title", "Tasks\n")]
        for task in self._task_cache[:5]:
            status = str(getattr(task, "status", "unknown"))
            title = str(getattr(task, "title", "Untitled"))
            style = "class:task.active" if status == "in_progress" else "class:task"
            fragments.append((style, f"  {icons.get(status, '·')} {title} · {status}\n"))
        return FormattedText(fragments)

    def _modal_fragments(self):
        modal = self._modal
        if modal is None:
            return FormattedText([])
        width = self._terminal_width()
        target = self._tool_target(modal.args)
        options = (
            "Yes, allow once",
            "Yes, remember this scope",
            "No, tell cc-harness what to do differently",
        )
        fragments: list[tuple[str, str]] = [
            ("class:modal.border", "─" * width + "\n"),
            ("class:modal.title", f"Allow {modal.tool}?\n"),
            ("class:modal.text", f"  {target}\n" if target else ""),
            ("class:modal.dim", f"  {modal.reason}\n" if modal.reason else ""),
        ]
        for index, option in enumerate(options):
            marker = "❯ " if index == modal.selected else "  "
            style = "class:modal.selected" if index == modal.selected else "class:modal.text"
            fragments.append((style, marker + option + "\n"))
        if modal.explain:
            fragments.append(("class:modal.risk", f"Ctrl+E risk explanation: {self._risk_label(modal)}\n"))
        else:
            fragments.append(("class:modal.dim", "Ctrl+E to explain risk · Esc to deny\n"))
        return FormattedText(fragments)

    def _overlay_fragments(self):
        if self._inspector is not None:
            return self._inspector_fragments()
        if self._rewind_modal is not None:
            return self._rewind_fragments()
        if self._resume_modal is not None:
            return self._resume_fragments()
        return self._modal_fragments()

    def _overlay_active(self) -> bool:
        return any((self._modal, self._rewind_modal, self._resume_modal, self._inspector))

    def _toggle_inspector(self) -> None:
        self._inspector = None if self._inspector is not None else _InspectorState()
        self._invalidate()

    def _inspector_fragments(self):
        inspector = self._inspector
        if inspector is None:
            return FormattedText([])
        width = self._terminal_width()
        tab = _INSPECTOR_TABS[inspector.tab]
        fragments: list[tuple[str, str]] = [
            ("class:inspector.border", "─" * width + "\n"),
            ("class:inspector.title", f"Run Inspector · {tab}\n"),
            ("class:inspector.tabs", "  " + "  ".join(
                f"[{name}]" if name == tab else name for name in _INSPECTOR_TABS
            ) + "\n"),
        ]
        fragments.extend(self._inspector_tab_lines(tab))
        fragments.append(("class:inspector.dim", "←/→ switch tab · F2 or Esc close\n"))
        return FormattedText(fragments)

    def _inspector_tab_lines(self, tab: str) -> list[tuple[str, str]]:
        state = self.runtime.state
        stats = getattr(state, "session_stats", None)
        metadata = getattr(self.runtime, "prompt_metadata", {}) or {}
        if tab == "Overview":
            lines = [
                f"  model: {getattr(getattr(self.runtime, 'llm', None), 'model', '?')}",
                f"  mode: {getattr(state, 'mode', '?')} · queued: {len(self.queue)}",
                f"  turns: {len(self.transcript.turns)} · active: {'yes' if self.transcript.active_turn else 'no'}",
            ]
        elif tab == "Timeline":
            active = self.transcript.active_turn
            lines = [
                f"  turns: {len(self.transcript.turns)}",
                f"  current: {getattr(active, 'status', 'idle')}",
                f"  events: {len(self.transcript.events)}",
            ]
        elif tab == "Token":
            lines = [
                f"  API input: {int(getattr(stats, 'api_prompt_tokens', 0) or 0):,}",
                f"  API output: {int(getattr(stats, 'api_completion_tokens', 0) or 0):,}",
                f"  cache hit: {int(getattr(stats, 'api_cache_read_prompt_tokens', 0) or 0):,}",
                f"  cost: {self._usage_text().split(' cost=', 1)[-1] if 'cost=' in self._usage_text() else 'unavailable'}",
            ]
        elif tab == "Context":
            lines = [
                f"  context: {self._context_tokens():,} / {getattr(state.context_config, 'context_window', 0):,}",
                f"  prompt version: {metadata.get('version', 'unknown')}",
                f"  prompt digest: {str(metadata.get('digest', 'unknown'))[:16]}…",
                f"  cache epoch: {metadata.get('cache_epoch', 'unknown')}",
                f"  tool bundle: {str(metadata.get('tool_bundle_digest', 'unknown'))[:16]}…",
                "  prompt text: hidden",
            ]
        elif tab == "Files":
            lines = [f"  changed files: use /diff ({len(getattr(self.transcript, 'events', []))} events tracked)"]
        else:
            errors = sum(
                1 for turn in self.transcript.turns
                if getattr(turn, "status", "") == "error" or getattr(turn, "error", "")
            )
            lines = [f"  recorded turn errors: {errors}", "  error details remain in the transcript/event store"]
        return [("class:inspector.text", line[: self._terminal_width()] + "\n") for line in lines]

    def _rewind_fragments(self):
        modal = self._rewind_modal
        if modal is None:
            return FormattedText([])
        width = self._terminal_width()
        fragments: list[tuple[str, str]] = [
            ("class:modal.border", "─" * width + "\n"),
            ("class:modal.title", "Rewind to a checkpoint\n"),
            ("class:modal.dim", "Conversation and files changed after it will be restored.\n"),
        ]
        for index, checkpoint in enumerate(modal.checkpoints):
            marker = "❯ " if index == modal.selected else "  "
            label = checkpoint.label or "(empty prompt)"
            style = "class:modal.selected" if index == modal.selected else "class:modal.text"
            fragments.append((style, f"{marker}{label}\n"))
        fragments.append(("class:modal.dim", "Enter to restore · Esc to cancel\n"))
        return FormattedText(fragments)

    async def _rewind_card(self) -> None:
        list_checkpoints = getattr(self.runtime.session_store, "list_checkpoints", None)
        checkpoints = (
            await list_checkpoints(self.runtime.state.session_id, limit=8)
            if list_checkpoints is not None else []
        )
        if not checkpoints:
            self.transcript.add_notice("No checkpoints yet.", "warning")
            self._invalidate()
            return
        loop = asyncio.get_running_loop()
        future: asyncio.Future[int | None] = loop.create_future()
        self._rewind_modal = _RewindModal(checkpoints, future)
        self._invalidate()
        try:
            checkpoint_index = await future
        finally:
            self._rewind_modal = None
            if self._application is not None:
                self._application.layout.focus(self._input_window)
            self._invalidate()
        if checkpoint_index is None:
            return
        await self._restore_checkpoint(checkpoint_index)

    def _resume_fragments(self):
        modal = self._resume_modal
        if modal is None:
            return FormattedText([])
        width = self._terminal_width()
        fragments: list[tuple[str, str]] = [
            ("class:modal.border", "─" * width + "\n"),
            ("class:modal.title", "Resume a session\n"),
        ]
        for index, record in enumerate(modal.sessions):
            marker = "❯ " if index == modal.selected else "  "
            style = "class:modal.selected" if index == modal.selected else "class:modal.text"
            fragments.append((style, f"{marker}{record.title} · {record.session_id[:12]}\n"))
        fragments.append(("class:modal.dim", "Enter to resume · Esc to cancel\n"))
        return FormattedText(fragments)

    async def _resume_card(self) -> None:
        records = await self.runtime.session_store.list_recent(8)
        if not records:
            self.transcript.add_notice("No saved sessions.", "warning")
            return
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str | None] = loop.create_future()
        self._resume_modal = _ResumeModal(records, future)
        self._invalidate()
        try:
            session_id = await future
        finally:
            self._resume_modal = None
            if self._application is not None:
                self._application.layout.focus(self._input_window)
            self._invalidate()
        if session_id is None or session_id == self.runtime.state.session_id:
            return
        await self.runtime.save()
        record = next(item for item in records if item.session_id == session_id)
        self.runtime.state.session_id = session_id
        self.runtime.state.mode = record.mode
        self.runtime.state.messages = await self.runtime.session_store.load(session_id)
        events = await self.runtime.session_store.load_events(session_id)
        self.transcript = (
            TranscriptState(session_id)
            if events else TranscriptState.from_messages(session_id, self.runtime.state.messages)
        )
        if events:
            self.transcript.replay(events)
        self._session_name = record.title if record.title != "Untitled session" else ""
        self.attachments.session_dir = (
            self.runtime.session_store.attachments_root / session_id
        )
        self.transcript.add_notice(f"Resumed: {record.title}", "info")
        self._jump_bottom()

    async def _branch_session(self, raw: str) -> None:
        name = raw.strip()[len("/branch"):].strip()
        source = self.runtime.state.session_id
        session_id = uuid.uuid4().hex
        events = self.transcript.to_jsonable()
        self.runtime.state.session_id = session_id
        await self.runtime.session_store.save(
            session_id,
            self.runtime.state.messages,
            mode=self.runtime.state.mode,
        )
        await self.runtime.session_store.save_events(session_id, events)
        if name:
            await self.runtime.session_store.rename(session_id, name)
        self.transcript = TranscriptState(session_id)
        self.transcript.replay(events)
        self._session_name = name or f"branch-{session_id[:8]}"
        self.attachments.session_dir = (
            self.runtime.session_store.attachments_root / session_id
        )
        self.transcript.add_notice(
            f"Branched {source[:8]} → {session_id[:8]}", "info",
        )
        self._jump_bottom()

    async def _rename_session(self, raw: str) -> None:
        name = raw.strip()[len("/rename"):].strip()
        if not name:
            self.transcript.add_notice("Usage: /rename <name>", "warning")
            return
        await self.runtime.session_store.rename(self.runtime.state.session_id, name)
        self._session_name = " ".join(name.split())[:80]
        self.transcript.add_notice(f"Session renamed: {self._session_name}", "info")

    async def _restore_checkpoint(self, checkpoint_index: int) -> None:
        store = self.runtime.session_store
        session_id = self.runtime.state.session_id
        messages = await store.load_checkpoint_messages(session_id, checkpoint_index)
        events = await store.load_checkpoint_events(session_id, checkpoint_index)
        restored, removed = await store.restore_checkpoint_files(session_id, checkpoint_index)
        self.runtime.state.messages = messages
        self.transcript.replay(events)
        await store.save(
            session_id,
            messages,
            mode=self.runtime.state.mode,
        )
        await store.save_events(session_id, events)
        self._checkpoints = [
            item for item in self._checkpoints
            if item[1] != messages
        ]
        detail = f" · {restored} restored, {removed} removed" if restored or removed else ""
        self.transcript.add_notice(f"Rewound to checkpoint{detail}", "info")
        self._jump_bottom()

    def _jump_fragments(self):
        label = "Jump to bottom (ctrl+End) ↓"
        if self._unseen_messages:
            label = f"{self._unseen_messages} new · " + label

        def click(mouse_event):
            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                self._jump_bottom()

        return FormattedText([("class:jump", label, click)])

    def _build_fullscreen_key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("enter")
        def enter(event) -> None:
            if self._modal is not None:
                self._resolve_modal(("yes", "always", "no")[self._modal.selected])
            elif self._rewind_modal is not None:
                modal = self._rewind_modal
                if not modal.future.done():
                    modal.future.set_result(
                        modal.checkpoints[modal.selected].checkpoint_index
                    )
            elif self._resume_modal is not None:
                modal = self._resume_modal
                if not modal.future.done():
                    modal.future.set_result(modal.sessions[modal.selected].session_id)
            elif event.current_buffer.text.endswith("\\"):
                event.current_buffer.delete_before_cursor(1)
                event.current_buffer.insert_text("\n")
            else:
                event.current_buffer.validate_and_handle()

        @kb.add("c-j")
        @kb.add("escape", "enter")
        def newline(event) -> None:
            if not self._overlay_active():
                event.current_buffer.insert_text("\n")

        @kb.add(
            "up",
            filter=Condition(lambda: self._overlay_active()),
        )
        def modal_up(event) -> None:
            del event
            if self._inspector is not None:
                return
            if self._rewind_modal is not None:
                modal = self._rewind_modal
                modal.selected = (modal.selected - 1) % len(modal.checkpoints)
            elif self._resume_modal is not None:
                modal = self._resume_modal
                modal.selected = (modal.selected - 1) % len(modal.sessions)
            else:
                assert self._modal is not None
                self._modal.selected = (self._modal.selected - 1) % 3
            self._invalidate()

        @kb.add(
            "down",
            filter=Condition(lambda: self._overlay_active()),
        )
        def modal_down(event) -> None:
            del event
            if self._inspector is not None:
                return
            if self._rewind_modal is not None:
                modal = self._rewind_modal
                modal.selected = (modal.selected + 1) % len(modal.checkpoints)
            elif self._resume_modal is not None:
                modal = self._resume_modal
                modal.selected = (modal.selected + 1) % len(modal.sessions)
            else:
                assert self._modal is not None
                self._modal.selected = (self._modal.selected + 1) % 3
            self._invalidate()

        @kb.add(
            "up",
            filter=Condition(
                lambda: self._modal is None
                and self._rewind_modal is None
                and self._resume_modal is None
                and bool(self.queue)
                and not self.input_buffer.text
            ),
        )
        def restore_queued(event) -> None:
            item = self.queue.pop()
            event.current_buffer.text = item.text
            event.current_buffer.cursor_position = len(item.text)
            self._pending_clipboard.extend(item.attachments)
            self.transcript.add_notice("Queued message restored for editing", "info")
            self._invalidate()

        @kb.add("c-e", filter=Condition(lambda: self._modal is not None))
        def explain(event) -> None:
            del event
            assert self._modal is not None
            self._modal.explain = not self._modal.explain
            self._invalidate()

        @kb.add("escape")
        def escape(event) -> None:
            if self._inspector is not None:
                self._inspector = None
                self._invalidate()
            elif self._modal is not None:
                self._resolve_modal("no")
            elif self._rewind_modal is not None:
                if not self._rewind_modal.future.done():
                    self._rewind_modal.future.set_result(None)
            elif self._resume_modal is not None:
                if not self._resume_modal.future.done():
                    self._resume_modal.future.set_result(None)
            elif self._active_task is not None and not self._active_task.done():
                self._active_task.cancel()
            else:
                self._handle_escape(event.current_buffer, event.app)

        @kb.add("c-c")
        def cancel(event) -> None:
            if event.current_buffer.text:
                event.current_buffer.reset()
            elif self._active_task is not None and not self._active_task.done():
                self._active_task.cancel()
            else:
                self._stop = True

        @kb.add("c-d")
        def exit_session(event) -> None:
            if not event.current_buffer.text and self._active_task is None and not self.queue:
                self._stop = True

        @kb.add("s-tab")
        @kb.add("escape", "m")
        def permission(event) -> None:
            del event
            self._cycle_permissions()
            self._invalidate()

        @kb.add("c-o")
        def transcript(event) -> None:
            del event
            self._detailed = not self._detailed
            self._jump_bottom()

        @kb.add("f2")
        def inspector(event) -> None:
            del event
            self._toggle_inspector()

        @kb.add("left", filter=Condition(lambda: self._inspector is not None))
        def inspector_left(event) -> None:
            del event
            assert self._inspector is not None
            self._inspector.tab = (self._inspector.tab - 1) % len(_INSPECTOR_TABS)
            self._invalidate()

        @kb.add("right", filter=Condition(lambda: self._inspector is not None))
        def inspector_right(event) -> None:
            del event
            assert self._inspector is not None
            self._inspector.tab = (self._inspector.tab + 1) % len(_INSPECTOR_TABS)
            self._invalidate()

        @kb.add("c-t")
        def tasks(event) -> None:
            del event
            self._show_task_list = not self._show_task_list
            self._invalidate()

        @kb.add("c-g")
        @kb.add("c-x", "c-e")
        def external_editor(event) -> None:
            if not self._overlay_active():
                event.current_buffer.open_in_editor()

        @kb.add("c-s")
        def stash(event) -> None:
            if event.current_buffer.text:
                self._stashed_draft = event.current_buffer.text
                event.current_buffer.reset()
            elif self._stashed_draft:
                event.current_buffer.text = self._stashed_draft
                event.current_buffer.cursor_position = len(self._stashed_draft)
                self._stashed_draft = ""

        @kb.add("c-l")
        def redraw(event) -> None:
            now = time.monotonic()
            if now - self._last_ctrl_l <= 2.0:
                event.app.create_background_task(self._dispatch_command("/clear"))
                self._last_ctrl_l = 0.0
            else:
                self._last_ctrl_l = now
                event.app.renderer.clear()
                self.transcript.add_notice("Press Ctrl+L again to clear conversation", "info")
            event.app.invalidate()

        @kb.add("escape", "p")
        def model_picker(event) -> None:
            if not self._overlay_active():
                event.current_buffer.text = "/model "
                event.current_buffer.cursor_position = len(event.current_buffer.text)

        @kb.add("escape", "t")
        def effort(event) -> None:
            del event
            self._cycle_effort()
            self._invalidate()

        @kb.add("escape", "v")
        def clipboard(event) -> None:
            event.app.create_background_task(self._clipboard(event.current_buffer))

        @kb.add(Keys.BracketedPaste)
        def bracketed_paste(event) -> None:
            data = event.data.replace("\r\n", "\n").replace("\r", "\n")
            if len(data) > 800 or data.count("\n") > 2:
                self._paste_counter += 1
                marker = f"[Pasted text #{self._paste_counter} +{data.count(chr(10)) + 1} lines]"
                self._pastes[marker] = data
                event.current_buffer.insert_text(marker)
            else:
                event.current_buffer.insert_text(data)

        @kb.add("pageup")
        def page_up(event) -> None:
            del event
            self._scroll_by(-max(3, self._viewport_height() // 2))

        @kb.add("pagedown")
        def page_down(event) -> None:
            del event
            self._scroll_by(max(3, self._viewport_height() // 2))

        @kb.add("c-home")
        def top(event) -> None:
            del event
            self._auto_follow = False
            self._vertical_scroll_value = 0
            self._invalidate()

        @kb.add("c-end")
        def bottom(event) -> None:
            del event
            self._jump_bottom()

        return kb

    def _scroll_by(self, amount: int) -> None:
        maximum = max(0, self._rendered_lines - self._viewport_height())
        current = self._vertical_scroll()
        self._vertical_scroll_value = max(0, min(maximum, current + amount))
        self._auto_follow = self._vertical_scroll_value >= maximum
        if self._auto_follow:
            self._unseen_messages = 0
        self._invalidate()

    def _jump_bottom(self) -> None:
        self._auto_follow = True
        self._unseen_messages = 0
        self._vertical_scroll_value = max(0, self._rendered_lines - self._viewport_height())
        self._invalidate()

    def _new_content_arrived(self, *, count: bool) -> None:
        if self._auto_follow:
            self._vertical_scroll_value = max(0, self._rendered_lines - self._viewport_height())
        elif count:
            self._unseen_messages += 1

    def _vertical_scroll(self) -> int:
        maximum = max(0, self._rendered_lines - self._viewport_height())
        if self._auto_follow:
            self._vertical_scroll_value = maximum
        return max(0, min(maximum, self._vertical_scroll_value))

    def _conversation_cursor_position(self) -> Point:
        """Keep prompt_toolkit's hidden cursor inside the requested viewport."""
        top = self._vertical_scroll()
        bottom = min(self._rendered_lines - 1, top + self._viewport_height() - 1)
        return Point(x=0, y=max(0, bottom))

    def _input_height(self) -> Dimension:
        width = max(12, self._terminal_width() - 3)
        rows = 0
        for line in self.input_buffer.text.split("\n"):
            rows += max(1, (len(line) + width - 1) // width)
        return Dimension.exact(min(8, max(1, rows)))

    def _viewport_height(self) -> int:
        render_info = getattr(self._conversation_window, "render_info", None)
        if render_info is not None and render_info.window_height > 0:
            return render_info.window_height
        task_rows = min(6, len(self._task_cache) + 1) if self._show_task_list else 0
        return max(
            4,
            shutil.get_terminal_size(fallback=(80, 24)).lines
            - 7 - task_rows - self._input_height().preferred,
        )

    def _terminal_width(self) -> int:
        return max(40, shutil.get_terminal_size(fallback=(80, 24)).columns)

    def _modal_height(self) -> int:
        if self._inspector is not None:
            return 8
        if self._rewind_modal is not None:
            return 4 + len(self._rewind_modal.checkpoints)
        if self._resume_modal is not None:
            return 3 + len(self._resume_modal.sessions)
        return 10 if self._modal is not None and self._modal.explain else 9

    async def _clipboard(self, buffer=None) -> None:
        try:
            attachment = await self.attachments.from_clipboard()
        except Exception as exc:  # noqa: BLE001 - platform clipboard boundary
            self.transcript.add_notice(str(exc), "warning")
            self._invalidate()
            return
        self._pending_clipboard.append(attachment)
        if buffer is not None:
            buffer.insert_text(f"[Image #{len(self._pending_clipboard)}]")
        self.transcript.add_notice(
            f"Clipboard image attached: {attachment.display_name}", "info",
        )
        self._invalidate()

    def _handle_escape(self, buffer, app) -> None:
        if self._active_task is not None and not self._active_task.done():
            self._active_task.cancel()
            return
        now = time.monotonic()
        if now - self._last_escape > 0.6:
            self._last_escape = now
            return
        self._last_escape = 0.0
        if buffer.text:
            self._stashed_draft = buffer.text
            buffer.reset()
            return
        app.create_background_task(self._rewind_card())

    def _jump_width(self) -> int:
        return min(self._terminal_width() - 4, 48)

    def _invalidate(self) -> None:
        if self._application is not None:
            self._application.invalidate()

    @staticmethod
    def _tool_target(args: dict) -> str:
        for key in ("command", "path", "file_path", "url", "query"):
            value = args.get(key)
            if isinstance(value, str):
                return value
        return json.dumps(args, ensure_ascii=False) if args else ""

    @staticmethod
    def _risk_label(modal: _PermissionModal) -> str:
        text = (modal.tool + " " + FullscreenTerminalApp._tool_target(modal.args)).lower()
        if any(token in text for token in ("rm ", "remove-item", "delete", "format", "shutdown")):
            return "High risk · destructive or irreversible operation"
        if any(token in text for token in ("write", "edit", "install", "push", "network")):
            return "Med risk · changes files, dependencies, or external state"
        return "Low risk · scoped read or routine project operation"

    @staticmethod
    def _fullscreen_style() -> Style:
        rules = dict(InlineTerminalApp._prompt_style().style_rules)
        rules.update({
            "queue": "#888888 italic",
            "modal": "bg:ansidefault",
            "modal.border": "#ff875f",
            "modal.title": "#ff875f bold",
            "modal.text": "#d0d0d0",
            "modal.dim": "#888888",
            "modal.selected": "#ffffff bold",
            "modal.risk": "#ffd75f",
            "inspector.border": "#5f87af",
            "inspector.title": "#87d7ff bold",
            "inspector.tabs": "#afd7ff",
            "inspector.text": "#d0d0d0",
            "inspector.dim": "#888888",
            "jump": "#ffffff bg:#444444",
            "task.title": "#888888 bold",
            "task": "#888888",
            "task.active": "#ffffff",
        })
        return Style.from_dict(rules)
