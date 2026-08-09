"""Renderer-agnostic terminal transcript state.

The terminal UI never treats stdout as the conversation database.  Agent events
are reduced into this model first; fullscreen, classic, detailed and focus
views are projections of the same state.  Keeping the reducer deterministic is
what prevents duplicate streamed answers and makes resumed sessions render the
same way as live ones.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable


_COMPLETION_VERBS = ("Sautéed", "Brewed", "Crafted", "Simmered", "Whisked")


@dataclass
class AssistantBlock:
    text: str


@dataclass
class ToolActivity:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    summary: str = "Running…"
    output: str = ""
    status: str = "running"
    is_error: bool = False
    duration_ms: int | None = None


TranscriptItem = AssistantBlock | ToolActivity


@dataclass
class TurnTranscript:
    prompt: str
    attachments: list[str]
    started_at: float
    items: list[TranscriptItem] = field(default_factory=list)
    stream_text: str = ""
    first_visible_at: float | None = None
    thought_seconds: int | None = None
    status: str = "active"
    duration_seconds: float | None = None
    completion_verb: str = ""
    error: str = ""
    retry_attempt: int | None = None
    retry_max: int | None = None
    retry_at: float | None = None
    retry_reason: str = ""

    @property
    def completed(self) -> bool:
        return self.status != "active"


class TranscriptState:
    """Deterministic reducer for the visible lifecycle of terminal turns."""

    def __init__(
        self,
        session_id: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.session_id = session_id
        self.clock = clock
        self.turns: list[TurnTranscript] = []
        self.events: list[dict[str, Any]] = []
        self.pending_notices: list[tuple[str, str]] = []

    @property
    def active_turn(self) -> TurnTranscript | None:
        if self.turns and self.turns[-1].status == "active":
            return self.turns[-1]
        return None

    def start_turn(
        self,
        prompt: str,
        attachments: Iterable[str] = (),
        *,
        ts: float | None = None,
        record: bool = True,
    ) -> TurnTranscript:
        if self.active_turn is not None:
            raise RuntimeError("cannot start a second active transcript turn")
        event = {
            "type": "user_committed",
            "text": prompt,
            "attachments": list(attachments),
            "ts": self.clock() if ts is None else ts,
        }
        if record:
            self.events.append(event)
        turn = TurnTranscript(
            prompt=prompt,
            attachments=list(attachments),
            started_at=float(event["ts"]),
        )
        self.turns.append(turn)
        return turn

    def apply(self, event: dict[str, Any], *, record: bool = True) -> None:
        """Apply one event emitted by the agent/runtime.

        Unknown events remain persisted for forward compatibility but do not
        affect the normal transcript projection.
        """
        normalized = dict(event)
        normalized.setdefault("ts", self.clock())
        kind = str(normalized.get("type", ""))
        if kind == "user_committed":
            self.start_turn(
                str(normalized.get("text", "")),
                normalized.get("attachments") or (),
                ts=float(normalized["ts"]),
                record=record,
            )
            return
        if record:
            self.events.append(normalized)
        turn = self.active_turn
        if turn is None:
            return

        if kind == "content_delta":
            text = str(normalized.get("text", ""))
            if text:
                self._mark_visible(turn, float(normalized["ts"]))
                turn.stream_text += text
        elif kind == "action":
            self._mark_visible(turn, float(normalized["ts"]))
            self._commit_stream(turn)
            args = normalized.get("args")
            turn.items.append(ToolActivity(
                name=str(normalized.get("name", "Tool")),
                args=dict(args) if isinstance(args, dict) else {},
            ))
        elif kind == "observation":
            self._mark_visible(turn, float(normalized["ts"]))
            tool = next(
                (item for item in reversed(turn.items)
                 if isinstance(item, ToolActivity) and item.status == "running"),
                None,
            )
            if tool is not None:
                tool.output = str(normalized.get("text", ""))
                tool.summary = self._one_line(tool.output) or "Done"
                tool.is_error = bool(normalized.get("is_error"))
                tool.status = "failed" if tool.is_error else "completed"
                duration = normalized.get("duration_ms")
                tool.duration_ms = int(duration) if isinstance(duration, (int, float)) else None
        elif kind == "tool_output_delta":
            self._mark_visible(turn, float(normalized["ts"]))
            tool = next(
                (item for item in reversed(turn.items)
                 if isinstance(item, ToolActivity) and item.status == "running"),
                None,
            )
            if tool is not None:
                tool.output += str(normalized.get("text", ""))
                tool.summary = self._one_line(tool.output.splitlines()[-1]) if tool.output else "Running…"
        elif kind == "subagent_progress":
            self._mark_visible(turn, float(normalized["ts"]))
            task_id = str(normalized.get("task_id", "?"))
            status = str(normalized.get("status", "unknown"))
            detail = str(normalized.get("detail", ""))
            tool = next(
                (
                    item for item in reversed(turn.items)
                    if isinstance(item, ToolActivity)
                    and item.name == f"Agent {task_id}"
                ),
                None,
            )
            if tool is None:
                self._commit_stream(turn)
                tool = ToolActivity(name=f"Agent {task_id}")
                turn.items.append(tool)
            tool.summary = status + (f" · {detail}" if detail else "")
            tool.status = (
                "completed" if status == "done"
                else "failed" if status in {"failed", "timeout", "blocked"}
                else "running"
            )
            tool.is_error = tool.status == "failed"
        elif kind == "retrying":
            turn.retry_attempt = self._optional_int(normalized.get("attempt"))
            turn.retry_max = self._optional_int(normalized.get("max_attempts"))
            delay = normalized.get("delay_seconds")
            turn.retry_at = (
                float(normalized["ts"]) + max(0.0, float(delay))
                if isinstance(delay, (int, float)) else None
            )
            turn.retry_reason = str(normalized.get("reason", ""))
        elif kind == "result":
            self._finish_success(turn, str(normalized.get("text", "")), float(normalized["ts"]))
        elif kind in ("interrupted", "cancelled"):
            self._finish(turn, "interrupted", float(normalized["ts"]), "")
        elif kind in ("failed", "error"):
            self._finish(
                turn,
                "failed",
                float(normalized["ts"]),
                str(normalized.get("message") or normalized.get("text") or "Unknown error"),
            )

    def interrupt(self, *, ts: float | None = None) -> None:
        self.apply({"type": "interrupted", "ts": self.clock() if ts is None else ts})

    def fail(self, message: str, *, ts: float | None = None) -> None:
        self.apply({
            "type": "failed",
            "message": message,
            "ts": self.clock() if ts is None else ts,
        })

    def add_notice(self, text: str, style: str = "info") -> None:
        self.pending_notices.append((style, text))
        self.pending_notices = self.pending_notices[-20:]

    def thinking_seconds(self, turn: TurnTranscript, *, now: float | None = None) -> float:
        end = turn.first_visible_at
        if end is None:
            end = self.clock() if now is None else now
        return max(0.0, end - turn.started_at)

    def retry_seconds(self, turn: TurnTranscript, *, now: float | None = None) -> int | None:
        if turn.retry_at is None:
            return None
        current = self.clock() if now is None else now
        return max(0, int(turn.retry_at - current + 0.999))

    def replay(self, events: Iterable[dict[str, Any]]) -> None:
        self.turns.clear()
        self.events.clear()
        for event in events:
            self.apply(event, record=True)

    def to_jsonable(self) -> list[dict[str, Any]]:
        return [dict(event) for event in self.events]

    @classmethod
    def from_messages(
        cls,
        session_id: str,
        messages: Iterable[dict[str, Any]],
    ) -> "TranscriptState":
        """Build a minimal transcript for sessions saved before event logging."""
        state = cls(session_id)
        synthetic_ts = time.time()
        open_turn = False
        for message in messages:
            role = message.get("role")
            if role == "user":
                if state.active_turn is not None:
                    state.apply({"type": "result", "text": "", "ts": synthetic_ts})
                state.start_turn(cls._message_text(message.get("content")), ts=synthetic_ts)
                open_turn = True
                synthetic_ts += 0.001
            elif role == "assistant" and open_turn:
                state.apply({
                    "type": "result",
                    "text": cls._message_text(message.get("content")),
                    "ts": synthetic_ts,
                })
                open_turn = False
                synthetic_ts += 0.001
        if state.active_turn is not None:
            state.interrupt(ts=synthetic_ts)
        return state

    def snapshot(self) -> dict[str, Any]:
        """Debug/test representation with dataclass values converted to dicts."""
        return {
            "session_id": self.session_id,
            "turns": [asdict(turn) for turn in self.turns],
            "events": json.loads(json.dumps(self.events, ensure_ascii=False)),
        }

    def _finish_success(self, turn: TurnTranscript, result: str, ts: float) -> None:
        self._mark_visible(turn, ts)
        streamed = turn.stream_text
        if streamed:
            if result.strip() and result.strip() != streamed.strip():
                self._commit_stream(turn)
                turn.items.append(AssistantBlock(result))
            else:
                self._commit_stream(turn)
        elif result:
            turn.items.append(AssistantBlock(result))
        self._finish(turn, "success", ts, "")
        seed = f"{self.session_id}:{len(self.turns) - 1}".encode("utf-8")
        turn.completion_verb = _COMPLETION_VERBS[
            int.from_bytes(hashlib.sha256(seed).digest()[:4], "big") % len(_COMPLETION_VERBS)
        ]

    def _finish(self, turn: TurnTranscript, status: str, ts: float, error: str) -> None:
        self._commit_stream(turn)
        turn.status = status
        turn.duration_seconds = max(0.0, ts - turn.started_at)
        turn.error = error
        turn.retry_at = None

    def _mark_visible(self, turn: TurnTranscript, ts: float) -> None:
        if turn.first_visible_at is not None:
            return
        turn.first_visible_at = ts
        elapsed = max(0.0, ts - turn.started_at)
        turn.thought_seconds = int(elapsed) if elapsed >= 1.0 else None

    @staticmethod
    def _commit_stream(turn: TurnTranscript) -> None:
        if turn.stream_text:
            turn.items.append(AssistantBlock(turn.stream_text))
            turn.stream_text = ""

    @staticmethod
    def _one_line(text: str, limit: int = 180) -> str:
        compact = " ".join(text.split())
        return compact if len(compact) <= limit else compact[: limit - 1] + "…"

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        return int(value) if isinstance(value, (int, float)) else None

    @staticmethod
    def _message_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content)
        chunks: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                chunks.append(str(part.get("text", "")))
            elif part.get("type") == "image_url":
                chunks.append("[Image]")
        return "\n".join(chunks)
