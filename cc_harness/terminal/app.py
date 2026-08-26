"""Claude Code-style inline terminal session."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import shutil
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import prompt
from prompt_toolkit.styles import Style
from rich.console import Console
from wcwidth import wcswidth

from cc_harness.project_instructions import initialize_project_instructions
from cc_harness.terminal.attachments import AttachmentManager
from cc_harness.terminal.commands import COMMAND_MAP, COMMANDS
from cc_harness.terminal.completion import TerminalCompleter
from cc_harness.terminal.renderer import TerminalRenderer
from cc_harness.terminal.settings import load_terminal_settings, save_project_terminal_setting
from cc_harness.tokens import SessionTokenStats

_PERMISSION_MODES = ("default", "auto-edit", "bypass-prompts")
_WORK_MODES = ("coding", "plan", "design", "chat")


@dataclass
class QueuedInput:
    text: str
    attachments: list = field(default_factory=list)


class _InlinePromptSession(PromptSession):
    """PromptSession whose input window only occupies its rendered content height."""

    def _get_default_buffer_control_height(self) -> Dimension:
        columns = max(12, shutil.get_terminal_size(fallback=(80, 24)).columns - 4)
        visual_rows = 0
        for line in self.default_buffer.text.split("\n"):
            line_width = max(0, wcswidth(line))
            visual_rows += max(1, (line_width + columns - 1) // columns)
        if self.default_buffer.complete_state is not None:
            visual_rows += min(6, len(self.default_buffer.complete_state.completions))
        return Dimension.exact(min(10, max(1, visual_rows)))


class InlineTerminalApp:
    def __init__(
        self,
        runtime,
        *,
        lang: str = "en",
        verbose: bool = False,
        permission_mode: str = "default",
        console: Console | None = None,
        create_prompt_session: bool = True,
    ) -> None:
        self.runtime = runtime
        self.lang = lang
        self.verbose = verbose
        self.permission_mode = permission_mode
        self.console = console or Console(highlight=False)
        self.renderer = TerminalRenderer(self.console, verbose=verbose, lang=lang)
        self.terminal_settings = load_terminal_settings(runtime.cwd)
        runtime.terminal_settings = self.terminal_settings
        self.queue: deque[QueuedInput] = deque()
        self._pending_clipboard = []
        self._pastes: dict[str, str] = {}
        self._paste_counter = 0
        self._stashed_draft = ""
        self._last_escape = 0.0
        self._checkpoints: list[tuple[str, list[dict]]] = []
        self._session_name = self._initial_session_name()
        self._stop = False
        self._last_idle_interrupt = 0.0
        self._active_task: asyncio.Task | None = None
        self._command_task: asyncio.Task | None = None
        self._command_label: str | None = None
        self._command_started = 0.0
        self._kb = self._build_key_bindings()
        state_dir = runtime.cwd / ".cc-harness"
        state_dir.mkdir(parents=True, exist_ok=True)
        self.attachments = AttachmentManager(
            runtime.cwd,
            runtime.additional_dirs,
            runtime.session_store.attachments_root / runtime.state.session_id,
        )
        self.prompt_session = None
        if create_prompt_session:
            self.prompt_session = _InlinePromptSession(
                history=FileHistory(str(state_dir / "input-history")),
                auto_suggest=AutoSuggestFromHistory(),
                # Command help stays Chinese in the picker, matching the
                # project-facing TUI even when the surrounding UI is English.
                completer=TerminalCompleter(runtime.cwd, lang="zh-CN"),
                # Keep slash commands discoverable without requiring Tab.
                complete_while_typing=True,
                multiline=True,
                key_bindings=self._kb,
                bottom_toolbar=self._bottom_toolbar,
                style=self._prompt_style(),
                prompt_continuation=lambda width, line_number, is_soft_wrap: "  · ",
            )

    async def run(self, *, version: str = "0.1.0") -> int:
        assert self.prompt_session is not None
        self.renderer.startup(self.runtime, version)
        for warning in self.runtime.warnings:
            self.renderer.warning(warning.message)

        prompt_task: asyncio.Task | None = None
        with patch_stdout(raw=True):
            while not self._stop:
                if self._active_task is None and self._command_task is None and self.queue:
                    item = self.queue.popleft()
                    self._active_task = asyncio.create_task(self._execute(item.text))
                if prompt_task is None:
                    prompt_task = asyncio.create_task(
                        self.prompt_session.prompt_async(
                            self._prompt_message(),
                            refresh_interval=0.5,
                            handle_sigint=False,
                        )
                    )
                wait_for = {prompt_task}
                if self._active_task is not None:
                    wait_for.add(self._active_task)
                if self._command_task is not None:
                    wait_for.add(self._command_task)
                done, _ = await asyncio.wait(wait_for, return_when=asyncio.FIRST_COMPLETED)

                if self._active_task is not None and self._active_task in done:
                    task = self._active_task
                    self._active_task = None
                    try:
                        await task
                    except asyncio.CancelledError:
                        self.renderer.warning(self._t("当前轮已取消。", "Current turn cancelled."))
                    except Exception as exc:  # noqa: BLE001 - terminal boundary reports provider failures
                        self.renderer.error(str(exc))

                if self._command_task is not None and self._command_task in done:
                    task = self._command_task
                    self._command_task = None
                    self._command_label = None
                    try:
                        if task.result():
                            self._stop = True
                    except asyncio.CancelledError:
                        self.renderer.warning(self._t("命令已取消。", "Command cancelled."))
                    except Exception as exc:  # noqa: BLE001 - terminal boundary
                        self.renderer.error(str(exc))

                if prompt_task in done:
                    try:
                        raw = prompt_task.result()
                    except EOFError:
                        self._stop = True
                        prompt_task = None
                        break
                    except KeyboardInterrupt:
                        prompt_task = None
                        await self._handle_interrupt()
                        continue
                    prompt_task = None
                    raw = raw.strip()
                    if not raw:
                        continue
                    if raw.startswith("/"):
                        if self._command_task is not None and not self._command_task.done():
                            self.renderer.warning(self._t(
                                "请先等待当前命令完成。",
                                "Wait for the current command to finish.",
                            ))
                            continue
                        self._command_label = raw.split(maxsplit=1)[0]
                        self._command_started = time.monotonic()
                        self._command_task = asyncio.create_task(self._handle_command(raw))
                        continue
                    if (
                        self._active_task is not None
                        or self._command_task is not None
                    ):
                        self.queue.append(QueuedInput(raw))
                        self.renderer.info(self._t(
                            f"消息已排队（{len(self.queue)}）",
                            f"Message queued ({len(self.queue)})",
                        ))
                    else:
                        self._active_task = asyncio.create_task(self._execute(raw))

        if prompt_task is not None and not prompt_task.done():
            prompt_task.cancel()
        if self._active_task is not None and not self._active_task.done():
            self._active_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._active_task
        if self._command_task is not None and not self._command_task.done():
            self._command_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._command_task
        await self.runtime.close()
        return 0

    async def _execute(self, text: str) -> None:
        display_text = text
        text = self._expand_pastes(text)
        if not self._session_name:
            self._session_name = self._compact_title(text)
        self._checkpoints.append((display_text[:80], copy.deepcopy(self.runtime.state.messages)))
        self._checkpoints = self._checkpoints[-100:]
        content, attachments = await self.attachments.prepare(
            text, confirm_outside=self._confirm_outside,
        )
        if self._pending_clipboard:
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            content.extend(item.message_part for item in self._pending_clipboard)
            attachments.extend(self._pending_clipboard)
            self._pending_clipboard = []
        self.renderer.user(display_text, attachments)
        await self.runtime.run_user_turn(
            text,
            event_emitter=self.renderer.event,
            confirm_handler=self._confirm_tool,
            message_content=content,
        )

    async def _handle_interrupt(self) -> None:
        if self._command_task is not None and not self._command_task.done():
            self._command_task.cancel()
            return
        if self._active_task is not None and not self._active_task.done():
            self._active_task.cancel()
            if self.queue:
                keep = await self._ask_yes_no(self._t(
                    f"保留 {len(self.queue)} 条排队消息并继续？",
                    f"Keep and continue {len(self.queue)} queued messages?",
                ), default=False)
                if not keep:
                    self.queue.clear()
            return
        now = time.monotonic()
        if now - self._last_idle_interrupt <= 1.5:
            self._stop = True
            return
        self._last_idle_interrupt = now
        self.renderer.info(self._t("再次按 Ctrl+C 退出", "Press Ctrl+C again to exit"))

    async def _handle_command(self, raw: str) -> bool:
        parts = raw.split()
        name = parts[0].lower()
        args = parts[1:]
        if name not in COMMAND_MAP:
            self.renderer.warning(self._t(f"未知命令：{name}", f"Unknown command: {name}"))
            return False
        busy = self._active_task is not None and not self._active_task.done()
        safe_while_busy = {
            "/help", "/status", "/mode", "/permissions", "/verbose", "/context",
            "/inspector",
        }
        safe_while_busy.add("/usage")
        if busy and name not in safe_while_busy:
            self.renderer.warning(self._t("请先等待或取消当前轮。", "Wait for or cancel the active turn first."))
            return False
        if name == "/exit":
            return True
        if name == "/help":
            for command in COMMANDS:
                self.console.print(f"[cyan]{command.name:<14}[/cyan] {command.description_zh}")
        elif name == "/init":
            path, created = initialize_project_instructions(self.runtime.cwd)
            if created:
                self.renderer.info(self._t(f"已创建 {path}", f"Created {path}"))
            else:
                self.renderer.info(self._t(f"已存在：{path}", f"Already exists: {path}"))
        elif name == "/release-notes":
            self.renderer.show_release_notes()
        elif name == "/status":
            self.renderer.info(self._status_text())
        elif name == "/clear":
            await self._clear_conversation()
        elif name in ("/coding", "/plan", "/design", "/chat"):
            self.runtime.state.mode = name[1:]
            self.renderer.info(self._t(f"模式：{name[1:]}", f"Mode: {name[1:]}"))
        elif name == "/mode":
            self.renderer.info(f"mode: {self.runtime.state.mode}")
        elif name == "/model":
            if args:
                self.runtime.llm.model = " ".join(args)
            self.renderer.info(f"model: {self.runtime.llm.model}")
        elif name == "/effort":
            if args:
                effort = args[0].lower()
                if effort not in ("low", "medium", "high"):
                    self.renderer.warning("usage: /effort low|medium|high")
                    return False
                self.runtime.llm.reasoning_effort = effort
                self.runtime.llm.reasoning_effort_supported = None
            support = self.runtime.llm.reasoning_effort_supported
            effective = "unsupported" if support is False else (self.runtime.llm.reasoning_effort or "default")
            self.renderer.info(f"effort: {effective}")
        elif name == "/permissions":
            if args:
                if args[0] not in _PERMISSION_MODES:
                    self.renderer.warning("usage: /permissions default|auto-edit|bypass-prompts")
                    return False
                self.permission_mode = args[0]
            else:
                self._cycle_permissions()
            self.renderer.info(f"permissions: {self.permission_mode}")
        elif name == "/verbose":
            self.verbose = not self.verbose if not args else args[0].lower() in ("1", "on", "true", "yes")
            self.renderer.verbose = self.verbose
            self.renderer.info(f"verbose: {'on' if self.verbose else 'off'}")
        elif name == "/context":
            used = self._context_tokens()
            conversation = self._context_tokens(include_system=False)
            system = max(0, used - conversation)
            total = self.runtime.state.context_config.context_window
            self.renderer.info(
                f"context: {used:,} / {total:,} ({used / total:.1%}); "
                f"conversation={conversation:,}, system={system:,}"
            )
        elif name == "/usage":
            self.renderer.info(self._usage_text())
        elif name == "/inspector":
            self.renderer.info(self._inspector_text())
        elif name == "/compact":
            from cc_harness.context import maybe_compact
            tools = await self.runtime.mcp.list_tools()
            stats = await maybe_compact(
                self.runtime.state.messages,
                tools,
                self.runtime.state.token_counter,
                self.runtime.state.context_config,
                self.runtime.llm,
            )
            self.renderer.info(f"compaction tier: {int(stats.tier)}")
            await self.runtime.save()
        elif name == "/tools":
            tools = await self.runtime.mcp.list_tools()
            names = [tool.get("function", {}).get("name", "") for tool in tools]
            names.append("run_command")
            self.console.print("\n".join(f"• {item}" for item in names), markup=False)
        elif name == "/mcp":
            tools = await self.runtime.mcp.list_tools()
            self.renderer.info(f"MCP tools: {len(tools)}")
        elif name == "/branch":
            await self._branch_session(args)
        elif name == "/rename":
            await self._rename_session(args)
        elif name == "/rewind":
            await self._rewind_picker()
        elif name == "/focus":
            self._show_focus()
        elif name == "/diff":
            self.renderer.info(await self._session_diff())
        elif name in ("/tasks", "/agents"):
            await self._show_tasks(agents_only=name == "/agents")
        elif name == "/tui":
            self._select_renderer(args)
        elif name == "/resume":
            await self._resume_picker()
        return False

    async def _clear_conversation(self, *, announce: bool = True) -> None:
        """Start a clean conversation without carrying stale usage forward.

        The system message is intentionally retained: it is part of the
        runtime contract and is sent on every provider request.  Session API
        counters, checkpoints, queued input, and the visible title are
        conversation-scoped and must be reset together, otherwise ``/clear``
        appears to work while the toolbar still reports the previous turn.
        """
        self.runtime.state.messages = [
            message for message in self.runtime.state.messages
            if message.get("role") == "system"
        ]
        self.runtime.state.session_stats = SessionTokenStats()
        reset_fields = (
            ("last_turn_text", ""),
            ("todo_hints", []),
            ("turn_counter", 0),
            ("decomposition_rejected", False),
            ("last_decomp_todo_ids", []),
            ("last_decomp_summary", None),
            ("subagent_cancelled", []),
        )
        for attr_name, value in reset_fields:
            if hasattr(self.runtime.state, attr_name):
                setattr(self.runtime.state, attr_name, value)
        self._checkpoints.clear()
        self.queue.clear()
        self._pending_clipboard.clear()
        self._stashed_draft = ""
        self._session_name = ""
        await self.runtime.save()
        # Fullscreen mode keeps a separate renderer event projection.  Clear
        # it when the store exposes the optional event API, while retaining
        # the immutable message history used for audit/recovery.
        save_events = getattr(self.runtime.session_store, "save_events", None)
        if save_events is not None:
            await save_events(self.runtime.state.session_id, [])
        if announce:
            system_tokens = self._context_tokens(include_system=True)
            self.renderer.info(self._t(
                f"对话上下文已清空；已重置本轮 API 统计。系统指令仍占用 {system_tokens:,} tokens。",
                f"Conversation context cleared; API usage reset. System instructions use {system_tokens:,} tokens.",
            ))

    async def _branch_session(self, args: list[str]) -> None:
        """Fork the current message projection into a new saved session."""
        store = self.runtime.session_store
        if store is None:
            self.renderer.warning(self._t("当前运行不支持会话分支。", "Session branching is unavailable."))
            return
        title = " ".join(args).strip()
        source_id = self.runtime.state.session_id
        await self.runtime.save()
        session_id = uuid.uuid4().hex
        messages = copy.deepcopy(self.runtime.state.messages)
        await store.save(session_id, messages, mode=self.runtime.state.mode)
        load_events = getattr(store, "load_events", None)
        save_events = getattr(store, "save_events", None)
        if load_events is not None and save_events is not None:
            await save_events(session_id, await load_events(source_id))
        if title:
            await store.rename(session_id, title)
        self.runtime.state.session_id = session_id
        self.runtime.state.session_stats = SessionTokenStats()
        self._session_name = " ".join(title.split())[:80] if title else f"branch-{session_id[:8]}"
        self.attachments.session_dir = store.attachments_root / session_id
        self.renderer.info(self._t(
            f"已创建会话分支：{source_id[:8]} → {session_id[:8]}",
            f"Branched session: {source_id[:8]} → {session_id[:8]}",
        ))

    async def _rename_session(self, args: list[str]) -> None:
        title = " ".join(args).strip()
        if not title:
            self.renderer.warning(self._t("用法：/rename <名称>", "Usage: /rename <name>"))
            return
        store = self.runtime.session_store
        if store is None:
            self.renderer.warning(self._t("当前运行不支持会话重命名。", "Session renaming is unavailable."))
            return
        await store.rename(self.runtime.state.session_id, title)
        self._session_name = " ".join(title.split())[:80]
        self.renderer.info(self._t(f"会话已重命名：{self._session_name}", f"Session renamed: {self._session_name}"))

    def _show_focus(self) -> None:
        messages = [
            message for message in self.runtime.state.messages
            if message.get("role") != "system"
        ]
        if len(messages) > 2:
            messages = messages[-2:]
        if not messages:
            self.renderer.info(self._t("当前回合暂无内容。", "No current-turn content."))
            return
        self.renderer.show_transcript(messages)

    async def _session_diff(self) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                "git", "-C", str(self.runtime.cwd), "diff", "--no-ext-diff", "--",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
        except OSError as exc:
            return self._t(f"无法读取差异：{exc}", f"Diff unavailable: {exc}")
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            return self._t(f"无法读取差异：{detail}", f"Diff unavailable: {detail}")
        value = stdout.decode("utf-8", errors="replace").strip()
        if not value:
            return self._t("本次会话没有已跟踪文件差异。", "No tracked file changes this session.")
        limit = 16_000
        return value if len(value) <= limit else value[:limit] + "\n… 差异已截断"

    async def _show_tasks(self, *, agents_only: bool) -> None:
        service = getattr(self.runtime.state, "todo_service", None)
        tasks = []
        if service is not None and hasattr(service, "list"):
            try:
                tasks = list(await service.list(include_done=True))
            except Exception as exc:  # noqa: BLE001 - status command must not abort the UI
                self.renderer.warning(self._t(f"任务读取失败：{exc}", f"Task read failed: {exc}"))
                return
        if agents_only:
            tasks = [
                task for task in tasks
                if getattr(task, "parent_task", None) is not None
                or getattr(task, "assigned_to", None)
            ]
        title = "代理任务" if agents_only else "任务列表"
        if not tasks:
            self.renderer.info(f"{title}：无")
            return
        icons = {"done": "✓", "in_progress": "●", "pending": "○", "blocked": "!", "cancelled": "×"}
        lines = [title]
        for task in tasks[:30]:
            status = str(getattr(task, "status", "unknown"))
            task_id = str(getattr(task, "id", "?"))
            task_title = str(getattr(task, "title", "Untitled"))
            assignee = getattr(task, "assigned_to", None)
            suffix = f" · {assignee}" if assignee else ""
            lines.append(f"{icons.get(status, '·')} {task_title} [{task_id}] · {status}{suffix}")
        self.console.print("\n".join(lines), markup=False)

    def _select_renderer(self, args: list[str]) -> None:
        value = args[0].lower() if args else self.terminal_settings.tui
        if value not in {"default", "fullscreen"}:
            self.renderer.warning(self._t("用法：/tui default|fullscreen", "Usage: /tui default|fullscreen"))
            return
        save_project_terminal_setting(self.runtime.cwd, "tui", value)
        self.terminal_settings = load_terminal_settings(self.runtime.cwd)
        self.renderer.info(self._t(
            f"终端界面：{value}（下次启动生效）",
            f"Terminal renderer: {value} (effective next launch)",
        ))

    async def _resume_picker(self) -> None:
        records = await self.runtime.session_store.list_recent(20)
        if not records:
            self.renderer.info(self._t("没有历史会话。", "No saved sessions."))
            return
        for index, record in enumerate(records, 1):
            self.console.print(f"{index:>2}. [cyan]{record.title}[/cyan] [dim]{record.session_id}[/dim]")
        answer = await self._ask(self._t("选择编号（空白取消）：", "Select number (blank cancels): "))
        if not answer.strip():
            return
        try:
            record = records[int(answer) - 1]
        except (ValueError, IndexError):
            self.renderer.warning(self._t("无效选择。", "Invalid selection."))
            return
        await self.runtime.save()
        self.runtime.state.session_id = record.session_id
        self.runtime.state.mode = record.mode
        self.runtime.state.messages = await self.runtime.session_store.load(record.session_id)
        self.runtime.state.session_stats = SessionTokenStats()
        self._checkpoints.clear()
        self.queue.clear()
        self._session_name = record.title if record.title != "Untitled session" else ""
        self.attachments.session_dir = self.runtime.session_store.attachments_root / record.session_id
        self.renderer.info(self._t(f"已恢复：{record.title}", f"Resumed: {record.title}"))

    async def _confirm_tool(self, tool: str, args: dict, reason: str) -> str:
        if self.permission_mode == "bypass-prompts":
            return "yes"
        if self.permission_mode == "auto-edit" and self._looks_like_project_edit(tool, args):
            return "yes"
        question = self._t(
            f"允许执行 {tool}？{reason} (yes/always/no): ",
            f"Allow {tool}? {reason} (yes/always/no): ",
        )
        answer = (await self._ask(question)).strip().lower()
        if answer in ("y", "yes"):
            return "yes"
        if answer in ("a", "always"):
            return "always"
        return "no"

    async def _confirm_outside(self, path: Path) -> bool:
        return await self._ask_yes_no(self._t(
            f"附件位于工作范围外，允许读取 {path}？",
            f"Attachment is outside the workspace. Read {path}?",
        ), default=False)

    async def _ask_yes_no(self, question: str, *, default: bool) -> bool:
        suffix = " [Y/n]: " if default else " [y/N]: "
        answer = (await self._ask(question + suffix)).strip().lower()
        return default if not answer else answer in ("y", "yes")

    async def _ask(self, question: str) -> str:
        return await run_in_terminal(lambda: prompt(question), in_executor=True)

    async def _clipboard(self, buffer=None) -> None:
        try:
            attachment = await self.attachments.from_clipboard()
        except Exception as exc:  # noqa: BLE001 - clipboard backends raise platform-specific errors
            self.renderer.warning(str(exc))
            return
        self._pending_clipboard.append(attachment)
        if buffer is not None:
            buffer.insert_text(f"[Image #{len(self._pending_clipboard)}]")
        self.renderer.info(self._t(
            f"已附加剪贴板图片：{attachment.display_name}",
            f"Clipboard image attached: {attachment.display_name}",
        ))

    def _build_key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("enter")
        def submit(event) -> None:
            if event.current_buffer.text.endswith("\\"):
                event.current_buffer.delete_before_cursor(1)
                event.current_buffer.insert_text("\n")
                return
            event.current_buffer.validate_and_handle()

        @kb.add("c-j")
        def control_newline(event) -> None:
            event.current_buffer.insert_text("\n")

        @kb.add("c-c")
        def cancel(event) -> None:
            if self._active_task is not None and not self._active_task.done():
                self._active_task.cancel()
            else:
                event.current_buffer.reset()

        @kb.add("escape", "enter")
        def newline(event) -> None:
            event.current_buffer.insert_text("\n")

        @kb.add("s-tab")
        def permission(event) -> None:
            del event
            self._cycle_permissions()

        @kb.add("escape", "m")
        def permission_fallback(event) -> None:
            del event
            self._cycle_permissions()

        @kb.add("c-o")
        def transcript(event) -> None:
            event.app.create_background_task(self._show_transcript())

        @kb.add("c-s")
        def stash(event) -> None:
            buffer = event.current_buffer
            if buffer.text:
                self._stashed_draft = buffer.text
                buffer.text = ""
            elif self._stashed_draft:
                buffer.text = self._stashed_draft
                buffer.cursor_position = len(buffer.text)
                self._stashed_draft = ""

        @kb.add("c-l")
        def redraw(event) -> None:
            event.app.renderer.clear()
            event.app.invalidate()

        @kb.add("escape", "p")
        def model_picker(event) -> None:
            event.app.create_background_task(self._model_picker())

        @kb.add("escape", "t")
        def effort(event) -> None:
            del event
            self._cycle_effort()

        @kb.add("escape", "v")
        def clipboard(event) -> None:
            event.app.create_background_task(self._clipboard(event.current_buffer))

        @kb.add(Keys.BracketedPaste)
        def bracketed_paste(event) -> None:
            data = event.data.replace("\r\n", "\n").replace("\r", "\n")
            if len(data) > 800 or data.count("\n") > 2:
                self._paste_counter += 1
                lines = data.count("\n") + 1
                marker = f"[Pasted text #{self._paste_counter} +{lines} lines]"
                self._pastes[marker] = data
                event.current_buffer.insert_text(marker)
            else:
                event.current_buffer.insert_text(data)

        @kb.add("escape")
        def escape(event) -> None:
            self._handle_escape(event.current_buffer, event.app)

        return kb

    def _cycle_permissions(self) -> None:
        index = (_PERMISSION_MODES.index(self.permission_mode) + 1) % len(_PERMISSION_MODES)
        self.permission_mode = _PERMISSION_MODES[index]

    def _bottom_toolbar(self):
        width = max(40, shutil.get_terminal_size((self.console.size.width, 24)).columns)
        fragments: list[tuple[str, str]] = [("class:status.border", "─" * width + "\n")]
        fragments.extend(self._identity_status(width))
        fragments.extend(self._context_status(width))
        fragments.extend(self._usage_status(width))
        fragments.extend(self._permission_status(width))
        return FormattedText(fragments)

    def _identity_status(self, width: int) -> list[tuple[str, str]]:
        model = self.runtime.llm.model if self.runtime.llm else "?"
        context_window = self.runtime.state.context_config.context_window
        model_badge = f"[{model}[{self._compact_number(context_window)}]]"
        left: list[tuple[str, str]] = [("class:status.model", model_badge)]
        project = self._project_name()
        if width >= 64 and self.terminal_settings.show_project and project:
            left.extend([
                ("class:status.separator", " │ "),
                ("class:status.project", project[:24]),
            ])
        if width >= 64 and self.terminal_settings.show_git:
            branch = self.renderer.git_branch(self.runtime.cwd)
            if branch:
                left.extend([
                    ("class:status.git.label", " git:("),
                    ("class:status.git", branch[:30]),
                    ("class:status.git.label", ")"),
                ])
        session_name = getattr(self, "_session_name", "")
        if width >= 92 and self.terminal_settings.show_session_name and session_name:
            left.extend([
                ("class:status.separator", " │ "),
                ("class:status.session", session_name[:32]),
            ])
        if width >= 110 and self.terminal_settings.show_duration:
            left.extend([
                ("class:status.separator", " │ "),
                ("class:status.duration", f"⏱  {self._duration_label()}"),
            ])
        if width >= 128 and self.terminal_settings.custom_line:
            left.extend([
                ("class:status.separator", " │ "),
                ("class:status.custom", self.terminal_settings.custom_line[:20]),
            ])
        right: list[tuple[str, str]] = []
        if width >= 72:
            supported = getattr(self.runtime.llm, "reasoning_effort_supported", None)
            effort = (
                "unsupported" if supported is False
                else (getattr(self.runtime.llm, "reasoning_effort", None) or "default")
            )
            right = [
                ("class:status.dim", "● "),
                ("class:status.effort", effort),
                ("class:status.dim", " · /effort" if width >= 92 else ""),
            ]
        command_task = getattr(self, "_command_task", None)
        if command_task is not None and not command_task.done():
            elapsed = max(0.0, time.monotonic() - getattr(self, "_command_started", 0.0))
            left_command = getattr(self, "_command_label", None) or "/命令"
            left_command = f"⟳ 执行中 {left_command} · {elapsed:.1f}s"
            right = []
            left.extend([("class:status.warning", " · " + left_command)])
        return self._aligned_line(left, right, width)

    def _context_status(self, width: int) -> list[tuple[str, str]]:
        used = self._context_tokens()
        conversation = self._context_tokens(include_system=False)
        system = max(0, used - conversation)
        total = max(1, self.runtime.state.context_config.context_window)
        ratio = min(1.0, used / total)
        filled = min(18, round(ratio * 18))
        context_style = (
            "class:status.critical" if ratio >= 0.9
            else "class:status.warning" if ratio >= 0.7
            else "class:status.context"
        )
        left = [
            ("class:status.dim", "Context  "),
            (context_style, "█" * filled),
            ("class:status.context.empty", "░" * (18 - filled)),
            (context_style, f"  {ratio:.0%}"),
        ]
        if system and conversation == 0:
            left.append(("class:status.dim", " · conversation 0% · system baseline"))
        return self._aligned_line(left, [], width)

    def _usage_status(self, width: int) -> list[tuple[str, str]]:
        stats = getattr(self.runtime.state, "session_stats", None)
        if stats is None or not getattr(stats, "turns_with_usage", 0):
            left = [("class:status.dim", "API usage  unavailable")]
            return self._aligned_line(left, [], width)
        prompt = int(getattr(stats, "api_prompt_tokens", 0) or 0)
        completion = int(getattr(stats, "api_completion_tokens", 0) or 0)
        cache = int(getattr(stats, "api_cache_read_prompt_tokens", 0) or 0)
        cache_rate = cache / prompt if prompt else 0.0
        left = [
            ("class:status.dim", "API  "),
            ("class:status.context", f"in {self._compact_number(prompt)}"),
            ("class:status.dim", f" · out {self._compact_number(completion)}"),
            ("class:status.dim", f" · cache {cache_rate:.0%}"),
        ]
        cost = getattr(stats, "api_reported_cost", None)
        currency = getattr(stats, "api_reported_cost_currency", None)
        if cost is None:
            left.append(("class:status.dim", " · cost unavailable"))
        else:
            label = f"{currency + ' ' if currency else ''}{cost:g}"
            left.append(("class:status.dim", f" · cost {label}"))
        return self._aligned_line(left, [], width)

    def _permission_status(self, width: int) -> list[tuple[str, str]]:
        if self.runtime.state.mode == "plan":
            icon, label, style = "⏸", "plan mode on", "class:status.plan"
        else:
            modes = {
                "default": ("⏸", "manual mode on", "class:status.dim"),
                "auto-edit": ("⏵⏵", "accept edits on", "class:status.accept"),
                "bypass-prompts": ("⏵⏵", "bypass permissions on", "class:status.bypass"),
            }
            icon, label, style = modes[self.permission_mode]
        left = [
            (style, f"{icon} {label}"),
            ("class:status.dim", "  (shift+tab to cycle)"),
        ]
        agent_tasks = getattr(self, "_agent_tasks", lambda: [])()
        has_agents = bool(agent_tasks)
        if has_agents and width >= 72:
            active = sum(
                getattr(task, "status", "") in {"pending", "in_progress"}
                for task in agent_tasks
            )
            left.append(("class:status.dim", f" · ← for agents ({active} active)"))
        return self._aligned_line(left, [], width, newline=False)

    @staticmethod
    def _aligned_line(
        left: list[tuple[str, str]],
        right: list[tuple[str, str]],
        width: int,
        *,
        newline: bool = True,
    ) -> list[tuple[str, str]]:
        left_width = sum(max(0, wcswidth(text)) for _, text in left)
        right_width = sum(max(0, wcswidth(text)) for _, text in right)
        available = max(1, width - left_width - right_width)
        result = [*left, ("", " " * available), *right]
        if newline:
            result.append(("", "\n"))
        return result

    @staticmethod
    def _prompt_style() -> Style:
        return Style.from_dict({
            "": "#d0d0d0 bg:ansidefault noreverse",
            "bottom-toolbar": "#d0d0d0 bg:ansidefault noreverse",
            "bottom-toolbar.text": "#d0d0d0 bg:ansidefault noreverse",
            "input.border": "#777777",
            "input.prompt": "#d0d0d0 bold",
            "completion-menu": "bg:#20242b #d6deeb",
            "completion-menu.completion": "bg:#20242b #d6deeb",
            "completion-menu.completion.current": "bg:#3b4252 #ffffff bold",
            # Keep the metadata column on the same dark surface as the
            # command column.  prompt-toolkit renders metadata as a child
            # fragment, so it must set its background explicitly instead of
            # relying on the parent completion-menu style.
            "completion-menu.meta.completion": "bg:#20242b #e5e9f0",
            "completion-menu.meta.completion.current": "bg:#3b4252 #ffffff bold",
            "status.border": "#777777",
            "status.model": "#00d7d7",
            "status.separator": "#888888",
            "status.project": "#ffd75f",
            "status.custom": "#ff8700",
            "status.git.label": "#d75fff",
            "status.git": "#00d7d7",
            "status.session": "#666666",
            "status.duration": "#777777",
            "status.effort": "#aaaaaa",
            "status.dim": "#888888",
            "status.context": "#00af00",
            "status.context.empty": "#164d20",
            "status.warning": "#ffd700",
            "status.critical": "#ff5f5f",
            "status.plan": "#5fafff bold",
            "status.accept": "#00d787 bold",
            "status.bypass": "#ff5f87 bold",
        })

    @staticmethod
    def _prompt_message() -> FormattedText:
        width = max(20, shutil.get_terminal_size(fallback=(80, 24)).columns)
        return FormattedText([
            ("class:input.border", "─" * width + "\n"),
            ("class:input.prompt", "> "),
        ])

    async def _show_transcript(self) -> None:
        def show() -> None:
            self.renderer.show_transcript(self.runtime.state.messages)
            prompt(self._t("按 Enter 返回…", "Press Enter to return…"))

        await run_in_terminal(show, in_executor=True)

    async def _model_picker(self) -> None:
        answer = (await self._ask(self._t("模型：", "Model: "))).strip()
        if answer:
            self.runtime.llm.model = answer
            self.renderer.info(f"model: {answer}")

    def _cycle_effort(self) -> None:
        values = ("low", "medium", "high")
        current = self.runtime.llm.reasoning_effort if self.runtime.llm else None
        next_value = values[(values.index(current) + 1) % len(values)] if current in values else "low"
        self.runtime.llm.reasoning_effort = next_value
        self.runtime.llm.reasoning_effort_supported = None

    def _handle_escape(self, buffer, app) -> None:
        if buffer.complete_state is not None:
            buffer.cancel_completion()
            app.invalidate()
            return
        if self._command_task is not None and not self._command_task.done():
            self._command_task.cancel()
            return
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
            buffer.text = ""
            return
        app.create_background_task(self._rewind_picker())

    async def _rewind_picker(self) -> None:
        if not self._checkpoints:
            self.renderer.info(self._t("还没有可恢复的检查点。", "No checkpoints yet."))
            return
        recent = list(reversed(self._checkpoints[-20:]))
        for index, (label, _messages) in enumerate(recent, 1):
            self.console.print(f"{index:>2}. [cyan]{label or '(empty prompt)'}[/cyan]")
        answer = (await self._ask(self._t("恢复对话到编号（空白取消）：", "Restore conversation to number (blank cancels): "))).strip()
        if not answer:
            return
        try:
            _label, messages = recent[int(answer) - 1]
        except (ValueError, IndexError):
            self.renderer.warning(self._t("无效选择。", "Invalid selection."))
            return
        self.runtime.state.messages = copy.deepcopy(messages)
        await self.runtime.save()
        self.renderer.info(self._t("对话已恢复。", "Conversation restored."))

    def _expand_pastes(self, text: str) -> str:
        for marker, payload in self._pastes.items():
            text = text.replace(marker, payload)
        return text

    def _session_duration(self) -> float:
        try:
            started = datetime.fromisoformat(self.runtime.state.started_at)
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            return max(0.0, (datetime.now(UTC) - started).total_seconds())
        except (TypeError, ValueError):
            return 0.0

    def _duration_label(self) -> str:
        seconds = max(0, int(self._session_duration()))
        hours, seconds = divmod(seconds, 3_600)
        minutes, seconds = divmod(seconds, 60)
        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m"
        return f"{seconds}s"

    def _project_name(self) -> str:
        manifest = getattr(self.runtime.state, "manifest", None)
        name = getattr(manifest, "name", "")
        return str(name or self.runtime.cwd.name or self.runtime.cwd)

    def _initial_session_name(self) -> str:
        for message in self.runtime.state.messages:
            if message.get("role") != "user":
                continue
            content = message.get("content", "")
            if isinstance(content, list):
                content = next(
                    (part.get("text", "") for part in content
                     if isinstance(part, dict) and part.get("type") == "text"),
                    "",
                )
            title = self._compact_title(str(content))
            if title:
                return title
        return ""

    @staticmethod
    def _compact_title(text: str) -> str:
        compact = " ".join(text.split())
        return compact[:32]

    @staticmethod
    def _compact_number(value: int) -> str:
        if value >= 1_000_000:
            return f"{value / 1_000_000:g}m"
        if value >= 1_000:
            return f"{value / 1_000:g}k"
        return str(value)

    def _status_text(self) -> str:
        return (
            f"model={self.runtime.llm.model} mode={self.runtime.state.mode} "
            f"permissions={self.permission_mode} cwd={self.runtime.cwd} "
            f"session={self.runtime.state.session_id} queued={len(self.queue)}"
            f" usage={self._usage_text()}"
        )

    def _usage_text(self) -> str:
        stats = getattr(self.runtime.state, "session_stats", None)
        if stats is None or not getattr(stats, "turns_with_usage", 0):
            return "API usage unavailable"
        prompt = int(getattr(stats, "api_prompt_tokens", 0) or 0)
        completion = int(getattr(stats, "api_completion_tokens", 0) or 0)
        cache = int(getattr(stats, "api_cache_read_prompt_tokens", 0) or 0)
        result = (
            f"API input={prompt:,} output={completion:,} "
            f"cache_hit={cache:,}/{prompt:,}"
        )
        cost = getattr(stats, "api_reported_cost", None)
        if cost is not None:
            currency = getattr(stats, "api_reported_cost_currency", None) or ""
            result += f" cost={currency + ' ' if currency else ''}{cost:g}"
        else:
            result += " cost=unavailable"
        return result

    def _inspector_text(self) -> str:
        """Print safe run metadata; never print effective prompt content."""
        metadata = getattr(self.runtime, "prompt_metadata", {}) or {}
        stats = getattr(self.runtime.state, "session_stats", None)
        return (
            "Inspector: "
            f"model={getattr(self.runtime.llm, 'model', 'unknown')} "
            f"prompt_version={metadata.get('version', 'unknown')} "
            f"prompt_digest={str(metadata.get('digest', 'unknown'))[:16]}… "
            f"cache_epoch={metadata.get('cache_epoch', 'unknown')} "
            f"rules={metadata.get('rules_version', 'unknown')} "
            f"context={self._context_tokens():,} "
            f"api_tokens={int(getattr(stats, 'api_total_tokens', 0) or 0):,} "
            "prompt_text=hidden"
        )

    def _context_tokens(self, *, include_system: bool = True) -> int:
        total = 0
        for message in self.runtime.state.messages:
            if not include_system and message.get("role") == "system":
                continue
            content = message.get("content", "")
            if isinstance(content, str):
                total += self.runtime.state.token_counter.count_text(content)
            elif isinstance(content, list):
                total += sum(self.runtime.state.token_counter.count_text(str(p.get("text", "")))
                             for p in content if isinstance(p, dict))
        return total

    def _looks_like_project_edit(self, tool: str, args: dict) -> bool:
        lower = tool.lower()
        if not any(word in lower for word in ("write", "edit", "create", "move", "rename")):
            return False
        value = next((args.get(k) for k in ("path", "file_path", "filePath", "filename")
                      if isinstance(args.get(k), str)), None)
        if value is None:
            return False
        path = Path(value)
        if not path.is_absolute():
            path = self.runtime.cwd / path
        resolved = path.resolve()
        return any(resolved.is_relative_to(root)
                   for root in (self.runtime.cwd, *self.runtime.additional_dirs))

    def _t(self, zh: str, en: str) -> str:
        return zh if self.lang.startswith("zh") else en
