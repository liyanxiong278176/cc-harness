from __future__ import annotations

import json
import uuid

import pytest

from cc_harness.run_events import (
    EVENT_SCHEMA_VERSION,
    EventActor,
    EventCodec,
    EventValidationError,
    EventValidator,
    RunEvent,
)
from cc_harness.run_model import EvidenceKind, EvidenceRef, GoalContract, RunStatus, RuntimeContract


RUN_ID = "00000000-0000-0000-0000-000000000001"
ACTOR = EventActor("worker", "worker-1")
CONTRACT = RuntimeContract("rebuild-test", 1, "sha256:tools", "sha256:model", "sha256:policy", "sha256:cap")
GOAL = GoalContract("ship", ("tests pass",))


def event(
    sequence: int,
    event_type: str,
    payload: dict,
    *,
    event_id: str | None = None,
    lease_epoch: int = 0,
    runtime_digest: str | None = None,
) -> RunEvent:
    return RunEvent.create(
        event_id=event_id or str(uuid.uuid5(uuid.NAMESPACE_URL, f"event-{sequence}-{event_type}")),
        run_id=RUN_ID,
        sequence=sequence,
        event_type=event_type,
        actor=ACTOR,
        runtime_contract_digest=runtime_digest or CONTRACT.digest,
        payload=payload,
        lease_epoch=lease_epoch,
        correlation_id="00000000-0000-0000-0000-000000000099",
    )


def test_event_codec_is_canonical_and_round_trips() -> None:
    created = event(
        1,
        "RunCreated",
        {"goal": GOAL.to_dict(), "runtime_contract": CONTRACT.to_dict()},
    )
    encoded = EventCodec.encode(created)
    assert encoded == EventCodec.encode(created)
    decoded = EventCodec.decode(encoded)
    assert decoded == created
    assert json.loads(encoded)["schema_version"] == EVENT_SCHEMA_VERSION


def test_history_rebuilds_status_and_rejects_sequence_gaps() -> None:
    evidence = EvidenceRef("e-1", EvidenceKind.TEST, "sha256:test", "pytest", 1.0)
    history = [
        event(1, "RunCreated", {"goal": GOAL.to_dict(), "runtime_contract": CONTRACT.to_dict()}),
        event(2, "RunQueued", {}),
        event(3, "RunClaimed", {"worker_id": "worker-1"}, lease_epoch=1),
        event(4, "RunSegmentStarted", {"segment": 1}, lease_epoch=1),
        event(5, "VerificationRecorded", {"evidence": [evidence.to_dict()]}, lease_epoch=1),
        event(
            6,
            "CompletionAccepted",
            {"acceptance_criteria": ["tests pass"], "evidence": [evidence.to_dict()]},
            lease_epoch=1,
        ),
    ]
    assert EventValidator.validate_history(history) is RunStatus.COMPLETED

    broken = list(history)
    broken[3] = event(7, "RunSegmentStarted", {"segment": 1}, lease_epoch=1)
    with pytest.raises(EventValidationError, match="sequence"):
        EventValidator.validate_history(broken)


def test_schema_validation_happens_before_any_secret_redaction() -> None:
    malformed = event(1, "ActionStarted", {"api_key": "redacted"})
    with pytest.raises(EventValidationError, match="missing required fields"):
        EventValidator.validate_event(malformed)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda e: RunEvent.from_dict({**e.to_dict(), "schema_version": 99}),
        lambda e: RunEvent.from_dict({**e.to_dict(), "event_type": "UnknownEvent"}),
        lambda e: RunEvent.from_dict({**e.to_dict(), "event_id": "not-a-uuid"}),
        lambda e: RunEvent.from_dict({**e.to_dict(), "occurred_at": "not-a-date"}),
    ],
)
def test_invalid_envelope_values_are_rejected(mutator) -> None:
    base = event(1, "RunQueued", {})
    with pytest.raises(EventValidationError):
        mutator(base)


