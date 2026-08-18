from __future__ import annotations

from cc_harness.action_recovery import migrate_legacy_journal
from cc_harness.artifacts import ArtifactStore
from cc_harness.run_model import ActionStatus
from cc_harness.run_projection import ProjectionBuilder
from cc_harness.run_events import EventActor, RunEvent
from cc_harness.run_model import GoalContract, RuntimeContract


RUN_ID = "00000000-0000-0000-0000-000000000301"
CONTRACT = RuntimeContract("journal-test", 1, "sha256:tools", "sha256:model", "sha256:policy", "sha256:cap")
GOAL = GoalContract("migrate journal", ("terminal facts are durable",))


def created_event() -> RunEvent:
    return RunEvent.create(
        event_id="00000000-0000-0000-0000-000000000302",
        run_id=RUN_ID,
        sequence=1,
        event_type="RunCreated",
        actor=EventActor("legacy_importer", "fixture"),
        runtime_contract_digest=CONTRACT.digest,
        payload={"goal": GOAL.to_dict(), "runtime_contract": CONTRACT.to_dict()},
        correlation_id="00000000-0000-0000-0000-000000000399",
    )


def active_prefix() -> list[RunEvent]:
    return [
        created_event(),
        RunEvent.create(
            event_id="00000000-0000-0000-0000-000000000303",
            run_id=RUN_ID,
            sequence=2,
            event_type="RunQueued",
            actor=EventActor("legacy_importer", "fixture"),
            runtime_contract_digest=CONTRACT.digest,
            payload={},
            correlation_id="00000000-0000-0000-0000-000000000399",
        ),
        RunEvent.create(
            event_id="00000000-0000-0000-0000-000000000304",
            run_id=RUN_ID,
            sequence=3,
            event_type="RunClaimed",
            actor=EventActor("worker", "worker-1"),
            runtime_contract_digest=CONTRACT.digest,
            payload={"worker_id": "worker-1"},
            lease_epoch=1,
            correlation_id="00000000-0000-0000-0000-000000000399",
        ),
    ]


def test_legacy_journal_emits_intent_started_and_terminal_result(tmp_path) -> None:
    artifacts = ArtifactStore(tmp_path / "objects")
    report = migrate_legacy_journal(
        [
            {
                "action_id": "action-success",
                "event": "intent",
                "tool": "Read",
                "args": {"path": "README.md"},
                "created_at": 10.0,
            },
            {
                "action_id": "action-success",
                "event": "started",
                "tool": "Read",
                "created_at": 11.0,
            },
            {
                "action_id": "action-success",
                "event": "result",
                "outcome": {"status": "success", "text": "ok"},
                "created_at": 12.0,
            },
        ],
        run_id=RUN_ID,
        runtime_contract_digest=CONTRACT.digest,
        start_sequence=4,
        artifact_store=artifacts,
    )
    assert report.completed_actions == ("action-success",)
    assert [item.event_type for item in report.events] == [
        "ActionPlanned",
        "ActionPrepared",
        "ActionStarted",
        "ActionSucceeded",
    ]
    result_ref = report.events[-1].payload["result_artifact"]
    assert artifacts.read_text(result_ref) == '{"status":"success","text":"ok"}'
    projection = ProjectionBuilder().rebuild(active_prefix() + list(report.events))
    assert projection.actions[0].status is ActionStatus.SUCCEEDED


def test_incomplete_legacy_action_becomes_unknown_and_is_not_replayed() -> None:
    report = migrate_legacy_journal(
        [
            {
                "action_id": "action-lost",
                "event": "intent",
                "tool": "send_email",
                "args": {"to": "user@example.com"},
                "created_at": 20.0,
            },
            {
                "action_id": "action-lost",
                "event": "started",
                "created_at": 21.0,
            },
        ],
        run_id=RUN_ID,
        runtime_contract_digest=CONTRACT.digest,
    )
    assert report.unknown_actions == ("action-lost",)
    assert report.events[-1].event_type == "ActionOutcomeUnknown"
    assert report.events[-1].payload["reason"]


def test_missing_action_id_is_skipped_without_fabricating_a_fact() -> None:
    report = migrate_legacy_journal(
        [{"event": "tool_finished", "tool": "Read", "outcome": {"status": "success"}}],
        run_id=RUN_ID,
        runtime_contract_digest=CONTRACT.digest,
    )
    assert report.skipped_records == 1
    assert report.events == ()
