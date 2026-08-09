"""Normalize cc-harness and Claude Code streams into semantic tool events."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from eval.core import canonical_json_bytes
from eval.launch import HarnessKind

from .models import (
    SemanticEvent,
    SemanticEventKind,
    SemanticOutcome,
    SemanticTrajectory,
)


def normalize_trajectory(harness: HarnessKind, stdout: bytes) -> SemanticTrajectory:
    documents = _jsonl(stdout)
    if harness is HarnessKind.CC_HARNESS:
        events = _cc_events(documents)
    elif harness is HarnessKind.CLAUDE_CODE:
        events = _claude_events(documents)
    else:
        raise ValueError(f"unsupported specialist harness: {harness.value}")
    return SemanticTrajectory(harness=harness.value, events=tuple(events))


def summarize_trajectory(trajectory: SemanticTrajectory) -> dict[str, int | bool]:
    calls = [event for event in trajectory.events if event.kind is SemanticEventKind.TOOL_CALL]
    results = [event for event in trajectory.events if event.kind is SemanticEventKind.TOOL_RESULT]
    repeated_calls = 0
    max_consecutive_repeat = 0
    current_repeat = 0
    previous: tuple[str | None, str | None] | None = None
    for event in calls:
        identity = (event.capability, event.argument_digest)
        if identity == previous:
            repeated_calls += 1
            current_repeat += 1
            max_consecutive_repeat = max(max_consecutive_repeat, current_repeat)
        else:
            current_repeat = 0
        previous = identity

    recovered_after_error = False
    saw_error = False
    for result in results:
        if result.outcome in {SemanticOutcome.ERROR, SemanticOutcome.TIMEOUT}:
            saw_error = True
        elif saw_error and result.outcome is SemanticOutcome.SUCCESS:
            recovered_after_error = True
            break
    return {
        "tool_calls": len(calls),
        "tool_errors": sum(
            result.outcome in {SemanticOutcome.ERROR, SemanticOutcome.TIMEOUT} for result in results
        ),
        "repeated_calls": repeated_calls,
        "max_consecutive_repeat": max_consecutive_repeat,
        "recovered_after_error": recovered_after_error,
        "final_observed": any(event.kind is SemanticEventKind.FINAL for event in trajectory.events),
    }


def _jsonl(stdout: bytes) -> list[dict[str, Any]]:
    try:
        documents = [json.loads(line) for line in stdout.decode("utf-8").splitlines() if line]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"trajectory is not valid JSONL: {exc}") from exc
    if not documents or not all(isinstance(item, dict) for item in documents):
        raise ValueError("trajectory contains no JSON objects")
    return documents


def _cc_events(documents: list[dict[str, Any]]) -> list[SemanticEvent]:
    terminal = documents[-1]
    if terminal.get("schema_version") != "cc-harness.print-result.v1":
        raise ValueError("cc-harness print result is missing")
    raw_events = terminal.get("trajectory")
    if not isinstance(raw_events, list):
        raise TypeError("cc-harness trajectory is missing")
    events: list[SemanticEvent] = []
    pending_call_ids: list[str] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if kind == "action":
            name = str(raw.get("name") or "unknown")
            call_id = f"cc-call-{len(pending_call_ids) + 1}"
            pending_call_ids.append(call_id)
            events.append(_tool_call(len(events) + 1, name, raw.get("args"), call_id=call_id))
        elif kind == "observation":
            call_id = pending_call_ids.pop(0) if pending_call_ids else None
            events.append(
                SemanticEvent(
                    sequence=len(events) + 1,
                    kind=SemanticEventKind.TOOL_RESULT,
                    call_id=call_id,
                    outcome=(
                        SemanticOutcome.ERROR
                        if bool(raw.get("is_error"))
                        else SemanticOutcome.SUCCESS
                    ),
                    duration_ms=_optional_nonnegative_int(raw.get("duration_ms")),
                )
            )
        elif kind == "result":
            events.append(
                SemanticEvent(
                    sequence=len(events) + 1,
                    kind=SemanticEventKind.FINAL,
                    outcome=SemanticOutcome.SUCCESS,
                )
            )
    return events


def _claude_events(documents: list[dict[str, Any]]) -> list[SemanticEvent]:
    events: list[SemanticEvent] = []
    for document in documents:
        document_type = document.get("type")
        message = document.get("message")
        if document_type in {"assistant", "user"} and isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        name = str(block.get("name") or "unknown")
                        events.append(
                            _tool_call(
                                len(events) + 1,
                                name,
                                block.get("input"),
                                call_id=_optional_string(block.get("id")),
                            )
                        )
                    elif block.get("type") == "tool_result":
                        events.append(
                            SemanticEvent(
                                sequence=len(events) + 1,
                                kind=SemanticEventKind.TOOL_RESULT,
                                call_id=_optional_string(block.get("tool_use_id")),
                                outcome=(
                                    SemanticOutcome.ERROR
                                    if bool(block.get("is_error"))
                                    else SemanticOutcome.SUCCESS
                                ),
                            )
                        )
        if document_type == "result":
            is_error = bool(document.get("is_error")) or document.get("subtype") in {
                "error",
                "error_max_turns",
                "error_max_budget_usd",
            }
            events.append(
                SemanticEvent(
                    sequence=len(events) + 1,
                    kind=(SemanticEventKind.ERROR if is_error else SemanticEventKind.FINAL),
                    outcome=(SemanticOutcome.ERROR if is_error else SemanticOutcome.SUCCESS),
                )
            )
    return events


def _tool_call(
    sequence: int,
    name: str,
    arguments: Any,
    *,
    call_id: str | None,
) -> SemanticEvent:
    normalized_arguments = arguments if isinstance(arguments, dict) else {"raw": arguments}
    digest = hashlib.sha256(canonical_json_bytes(normalized_arguments)).hexdigest()
    return SemanticEvent(
        sequence=sequence,
        kind=SemanticEventKind.TOOL_CALL,
        capability=_semantic_capability(name),
        native_name=name,
        call_id=call_id,
        argument_digest=f"sha256:{digest}",
    )


def _semantic_capability(name: str) -> str:
    lowered = name.lower()
    if lowered.startswith(("mcp__", "mcp_")):
        return "mcp"
    if lowered in {"bash", "shell", "run_command", "run-command"}:
        return "shell"
    for capability in ("read", "edit", "write", "glob", "grep"):
        if lowered == capability or lowered.endswith(f"__{capability}"):
            return capability
    return "mcp"


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return None
