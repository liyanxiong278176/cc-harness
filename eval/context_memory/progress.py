"""Live, restart-safe progress reporting for context-memory evaluations."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from eval.cc_only.storage import atomic_json, atomic_text, read_json, utc_now

PROGRESS_SCHEMA = "eval.context-memory-progress.v1"
_USAGE_FIELDS = (
    "wall_time_ms",
    "model_calls",
    "tool_calls",
    "input_tokens",
    "uncached_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
    "cost_microusd",
)
_TERMINAL_STATUSES = {"complete", "invalid", "unsupported"}


class ContextMemoryProgress:
    """Persist and print progress without becoming part of the eval outcome.

    Every reporting operation is best effort. A console encoding problem or a full
    progress disk cannot invalidate a benchmark run, so failures are deliberately
    swallowed by this class.
    """

    def __init__(
        self,
        output_root: Path,
        *,
        benchmark: str,
        profile: str,
        model: str,
        total_tasks: int,
        state: Mapping[str, Any] | None = None,
        emit: Callable[[str], None] = print,
    ) -> None:
        self.output_root = output_root.resolve()
        self.progress_path = self.output_root / "progress.json"
        self.markdown_path = self.output_root / "progress.md"
        self.log_path = self.output_root / "progress.log"
        self.benchmark = benchmark
        self.profile = profile
        self.model = model
        self.total_tasks = int(total_tasks)
        self.emit = emit
        self._session_started = time.monotonic()
        self._started_at = utc_now()
        self._prior_elapsed_seconds = 0.0
        self._prior_completed_tasks = 0
        self._usage = {name: 0 for name in _USAGE_FIELDS}
        self._completed_task_ids: set[str] = set()
        self._current: dict[str, Any] | None = None
        self._load_existing()
        if state is not None:
            self._completed_task_ids.update(_completed_task_ids(state))
        self._payload: dict[str, Any] = {
            "schema_version": PROGRESS_SCHEMA,
            "benchmark": self.benchmark,
            "profile": self.profile,
            "model": self.model,
            "total_tasks": self.total_tasks,
            "completed_tasks": self._completed_tasks_count(),
            "status": "starting",
            "started_at": self._started_at,
            "last_updated_at": self._started_at,
            "elapsed_seconds": self._elapsed_seconds(),
            "eta_seconds": None,
            "current": None,
            "last_event": "initialized",
            "usage": dict(self._usage),
            "artifacts": {
                "state": "state.json",
                "progress_json": "progress.json",
                "progress_markdown": "progress.md",
                "progress_log": "progress.log",
            },
        }
        self._write("initialized", emit_line=False)

    def message(self, message: str) -> None:
        """Record an existing runner message and mirror it to the console."""

        self._payload["last_event"] = str(message)
        self._write(str(message), emit_line=True)

    def task_start(
        self,
        sequence: int,
        task_id: str,
        arm: str,
        *,
        resumed: bool = False,
    ) -> None:
        self._current = {
            "sequence": int(sequence),
            "task_id": str(task_id),
            "arm": str(arm),
            "status": "running",
            "resumed": bool(resumed),
            "started_at": utc_now(),
            "event_index": None,
            "event_count": None,
            "question_index": None,
            "question_count": None,
            "phase": None,
            "phase_status": None,
            "phase_started_at": None,
            "last_phase": None,
            "last_phase_elapsed_seconds": None,
            "last_error": None,
        }
        self._payload["status"] = "running"
        self._payload["current"] = self._current
        self._write(f"task start {sequence}/{self.total_tasks} {task_id}.{arm}")

    def task_shape(self, event_count: int, question_count: int) -> None:
        if self._current is None:
            return
        self._current["event_count"] = int(event_count)
        self._current["question_count"] = int(question_count)
        self._write("task shape updated", emit_line=False)

    def item_started(self, kind: str, index: int, total: int, label: str) -> None:
        if self._current is None:
            return
        normalized = "event" if kind == "event" else "question"
        self._current[f"{normalized}_index"] = int(index)
        self._current[f"{normalized}_count"] = int(total)
        self._current[f"{normalized}_label"] = str(label)
        self._current["phase"] = None
        self._current["phase_status"] = None
        self._write(f"{normalized} {index}/{total} started", emit_line=False)

    def item_completed(
        self,
        kind: str,
        index: int,
        total: int,
        *,
        score: float | None = None,
    ) -> None:
        if self._current is None:
            return
        normalized = "event" if kind == "event" else "question"
        self._current[f"{normalized}_index"] = int(index)
        self._current[f"{normalized}_count"] = int(total)
        self._current[f"{normalized}_status"] = "complete"
        if score is not None:
            self._current["last_score"] = float(score)
        detail = f"{normalized} {index}/{total} complete"
        if score is not None:
            detail += f" score={score:.4f}"
        self._write(detail)

    def item_failed(self, kind: str, index: int, error: str) -> None:
        if self._current is None:
            return
        normalized = "event" if kind == "event" else "question"
        self._current[f"{normalized}_status"] = "failed"
        self._current["last_error"] = str(error)[:1_000]
        self._write(f"{normalized} {index} failed: {error}")

    def phase_started(self, phase_name: str) -> None:
        if self._current is None:
            return
        self._current["phase"] = str(phase_name)
        self._current["phase_status"] = "running"
        self._current["phase_started_at"] = utc_now()
        self._current["phase_started_monotonic"] = time.monotonic()
        self._write(f"phase {phase_name} running")

    def phase_completed(
        self,
        phase_name: str,
        usage: Mapping[str, Any] | None,
        *,
        cached: bool = False,
    ) -> None:
        if self._current is None:
            return
        phase_name = str(phase_name)
        if not cached:
            self._add_usage(usage)
        started = self._current.get("phase_started_monotonic")
        elapsed = None
        if isinstance(started, (int, float)):
            elapsed = max(0.0, time.monotonic() - float(started))
        self._current["phase"] = phase_name
        self._current["phase_status"] = "cached" if cached else "complete"
        self._current["last_phase"] = phase_name
        self._current["last_phase_elapsed_seconds"] = elapsed
        suffix = " cached" if cached else " complete"
        calls = int((usage or {}).get("model_calls") or 0)
        self._write(f"phase {phase_name}{suffix} calls={calls}")

    def phase_failed(self, phase_name: str, error: str) -> None:
        if self._current is None:
            return
        self._current["phase"] = str(phase_name)
        self._current["phase_status"] = "failed"
        self._current["last_error"] = str(error)[:1_000]
        self._write(f"phase {phase_name} failed: {error}")

    def heartbeat(self, phase_name: str) -> None:
        if self._current is None:
            return
        self._current["phase"] = str(phase_name)
        self._current["heartbeat_at"] = utc_now()
        self._write(f"heartbeat phase={phase_name}")

    def task_complete(self, task_id: str, status: str) -> None:
        task_id = str(task_id)
        if status in _TERMINAL_STATUSES:
            self._completed_task_ids.add(task_id)
        if self._current is None or self._current.get("task_id") != task_id:
            self._current = {"task_id": task_id, "status": str(status)}
        else:
            self._current["status"] = str(status)
            self._current["finished_at"] = utc_now()
        self._payload["current"] = self._current
        self._payload["completed_tasks"] = self._completed_tasks_count()
        self._write(f"task complete {task_id} status={status}")

    def task_skipped(self, sequence: int, task_id: str, status: str) -> None:
        self._completed_task_ids.add(str(task_id))
        self._payload["completed_tasks"] = self._completed_tasks_count()
        self._current = {
            "sequence": int(sequence),
            "task_id": str(task_id),
            "status": "skipped",
            "terminal_status": str(status),
        }
        self._payload["current"] = self._current
        self._write(f"task skip {sequence}/{self.total_tasks} {task_id} status={status}")

    def finish(self, status: str) -> None:
        self._payload["status"] = str(status)
        self._payload["completed_tasks"] = self._completed_tasks_count()
        self._write(f"finished status={status}")

    def interrupted(self, message: str = "interrupted") -> None:
        self._payload["status"] = "interrupted"
        self._payload["last_event"] = str(message)
        self._write(str(message))

    def failed(self, message: str) -> None:
        self._payload["status"] = "failed"
        self._payload["last_event"] = str(message)
        self._write(str(message))

    def _load_existing(self) -> None:
        try:
            existing = read_json(self.progress_path)
        except (OSError, TypeError, ValueError):
            return
        if (
            existing.get("schema_version") != PROGRESS_SCHEMA
            or existing.get("benchmark") != self.benchmark
            or existing.get("profile") != self.profile
            or existing.get("model") != self.model
            or int(existing.get("total_tasks") or -1) != self.total_tasks
        ):
            return
        self._prior_elapsed_seconds = float(existing.get("elapsed_seconds") or 0.0)
        usage = existing.get("usage")
        if isinstance(usage, Mapping):
            for name in _USAGE_FIELDS:
                self._usage[name] = int(usage.get(name) or 0)
        completed = existing.get("completed_tasks")
        if isinstance(completed, int) and completed >= 0:
            self._prior_completed_tasks = completed

    def _add_usage(self, usage: Mapping[str, Any] | None) -> None:
        if usage is None:
            return
        for name in _USAGE_FIELDS:
            self._usage[name] += int(usage.get(name) or 0)

    def _elapsed_seconds(self) -> float:
        return self._prior_elapsed_seconds + max(0.0, time.monotonic() - self._session_started)

    def _eta_seconds(self) -> float | None:
        completed = self._completed_tasks_count()
        if completed <= 0 or completed >= self.total_tasks:
            return None
        return self._elapsed_seconds() / completed * (self.total_tasks - completed)

    def _completed_tasks_count(self) -> int:
        return max(self._prior_completed_tasks, len(self._completed_task_ids))

    def _write(self, event: str, *, emit_line: bool = True) -> None:
        now = utc_now()
        self._payload.update(
            {
                "last_updated_at": now,
                "elapsed_seconds": round(self._elapsed_seconds(), 3),
                "eta_seconds": (
                    round(self._eta_seconds(), 3) if self._eta_seconds() is not None else None
                ),
                "completed_tasks": self._completed_tasks_count(),
                "current": self._current,
                "last_event": str(event),
                "usage": dict(self._usage),
            }
        )
        line = self._format_line(event)
        try:
            atomic_json(self.progress_path, self._payload)
            atomic_text(self.markdown_path, self._markdown())
            with self.log_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(f"{now} {line}\n")
        except Exception:  # noqa: BLE001 - progress must never affect evaluation
            if emit_line:
                self._safe_emit(line)
            return
        if emit_line:
            self._safe_emit(line)

    def _safe_emit(self, line: str) -> None:
        try:
            self.emit(line)
        except Exception:  # noqa: BLE001 - console output is best effort
            return

    def _format_line(self, event: str) -> str:
        completed = self._completed_tasks_count()
        bar = _bar(completed, self.total_tasks)
        current = self._current or {}
        task = current.get("task_id") or "-"
        phase = current.get("phase") or "-"
        item = _item_progress(current)
        eta = _duration(self._eta_seconds())
        elapsed = _duration(self._elapsed_seconds())
        calls = int(self._usage.get("model_calls") or 0)
        return (
            f"[context-memory] {bar} {completed}/{self.total_tasks} "
            f"task={task} phase={phase} {item} calls={calls} "
            f"elapsed={elapsed} eta={eta} event={event}"
        )

    def _markdown(self) -> str:
        current = self._current or {}
        eta = _duration(self._eta_seconds())
        elapsed = _duration(self._elapsed_seconds())
        usage = self._usage
        lines = [
            "# Context-Memory Progress",
            "",
            f"- Status: `{self._payload.get('status')}`",
            f"- Benchmark: `{self.benchmark}`",
            f"- Profile: `{self.profile}`",
            f"- Model: `{self.model}`",
            f"- Tasks: `{self._completed_tasks_count()}/{self.total_tasks}`",
            f"- Elapsed: `{elapsed}`",
            f"- ETA: `{eta}`",
            f"- Model calls: `{usage.get('model_calls', 0)}`",
            f"- Tool calls: `{usage.get('tool_calls', 0)}`",
            f"- Last update: `{self._payload.get('last_updated_at')}`",
            "",
            "## Current task",
            "",
        ]
        if current:
            lines.extend(
                [
                    f"- Task: `{current.get('task_id', '-')}`",
                    f"- Status: `{current.get('status', '-')}`",
                    (
                        f"- Phase: `{current.get('phase', '-')}` "
                        f"(`{current.get('phase_status', '-')}`)"
                    ),
                    f"- Events: `{_count_text(current, 'event')}`",
                    f"- Questions: `{_count_text(current, 'question')}`",
                ]
            )
        else:
            lines.append("- No task is currently running.")
        lines.extend(("", "## Last event", "", str(self._payload.get("last_event") or ""), ""))
        return "\n".join(lines)


def _completed_task_ids(state: Mapping[str, Any]) -> set[str]:
    completed: set[str] = set()
    for task_id, record in (state.get("trials") or {}).items():
        treatment = record.get("treatment") if isinstance(record, Mapping) else None
        status = treatment.get("status") if isinstance(treatment, Mapping) else None
        if status in _TERMINAL_STATUSES:
            completed.add(str(task_id))
    return completed


def _bar(completed: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "[####################]"
    filled = min(width, max(0, int(width * completed / total)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--"
    value = int(seconds)
    hours, remainder = divmod(value, 3_600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _item_progress(current: Mapping[str, Any]) -> str:
    phase = str(current.get("phase") or "")
    if phase.startswith("ingest-"):
        return f"events={current.get('event_index', '--')}/{current.get('event_count', '--')}"
    if phase.startswith(("answer-", "judge-")):
        return f"questions={current.get('question_index', '--')}/{current.get('question_count', '--')}"
    return "items=--"


def _count_text(current: Mapping[str, Any], kind: str) -> str:
    index = current.get(f"{kind}_index")
    count = current.get(f"{kind}_count")
    if index is None or count is None:
        return "--"
    return f"{index}/{count}"
