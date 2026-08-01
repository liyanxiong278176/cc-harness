"""Render layer abstraction: events + driver protocol.

TUI / REPL / Test 共用同一套事件,driver 决定具体输出。

All event payloads are frozen dataclasses so a single event can be safely
shared across drivers (e.g. TUI + TestRecordingDriver) without defensive
copies. ``RenderEvent`` is a Union of all concrete variants for static
type-checking; consumers pattern-match on it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


# --- RenderEvent 子类(每个 frozen dataclass) ---


@dataclass(frozen=True)
class ThinkingChunk:
    """A streaming chunk of LLM "thinking" output."""

    delta: str


@dataclass(frozen=True)
class ThinkingDone:
    """Final complete thinking text after streaming completes."""

    text: str


@dataclass(frozen=True)
class ToolCallStart:
    """A tool invocation begins. Drivers typically open a panel/spinner."""

    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolCallEnd:
    """A tool invocation finishes, with result or error."""

    name: str
    result: str
    error: bool
    duration_ms: int


@dataclass(frozen=True)
class FinalText:
    """The model's final assistant turn text (post-thinking, post-tools)."""

    text: str


@dataclass(frozen=True)
class Usage:
    """Token accounting snapshot. Drivers use this to refresh status bars."""

    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int


@dataclass(frozen=True)
class TodoUpdate:
    """Replaces the current todo list (full snapshot, not a diff).

    items shape: ``[{"id": str, "title": str, "status": str}, ...]``
    """

    items: list[dict[str, str]]


@dataclass(frozen=True)
class ModeChanged:
    """Session mode changed. ``mode`` in ``coding | plan | design``."""

    mode: str


@dataclass(frozen=True)
class PermissionModeChanged:
    """Permission mode changed. ``mode`` in ``default | auto``."""

    mode: str


# --- RenderEvent 联合类型 ---


RenderEvent = (
    ThinkingChunk
    | ThinkingDone
    | ToolCallStart
    | ToolCallEnd
    | FinalText
    | Usage
    | TodoUpdate
    | ModeChanged
    | PermissionModeChanged
)


# --- RenderDriver Protocol ---


@runtime_checkable
class RenderDriver(Protocol):
    """Driver that consumes RenderEvents and decides concrete output.

    Implementations: TextualTuiDriver, ReplDriver, TestRecordingDriver, etc.
    All methods are fire-and-forget; drivers must not raise.
    """

    def write(self, text: str) -> None: ...
    def write_chunk(self, token: str) -> None: ...
    def write_tool_call(self, name: str, args: dict[str, Any]) -> None: ...
    def write_tool_result(self, result: str, error: bool) -> None: ...
    def write_todo(self, items: list[dict[str, str]]) -> None: ...
    def write_status(self, **fields: Any) -> None: ...
    def refresh_token(self, stats: Any) -> None: ...