def test_duplicate_event_id_and_stale_lease_are_rejected() -> None:
    first = event(1, "RunCreated", {"goal": GOAL.to_dict(), "runtime_contract": CONTRACT.to_dict()})
    duplicate = event(
        2,
        "RunQueued",
        {},
        event_id=first.event_id,
    )
    with pytest.raises(EventValidationError, match="duplicate event id"):
        EventValidator.validate_history([first, duplicate])

    claimed = event(2, "RunClaimed", {"worker_id": "worker-1"}, lease_epoch=1)
    with pytest.raises(EventValidationError, match="stale"):
        EventValidator.validate_event(claimed, expected_lease_epoch=2)


def test_illegal_state_change_and_runtime_contract_staleness_are_rejected() -> None:
    action = event(1, "ActionStarted", {"action_id": "a-1", "attempt": 1})
    with pytest.raises(EventValidationError, match="invalid from draft"):
        EventValidator.validate_event(action, current_status=RunStatus.DRAFT)

    queued = event(1, "RunQueued", {})
    with pytest.raises(EventValidationError, match="stale"):
        EventValidator.validate_event(
            queued,
            expected_runtime_contract_digest="sha256:old-runtime",
        )


def test_runtime_migration_changes_digest_and_fences_old_history() -> None:
    new_digest = "sha256:new-runtime"
    history = [
        event(1, "RunCreated", {"goal": GOAL.to_dict(), "runtime_contract": CONTRACT.to_dict()}),
        event(2, "RunQueued", {}),
        event(
            3,
            "RunRuntimeMigrated",
            {
                "previous_runtime_contract_digest": CONTRACT.digest,
                "new_runtime_contract_digest": new_digest,
            },
            runtime_digest=new_digest,
        ),
    ]
    assert EventValidator.validate_history(history) is RunStatus.QUEUED

    stale = event(4, "WorkerHeartbeat", {"heartbeat_at": "2026-08-17T00:00:00Z"})
    with pytest.raises(EventValidationError, match="stale"):
        EventValidator.validate_event(stale, expected_runtime_contract_digest=new_digest)


def test_model_invocation_terminal_usage_and_outcome_are_replayable() -> None:
    history = [
        event(1, "RunCreated", {"goal": GOAL.to_dict(), "runtime_contract": CONTRACT.to_dict()}),
        event(2, "RunQueued", {}),
        event(3, "RunClaimed", {"worker_id": "worker-1"}, lease_epoch=1),
        event(
            4,
            "ModelInvocationStarted",
            {"invocation_id": "model-1", "segment": 0, "round": 0},
            lease_epoch=1,
        ),
        event(
            5,
            "ModelInvocationFinished",
            {
                "invocation_id": "model-1",
                "status": "succeeded",
                "duration_ms": 42,
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 8,
                    "output_tokens": 2,
                    "model_calls": 1,
                    "reported_cost": 0.01,
                    "reported_cost_currency": "USD",
                },
            },
            lease_epoch=1,
        ),
        event(
            6,
            "RunOutcomeRecorded",
            {"outcome": "pass", "primary_class": "none", "retryable": False},
            lease_epoch=1,
        ),
    ]

    assert EventValidator.validate_history(history) is RunStatus.RUNNING
    assert EventCodec.decode(EventCodec.encode(history[5])).payload["outcome"] == "pass"


def test_model_invocation_usage_rejects_negative_or_unknown_values() -> None:
    negative = event(
        1,
        "ModelInvocationFinished",
        {"invocation_id": "model-1", "status": "succeeded", "usage": {"output_tokens": -1}},
    )
    with pytest.raises(EventValidationError, match="cannot be negative"):
        EventValidator.validate_event(negative)

    unknown_status = event(
        1,
        "ModelInvocationFinished",
        {"invocation_id": "model-1", "status": "unknown", "usage": {}},
    )
    with pytest.raises(EventValidationError, match="status is invalid"):
        EventValidator.validate_event(unknown_status)
