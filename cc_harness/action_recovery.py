"""Migration and recovery helpers for legacy JSONL action journals."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .artifacts import ArtifactStore
from .run_events import EventActor, RunEvent


@dataclass(frozen=True)
class JournalMigrationReport:
    events: tuple[RunEvent, ...]
    completed_actions: tuple[str, ...]
    unknown_actions: tuple[str, ...]
    skipped_records: int = 0


def migrate_legacy_journal(
    records: Iterable[Mapping[str, Any]],
    *,
    run_id: str,
    runtime_contract_digest: str,
    start_sequence: int = 1,
    artifact_store: ArtifactStore | None = None,
    actor_id: str = "legacy-importer",
    lease_epoch: int = 0,
) -> JournalMigrationReport:
    """Convert legacy action facts without replaying an incomplete action.

    Legacy records are grouped by action id.  A missing intent is synthesized
    from the first available record, while a missing terminal result becomes
    ``ActionOutcomeUnknown``.  The conversion is deterministic for the same
    source records and sequence offset.
    """
    if start_sequence < 1:
        raise ValueError("start_sequence must be positive")
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    skipped = 0
    for record in records:
        action_id = str(record.get("action_id") or "")
        if not action_id:
            skipped += 1
            continue
        grouped.setdefault(action_id, []).append(record)

    actor = EventActor("legacy_importer", actor_id)
    output: list[RunEvent] = []
    completed: list[str] = []
    unknown: list[str] = []
    sequence = start_sequence
    for action_id, action_records in grouped.items():
        first = action_records[0]
        tool = str(first.get("tool") or "unknown")
        args = first.get("args") if isinstance(first.get("args"), Mapping) else {}
        args_digest = _digest_json(args)
        correlation_id = _stable_uuid(run_id, action_id, "correlation")
        common = {
            "run_id": run_id,
            "actor": actor,
            "runtime_contract_digest": runtime_contract_digest,
            "correlation_id": correlation_id,
            "lease_epoch": lease_epoch,
        }
        output.append(
            _event(
                sequence,
                "ActionPlanned",
                {
                    "action_id": action_id,
                    "attempt": 1,
                    "tool_name": tool,
                    "effect_class": "unknown",
                    "normalized_args_digest": args_digest,
                    "contract_digest": "sha256:legacy-unverified",
                },
                common=common,
                source_key=f"{action_id}:planned",
                occurred_at=_record_time(first),
            )
        )
        sequence += 1

        terminal = next(
            (
                item
                for item in action_records
                if str(item.get("event") or item.get("kind"))
                in {"result", "tool_finished", "tool_failed", "finished"}
            ),
            None,
        )
        started_record = next(
            (item for item in action_records if str(item.get("event") or item.get("kind")) in {"started", "tool_started"}),
            terminal,
        )
        if started_record is not None:
            output.append(
                _event(
                    sequence,
                    "ActionPrepared",
                    {"action_id": action_id, "attempt": 1},
                    common=common,
                    source_key=f"{action_id}:prepared",
                    occurred_at=_record_time(started_record),
                )
            )
            sequence += 1
            output.append(
                _event(
                    sequence,
                    "ActionStarted",
                    {"action_id": action_id, "attempt": 1},
                    common=common,
                    source_key=f"{action_id}:started",
                    occurred_at=_record_time(started_record),
                )
            )
            sequence += 1

        if terminal is None:
            unknown.append(action_id)
            output.append(
                _event(
                    sequence,
                    "ActionOutcomeUnknown",
                    {
                        "action_id": action_id,
                        "attempt": 1,
                        "reason": "legacy journal ended without a terminal result",
                    },
                    common=common,
                    source_key=f"{action_id}:unknown",
                    occurred_at=_record_time(action_records[-1]),
                )
            )
            sequence += 1
            continue

        outcome = terminal.get("outcome")
        if not isinstance(outcome, Mapping):
            outcome = {
                key: value
                for key, value in terminal.items()
                if key not in {"session_id", "action_id", "event", "kind", "tool", "args", "created_at"}
            }
        result_ref = _store_result(outcome, artifact_store)
        event_name = str(terminal.get("event") or terminal.get("kind") or "")
        failed = event_name in {"tool_failed"} or bool(outcome.get("is_error")) or str(
            outcome.get("status") or ""
        ).lower() in {"failed", "error"}
        if bool(outcome.get("outcome_unknown")):
            terminal_type = "ActionOutcomeUnknown"
            payload = {
                "action_id": action_id,
                "attempt": 1,
                "reason": str(outcome.get("reason") or "legacy result is explicitly unknown"),
            }
            unknown.append(action_id)
        elif failed:
            terminal_type = "ActionFailed"
            payload = {
                "action_id": action_id,
                "attempt": 1,
                "error_kind": str(outcome.get("error_kind") or "legacy_failure"),
            }
        else:
            terminal_type = "ActionSucceeded"
            payload = {"action_id": action_id, "attempt": 1}
            completed.append(action_id)
        if result_ref is not None:
            payload["result_artifact"] = result_ref
        output.append(
            _event(
                sequence,
                terminal_type,
                payload,
                common=common,
                source_key=f"{action_id}:terminal",
                occurred_at=_record_time(terminal),
            )
        )
        sequence += 1

    return JournalMigrationReport(tuple(output), tuple(completed), tuple(unknown), skipped)


def _event(
    sequence: int,
    event_type: str,
    payload: dict[str, Any],
    *,
    common: Mapping[str, Any],
    source_key: str,
    occurred_at: str,
) -> RunEvent:
    return RunEvent.create(
        event_id=_stable_uuid(str(common["run_id"]), source_key, "event"),
        run_id=str(common["run_id"]),
        sequence=sequence,
        event_type=event_type,
        actor=common["actor"],
        runtime_contract_digest=str(common["runtime_contract_digest"]),
        lease_epoch=int(common.get("lease_epoch", 0)),
        payload=payload,
        correlation_id=str(common["correlation_id"]),
        occurred_at=occurred_at,
    )


def _store_result(outcome: Mapping[str, Any], artifact_store: ArtifactStore | None) -> str | None:
    if not outcome:
        return None
    content = json.dumps(dict(outcome), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    if artifact_store is not None:
        artifact_store.put(content, media_type="application/json", expected_digest=digest)
    return digest


def _digest_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _stable_uuid(run_id: str, value: str, namespace: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cc-harness:{namespace}:{run_id}:{value}"))


def _record_time(record: Mapping[str, Any]) -> str:
    raw = record.get("created_at", record.get("timestamp", record.get("ts")))
    try:
        value = float(raw)
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        return datetime.fromtimestamp(0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["JournalMigrationReport", "migrate_legacy_journal"]
