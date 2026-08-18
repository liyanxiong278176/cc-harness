from __future__ import annotations

import uuid

from cc_harness.run_events import EventActor, RunEvent
from cc_harness.run_model import (
    ActionStatus,
    EffectClass,
    EvidenceKind,
    EvidenceRef,
    GoalContract,
    PlanGraph,
    PlanNode,
    RunStatus,
    RuntimeContract,
)
from cc_harness.run_projection import ProjectionBuilder, RunProjection


RUN_ID = "00000000-0000-0000-0000-000000000101"
ACTOR = EventActor("worker", "worker-1")
CONTRACT = RuntimeContract("projection-test", 1, "sha256:tools", "sha256:model", "sha256:policy", "sha256:cap")
GOAL = GoalContract("ship projection", ("tests pass", "child accepted"))
EVIDENCE = EvidenceRef("evidence-1", EvidenceKind.TEST, "sha256:test", "pytest", 1.0)


def make_event(sequence: int, event_type: str, payload: dict, *, lease_epoch: int = 0) -> RunEvent:
    return RunEvent.create(
        event_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"projection-{sequence}-{event_type}")),
        run_id=RUN_ID,
        sequence=sequence,
        event_type=event_type,
        actor=ACTOR,
        runtime_contract_digest=CONTRACT.digest,
        payload=payload,
        lease_epoch=lease_epoch,
        correlation_id="00000000-0000-0000-0000-000000000199",
    )


def projection_events() -> list[RunEvent]:
    plan = PlanGraph(
        nodes=(PlanNode("child-node", "child", depth=1, owned_paths=("src/",)),),
        revision=1,
    )
    return [
        make_event(
            1,
            "RunCreated",
            {"goal": GOAL.to_dict(), "runtime_contract": CONTRACT.to_dict()},
        ),
        make_event(2, "PlanCreated", {"plan": plan.to_dict()}),
        make_event(
            3,
            "TodoCreated",
            {
                "todo": {
                    "id": "todo-1",
                    "title": "Verify projection",
                    "status": "in_progress",
                    "active_sessions": [RUN_ID],
                }
            },
        ),
        make_event(4, "RunQueued", {}),
        make_event(5, "RunClaimed", {"worker_id": "worker-1"}, lease_epoch=1),
        make_event(
            6,
            "ActionPlanned",
            {
                "action_id": "action-1",
                "attempt": 1,
                "tool_name": "Write",
                "effect_class": EffectClass.WORKSPACE_MUTATION.value,
                "normalized_args_digest": "sha256:args",
                "contract_digest": "sha256:tool-contract",
                "worker_id": "worker-1",
            },
            lease_epoch=1,
        ),
        make_event(7, "ActionPrepared", {"action_id": "action-1", "attempt": 1}, lease_epoch=1),
        make_event(8, "ActionStarted", {"action_id": "action-1", "attempt": 1}, lease_epoch=1),
        make_event(
            9,
            "ActionSucceeded",
            {
                "action_id": "action-1",
                "attempt": 1,
                "result_artifact": "sha256:result",
                "modified_paths": ["src/main.py"],
            },
            lease_epoch=1,
        ),
        make_event(
            10,
            "VerificationRecorded",
            {"evidence": [EVIDENCE.to_dict()], "ok": True},
            lease_epoch=1,
        ),
        make_event(
            11,
            "ChildRunCreated",
            {"child_run_id": "child-1", "node_id": "child-node"},
            lease_epoch=1,
        ),
        make_event(
            12,
            "ChildRunClaimed",
            {"child_run_id": "child-1", "worker_id": "worker-child"},
            lease_epoch=1,
        ),
        make_event(
            13,
            "ChildCandidateSubmitted",
            {
                "child_run_id": "child-1",
                "candidate_commit": "commit-child",
                "diff_digest": "sha256:diff",
                "modified_paths": ["src/child.py"],
            },
            lease_epoch=1,
        ),
        make_event(
            14,
            "ChildCandidateAccepted",
            {"child_run_id": "child-1"},
            lease_epoch=1,
        ),
        make_event(
            15,
            "CompletionAccepted",
            {
                "acceptance_criteria": list(GOAL.acceptance_criteria),
                "evidence": [EVIDENCE.to_dict()],
            },
            lease_epoch=1,
        ),
    ]


def test_projection_rebuilds_run_todo_plan_working_and_child_views() -> None:
    events = projection_events()
    projection = ProjectionBuilder().rebuild(events)

    assert projection.status is RunStatus.COMPLETED
    assert projection.sequence == 15
    assert projection.goal == GOAL
    assert projection.plan.nodes[0].node_id == "child-node"
    assert projection.todos[0].status == "in_progress"
    assert projection.working_state.modified_paths == ("src/main.py",)
    assert projection.working_state.last_verification_ok is True
    assert projection.actions[0].status is ActionStatus.SUCCEEDED
    assert projection.children[0].status == "accepted"
    assert projection.children[0].diff_digest == "sha256:diff"
    assert projection.evidence[0].digest == "sha256:test"


def test_snapshot_incremental_replay_has_same_digest_as_full_replay() -> None:
    events = projection_events()
    builder = ProjectionBuilder()
    full = builder.rebuild(events)
    snapshot = builder.rebuild(events[:8])
    resumed = builder.rebuild(events[8:], snapshot=snapshot)
    assert resumed.digest == full.digest
    assert RunProjection.from_dict(resumed.to_dict()).digest == full.digest


def test_reducer_does_not_mutate_event_payloads() -> None:
    events = projection_events()
    before = [event.to_dict() for event in events]
    ProjectionBuilder().rebuild(events)
    assert [event.to_dict() for event in events] == before


def test_approval_and_follow_up_are_projection_records() -> None:
    events = [
        make_event(
            1,
            "RunCreated",
            {"goal": GOAL.to_dict(), "runtime_contract": CONTRACT.to_dict()},
        ),
        make_event(2, "RunQueued", {}),
        make_event(3, "RunClaimed", {"worker_id": "worker-1"}, lease_epoch=1),
        make_event(
            4,
            "ApprovalRequested",
            {
                "approval_id": "approval-1",
                "action_id": "action-1",
                "action_args_digest": "sha256:args",
                "scope": ["network"],
            },
            lease_epoch=1,
        ),
        make_event(
            5,
            "ApprovalGranted",
            {"approval_id": "approval-1", "action_args_digest": "sha256:args"},
            lease_epoch=1,
        ),
        make_event(
            6,
            "FollowUpQueued",
            {
                "follow_up_run_id": "follow-up-1",
                "predecessor_run_id": RUN_ID,
                "message_artifact": "sha256:message",
                "gate": "waiting",
            },
            lease_epoch=1,
        ),
    ]
    projection = ProjectionBuilder().rebuild(events)
    assert projection.status is RunStatus.QUEUED
    assert projection.approvals[0].status.value == "granted"
    assert projection.queue[0].predecessor_run_id == RUN_ID
