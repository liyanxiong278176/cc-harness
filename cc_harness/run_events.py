"""Run Event envelope, schema validation, and lifecycle validation.

Events are the durable facts of a run.  This module validates an event before
it reaches a store; it never mutates or "redacts" a malformed payload into a
different event.  Secret handling belongs to the capability/object adapters,
while schema and lifecycle checks remain mandatory.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .run_model import (
    CompletionCandidate,
    GoalContract,
    InvalidRunTransition,
    RunStateMachine,
    RunStatus,
    RuntimeContract,
)


class EventValidationError(ValueError):
    """Raised when an event is malformed, stale, or out of sequence."""


EVENT_SCHEMA_VERSION = 1

EVENT_TYPES = frozenset(
    {
        "RunCreated",
        "GoalContractAccepted",
        "GoalContractRevised",
        "RunQueued",
        "RunClaimed",
        "RunYielded",
        "WorkerLeaseExpired",
        "WorkerHeartbeat",
        "RunSegmentStarted",
        "RunSegmentFinished",
        "PlanDiscoveryStarted",
        "PlanDiscoveryCompleted",
        "PlanNodeStarted",
        "PlanNodeCompleted",
        "PlanNodeBlocked",
        "ModelInvocationStarted",
        "AssistantMessageCommitted",
        "AssistantMessageInterrupted",
        "ToolObservationChunkCommitted",
        "ToolObservationCommitted",
        "ContextProjectionBuilt",
        "ContextCompacted",
        "MemoryCandidateRecorded",
        "MemoryCheckpointCommitted",
        "PredecessorHandoffCommitted",
        "ChildDelegationCommitted",
        "PlanCreated",
        "PlanRevised",
        "ChildRunCreated",
        "ChildRunClaimed",
        "ChildCandidateSubmitted",
        "ChildCandidateAccepted",
        "ChildCandidateRejected",
        "IntegrationConflictRaised",
        "ActionPlanned",
        "ActionPrepared",
        "ActionStarted",
        "ActionProgressRecorded",
        "ActionSucceeded",
        "ActionFailed",
        "ActionCancelled",
        "ActionOutcomeUnknown",
        "ReconciliationStarted",
        "ReconciliationResolved",
        "ApprovalRequested",
        "ApprovalGranted",
        "ApprovalRejected",
        "InterruptRequested",
        "FollowUpQueued",
        "FollowUpStarted",
        "PredecessorBypassed",
        "ProgressRecorded",
        "TodoCreated",
        "TodoUpdated",
        "TodoCompleted",
        "StallDiagnosisRecorded",
        "RunStalled",
        "VerificationRecorded",
        "CompletionCandidateSubmitted",
        "CompletionAccepted",
        "RunBlocked",
        "RunCancelled",
        "RunFailed",
        "RunResumed",
        "RunRuntimeMigrated",
        "LegacyRunImported",
        "RunSnapshotCreated",
    }
)


_REQUIRED_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    "RunCreated": ("goal", "runtime_contract"),
    "GoalContractAccepted": ("goal",),
    "GoalContractRevised": ("goal",),
    "RunClaimed": ("worker_id",),
    "WorkerLeaseExpired": ("reason",),
    "WorkerHeartbeat": ("heartbeat_at",),
    "RunSegmentStarted": ("segment",),
    "RunSegmentFinished": ("segment",),
    "PlanDiscoveryStarted": ("discovery_id",),
    "PlanDiscoveryCompleted": ("discovery_id", "result"),
    "PlanNodeStarted": ("node_id",),
    "PlanNodeCompleted": ("node_id",),
    "PlanNodeBlocked": ("node_id", "reason"),
    "ModelInvocationStarted": ("invocation_id", "segment", "round"),
    "AssistantMessageCommitted": ("message_id", "message_artifact", "segment", "round"),
    "AssistantMessageInterrupted": ("message_id", "reason"),
    "ToolObservationChunkCommitted": (
        "observation_id",
        "chunk_index",
        "observation_artifact",
        "complete",
    ),
    "ToolObservationCommitted": (
        "observation_id",
        "action_id",
        "attempt",
        "observation_artifact",
        "status",
        "complete",
    ),
    "ContextProjectionBuilt": (
        "projection_id",
        "source_message_count",
        "projected_message_count",
        "projection_digest",
    ),
    "ContextCompacted": ("projection_id", "tier", "source_digest"),
    "MemoryCandidateRecorded": ("candidate_id", "source_digest", "status"),
    "MemoryCheckpointCommitted": ("checkpoint_id", "source_digest", "captured_count"),
    "PredecessorHandoffCommitted": ("predecessor_run_id", "handoff_artifact"),
    "ChildDelegationCommitted": ("child_run_id", "delegation_artifact"),
    "RunYielded": ("segment",),
    "PlanCreated": ("plan",),
    "PlanRevised": ("plan",),
    "ChildRunCreated": ("child_run_id",),
    "ChildRunClaimed": ("child_run_id", "worker_id"),
    "ChildCandidateSubmitted": ("child_run_id", "candidate_commit", "diff_digest"),
    "ChildCandidateAccepted": ("child_run_id",),
    "ChildCandidateRejected": ("child_run_id", "reason"),
    "IntegrationConflictRaised": ("child_run_id", "paths"),
    "ActionPlanned": ("action_id", "attempt", "tool_name", "effect_class"),
    "ActionPrepared": ("action_id", "attempt"),
    "ActionStarted": ("action_id", "attempt"),
    "ActionProgressRecorded": ("action_id", "attempt", "progress"),
    "ActionSucceeded": ("action_id", "attempt"),
    "ActionFailed": ("action_id", "attempt", "error_kind"),
    "ActionCancelled": ("action_id", "attempt"),
    "ActionOutcomeUnknown": ("action_id", "attempt", "reason"),
    "ReconciliationStarted": ("action_id",),
    "ReconciliationResolved": ("action_id", "resolved_status"),
    "ApprovalRequested": ("approval_id", "action_id", "action_args_digest", "scope"),
    "ApprovalGranted": ("approval_id", "action_args_digest"),
    "ApprovalRejected": ("approval_id", "reason"),
    "InterruptRequested": ("reason",),
    "FollowUpQueued": ("follow_up_run_id", "message_artifact"),
    "FollowUpStarted": ("follow_up_run_id",),
    "PredecessorBypassed": ("predecessor_run_id", "reason"),
    "ProgressRecorded": ("progress",),
    "TodoCreated": ("todo",),
    "TodoUpdated": ("todo",),
    "TodoCompleted": ("todo_id",),
    "StallDiagnosisRecorded": ("diagnosis",),
    "RunStalled": ("reason",),
    "VerificationRecorded": ("evidence",),
    "CompletionCandidateSubmitted": ("acceptance_criteria", "evidence"),
    "CompletionAccepted": ("acceptance_criteria", "evidence"),
    "RunBlocked": ("reason",),
    "RunCancelled": ("reason",),
    "RunFailed": ("reason", "target_status"),
    "RunResumed": ("reason",),
    "RunRuntimeMigrated": ("previous_runtime_contract_digest", "new_runtime_contract_digest"),
    "LegacyRunImported": ("source_digest",),
    "RunSnapshotCreated": ("snapshot_digest",),
}

_ACTION_EVENTS = {
    "ActionPlanned",
    "ActionPrepared",
    "ActionStarted",
    "ActionProgressRecorded",
    "ActionSucceeded",
    "ActionFailed",
    "ActionCancelled",
    "ActionOutcomeUnknown",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_uuid(value: str, field_name: str) -> None:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise EventValidationError(f"{field_name} must be a UUID") from exc


def _require_string(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise EventValidationError(f"{field_name} must be a non-empty string")


@dataclass(frozen=True)
class EventActor:
    kind: str
    actor_id: str

    def __post_init__(self) -> None:
        _require_string(self.kind, "actor.kind")
        _require_string(self.actor_id, "actor.id")

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.actor_id}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EventActor":
        if not isinstance(data, Mapping):
            raise EventValidationError("actor must be an object")
        return cls(kind=str(data.get("kind", "")), actor_id=str(data.get("id", "")))


@dataclass(frozen=True)
class RunEvent:
    event_id: str
    run_id: str
    sequence: int
    event_type: str
    schema_version: int
    occurred_at: str
    actor: EventActor
    causation_id: str | None
    correlation_id: str
    lease_epoch: int
    runtime_contract_digest: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        sequence: int,
        event_type: str,
        actor: EventActor,
        runtime_contract_digest: str,
        payload: Mapping[str, Any] | None = None,
        lease_epoch: int = 0,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        event_id: str | None = None,
        occurred_at: str | None = None,
        artifact_refs: tuple[str, ...] = (),
    ) -> "RunEvent":
        return cls(
            event_id=event_id or str(uuid.uuid4()),
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            schema_version=EVENT_SCHEMA_VERSION,
            occurred_at=occurred_at or utc_now(),
            actor=actor,
            causation_id=causation_id,
            correlation_id=correlation_id or str(uuid.uuid4()),
            lease_epoch=lease_epoch,
            runtime_contract_digest=runtime_contract_digest,
            payload=dict(payload or {}),
            artifact_refs=tuple(artifact_refs),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "schema_version": self.schema_version,
            "occurred_at": self.occurred_at,
            "actor": self.actor.to_dict(),
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "lease_epoch": self.lease_epoch,
            "runtime_contract_digest": self.runtime_contract_digest,
            "payload": dict(self.payload),
            "artifact_refs": list(self.artifact_refs),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunEvent":
        if not isinstance(data, Mapping):
            raise EventValidationError("event must be an object")
        try:
            event = cls(
                event_id=str(data["event_id"]),
                run_id=str(data["run_id"]),
                sequence=int(data["sequence"]),
                event_type=str(data["event_type"]),
                schema_version=int(data["schema_version"]),
                occurred_at=str(data["occurred_at"]),
                actor=EventActor.from_dict(data["actor"]),
                causation_id=data.get("causation_id"),
                correlation_id=str(data["correlation_id"]),
                lease_epoch=int(data["lease_epoch"]),
                runtime_contract_digest=str(data["runtime_contract_digest"]),
                payload=dict(data.get("payload") or {}),
                artifact_refs=tuple(str(item) for item in data.get("artifact_refs") or ()),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EventValidationError("event envelope is missing or has invalid fields") from exc
        EventValidator.validate_event(event)
        return event


class EventCodec:
    """Canonical JSON codec for durable events."""

    @staticmethod
    def encode(event: RunEvent) -> bytes:
        EventValidator.validate_event(event)
        try:
            return json.dumps(
                event.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise EventValidationError("event payload is not JSON serializable") from exc

    @staticmethod
    def decode(raw: bytes | str) -> RunEvent:
        try:
            data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EventValidationError("event is not valid UTF-8 JSON") from exc
        return RunEvent.from_dict(data)


class EventValidator:
    """Validates envelope, event payload, sequence, lease, and state changes."""

    @staticmethod
    def validate_event(
        event: RunEvent,
        *,
        expected_sequence: int | None = None,
        expected_run_id: str | None = None,
        expected_lease_epoch: int | None = None,
        expected_runtime_contract_digest: str | None = None,
        current_status: RunStatus | None = None,
        goal: GoalContract | None = None,
    ) -> None:
        _require_uuid(event.event_id, "event_id")
        _require_uuid(event.run_id, "run_id")
        _require_uuid(event.correlation_id, "correlation_id")
        if event.causation_id is not None:
            _require_uuid(event.causation_id, "causation_id")
            if event.causation_id == event.event_id:
                raise EventValidationError("causation_id cannot equal event_id")
        if expected_run_id is not None and event.run_id != expected_run_id:
            raise EventValidationError("event run_id does not match the stream")
        if event.sequence < 1:
            raise EventValidationError("event sequence must be positive")
        if expected_sequence is not None and event.sequence != expected_sequence:
            raise EventValidationError(
                f"event sequence {event.sequence} does not match expected {expected_sequence}"
            )
        if event.event_type not in EVENT_TYPES:
            raise EventValidationError(f"unknown event type: {event.event_type}")
        if event.schema_version != EVENT_SCHEMA_VERSION:
            raise EventValidationError(f"unsupported event schema: {event.schema_version}")
        if event.lease_epoch < 0:
            raise EventValidationError("lease_epoch cannot be negative")
        _require_string(event.runtime_contract_digest, "runtime_contract_digest")
        if expected_lease_epoch is not None and event.lease_epoch != expected_lease_epoch:
            raise EventValidationError("event lease epoch is stale")
        if (
            expected_runtime_contract_digest is not None
            and event.runtime_contract_digest != expected_runtime_contract_digest
            and event.event_type != "RunRuntimeMigrated"
        ):
            raise EventValidationError("event runtime contract digest is stale")
        try:
            parsed = datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EventValidationError("occurred_at must be RFC3339") from exc
        if parsed.tzinfo is None:
            raise EventValidationError("occurred_at must include a timezone")
        if not isinstance(event.payload, Mapping):
            raise EventValidationError("payload must be an object")
        try:
            json.dumps(event.payload, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise EventValidationError("payload must be JSON serializable") from exc
        for reference in event.artifact_refs:
            _require_string(reference, "artifact_refs item")

        required = _REQUIRED_PAYLOAD_FIELDS.get(event.event_type, ())
        missing = [field_name for field_name in required if field_name not in event.payload]
        if missing:
            raise EventValidationError(
                f"{event.event_type} payload is missing required fields: {missing}"
            )
        EventValidator._validate_payload_types(event)
        if current_status is not None:
            target = None
            if event.event_type == "RunFailed":
                try:
                    target = RunStatus(str(event.payload["target_status"]))
                except ValueError as exc:
                    raise EventValidationError("RunFailed target_status is invalid") from exc
            elif event.event_type == "RunRuntimeMigrated":
                target = current_status
            try:
                if event.event_type == "CompletionAccepted":
                    candidate = CompletionCandidate.from_dict(event.payload)
                    if goal is None:
                        raise EventValidationError(
                            "CompletionAccepted validation requires the active goal contract"
                        )
                    RunStateMachine().transition(
                        current_status,
                        event.event_type,
                        completion=candidate,
                        goal=goal,
                    )
                else:
                    RunStateMachine().transition(current_status, event.event_type, target=target)
            except (InvalidRunTransition, ValueError) as exc:
                if isinstance(exc, EventValidationError):
                    raise
                raise EventValidationError(str(exc)) from exc

    @staticmethod
    def _validate_payload_types(event: RunEvent) -> None:
        payload = event.payload
        string_fields = {
            field_name
            for field_name in _REQUIRED_PAYLOAD_FIELDS.get(event.event_type, ())
            if field_name
            not in {
                "goal",
                "runtime_contract",
                "plan",
                "todo",
                "paths",
                "scope",
                "evidence",
                "acceptance_criteria",
                "progress",
                "attempt",
                "segment",
                "result",
                "status",
                "complete",
                "round",
                "chunk_index",
                "source_message_count",
                "projected_message_count",
                "captured_count",
            }
        }
        for field_name in string_fields:
            _require_string(payload[field_name], f"payload.{field_name}")
        if event.event_type in _ACTION_EVENTS:
            if not isinstance(payload["attempt"], int) or payload["attempt"] < 1:
                raise EventValidationError("action attempt must be a positive integer")
        if event.event_type in {"RunSegmentStarted", "RunSegmentFinished", "RunYielded"}:
            if not isinstance(payload["segment"], int) or payload["segment"] < 0:
                raise EventValidationError("segment must be a non-negative integer")
        if event.event_type == "ModelInvocationStarted":
            if not isinstance(payload["segment"], int) or payload["segment"] < 0:
                raise EventValidationError("model invocation segment must be non-negative")
            if not isinstance(payload["round"], int) or payload["round"] < 0:
                raise EventValidationError("model invocation round must be non-negative")
        if event.event_type == "ToolObservationChunkCommitted":
            if not isinstance(payload["chunk_index"], int) or payload["chunk_index"] < 0:
                raise EventValidationError("observation chunk_index must be non-negative")
            if not isinstance(payload["complete"], bool):
                raise EventValidationError("observation complete must be boolean")
        if event.event_type == "ToolObservationCommitted":
            if not isinstance(payload["attempt"], int) or payload["attempt"] < 1:
                raise EventValidationError("observation attempt must be a positive integer")
            _require_string(payload["status"], "payload.status")
        if event.event_type in {"ToolObservationCommitted"} and not isinstance(
            payload["complete"], bool
        ):
            raise EventValidationError("observation complete must be boolean")
        if event.event_type in {"ContextProjectionBuilt"}:
            for name in ("source_message_count", "projected_message_count"):
                if not isinstance(payload[name], int) or payload[name] < 0:
                    raise EventValidationError(f"{name} must be a non-negative integer")
        if event.event_type == "MemoryCheckpointCommitted":
            if not isinstance(payload["captured_count"], int) or payload["captured_count"] < 0:
                raise EventValidationError("captured_count must be a non-negative integer")
        if event.event_type == "MemoryCandidateRecorded":
            _require_string(payload["status"], "payload.status")
        if event.event_type in {"PlanCreated", "PlanRevised"} and not isinstance(
            payload["plan"], Mapping
        ):
            raise EventValidationError("plan payload must be an object")
        if event.event_type in {"TodoCreated", "TodoUpdated"} and not isinstance(
            payload["todo"], Mapping
        ):
            raise EventValidationError("todo payload must be an object")
        if event.event_type in {"VerificationRecorded"}:
            if not isinstance(payload["evidence"], list | tuple) or not payload["evidence"]:
                raise EventValidationError("verification requires at least one evidence ref")
        if event.event_type in {"CompletionAccepted", "CompletionCandidateSubmitted"}:
            if not isinstance(payload["acceptance_criteria"], list | tuple):
                raise EventValidationError("acceptance_criteria must be a list")
            if not isinstance(payload["evidence"], list | tuple) or not payload["evidence"]:
                raise EventValidationError("completion candidate requires evidence")
        if event.event_type == "RunCreated":
            try:
                GoalContract.from_dict(payload["goal"])
                RuntimeContract.from_dict(payload["runtime_contract"])
            except (KeyError, TypeError, ValueError) as exc:
                raise EventValidationError("RunCreated contains an invalid goal or runtime contract") from exc
        if event.event_type in {"GoalContractAccepted", "GoalContractRevised"}:
            try:
                GoalContract.from_dict(payload["goal"])
            except (KeyError, TypeError, ValueError) as exc:
                raise EventValidationError("goal contract payload is invalid") from exc
        if event.event_type == "RunRuntimeMigrated":
            _require_string(payload["previous_runtime_contract_digest"], "payload.previous...")
            _require_string(payload["new_runtime_contract_digest"], "payload.new...")
            if payload["previous_runtime_contract_digest"] == payload["new_runtime_contract_digest"]:
                raise EventValidationError("runtime migration must change the contract digest")

    @classmethod
    def validate_history(cls, events: list[RunEvent] | tuple[RunEvent, ...]) -> RunStatus:
        if not events:
            raise EventValidationError("event history cannot be empty")
        if events[0].event_type != "RunCreated" or events[0].sequence != 1:
            raise EventValidationError("event history must start with RunCreated at sequence 1")
        seen_ids: set[str] = set()
        current = RunStatus.DRAFT
        run_id = events[0].run_id
        runtime_digest = events[0].runtime_contract_digest
        goal: GoalContract | None = None
        for expected_sequence, event in enumerate(events, start=1):
            if event.event_id in seen_ids:
                raise EventValidationError(f"duplicate event id: {event.event_id}")
            seen_ids.add(event.event_id)
            cls.validate_event(
                event,
                expected_sequence=expected_sequence,
                expected_run_id=run_id,
                expected_runtime_contract_digest=runtime_digest,
                current_status=current,
                goal=goal,
            )
            if event.event_type == "RunCreated":
                goal = GoalContract.from_dict(event.payload["goal"])
            elif event.event_type in {"GoalContractAccepted", "GoalContractRevised"}:
                goal = GoalContract.from_dict(event.payload["goal"])
            elif event.event_type == "RunRuntimeMigrated":
                runtime_digest = str(event.payload["new_runtime_contract_digest"])
            target = None
            if event.event_type == "RunFailed":
                target = RunStatus(str(event.payload["target_status"]))
            elif event.event_type == "RunRuntimeMigrated":
                target = current
            if event.event_type == "CompletionAccepted":
                current = RunStateMachine().transition(
                    current,
                    event.event_type,
                    completion=CompletionCandidate.from_dict(event.payload),
                    goal=goal,
                )
            else:
                current = RunStateMachine().transition(current, event.event_type, target=target)
        return current


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "EVENT_TYPES",
    "EventActor",
    "EventCodec",
    "EventValidationError",
    "EventValidator",
    "RunEvent",
    "utc_now",
]
