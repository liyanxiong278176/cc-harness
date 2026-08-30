from __future__ import annotations

import pytest

from cc_harness.run_model import (
    ActionAttempt,
    ActionStatus,
    ApprovalRequest,
    ApprovalStatus,
    CompletionCandidate,
    CompletionEvidenceError,
    EffectClass,
    EvidenceKind,
    EvidenceRef,
    GoalContract,
    Lease,
    PlanGraph,
    PlanNode,
    PredecessorGateStatus,
    ProgressKind,
    Run,
    RunProgress,
    RunStateMachine,
    RunStatus,
    RuntimeContract,
    predecessor_gate,
)


def runtime_contract() -> RuntimeContract:
    return RuntimeContract("rebuild-test", 1, "tool", "model", "policy", "capability")


def goal() -> GoalContract:
    return GoalContract(
        objective="Implement and verify the sample change",
        acceptance_criteria=("tests pass", "change is scoped"),
        excluded_scope=("secrets/",),
        required_evidence=("test",),
    )


def evidence(kind: EvidenceKind = EvidenceKind.TEST) -> EvidenceRef:
    return EvidenceRef(
        evidence_id="evidence-1",
        kind=kind,
        digest="sha256:evidence",
        source="pytest",
        recorded_at=1.0,
        project_scope="project-1",
    )


def test_goal_runtime_and_progress_round_trip() -> None:
    contract = goal()
    assert GoalContract.from_dict(contract.to_dict()) == contract
    assert RuntimeContract.from_dict(runtime_contract().to_dict()).digest == runtime_contract().digest

    progress = RunProgress(ProgressKind.VERIFICATION_PASSED, "tests passed", (evidence(),), 2)
    assert RunProgress.from_dict(progress.to_dict()) == progress


def test_plan_graph_rejects_cycles_and_enforces_depth_and_ownership() -> None:
    graph = PlanGraph().add_node(
        PlanNode("child-a", "child", depth=1, owned_paths=("src/a.py",), worktree_id="wt-a")
    )
    graph = graph.add_node(
        PlanNode("child-b", "child", depth=1, owned_paths=("docs/",), worktree_id="wt-b")
    )
    assert graph.can_run_parallel(("child-a", "child-b"))
    assert not graph.can_run_parallel(("child-a", "child-a"))

    overlapping = graph.add_node(PlanNode("child-c", "child", depth=1, owned_paths=("src/",)))
    assert not overlapping.can_run_parallel(("child-a", "child-c"))

    with pytest.raises(ValueError, match="depth"):
        PlanGraph(max_child_depth=1).add_node(PlanNode("too-deep", "child", depth=2))

    with pytest.raises(ValueError, match="cycles"):
        PlanGraph(
            nodes=(
                PlanNode("a", "action", depends_on=("b",)),
                PlanNode("b", "action", depends_on=("a",)),
            )
        )


def test_action_attempt_requires_reconciliation_after_unknown() -> None:
    action = ActionAttempt(
        action_id="action-1",
        run_id="run-1",
        tool_name="send_email",
        normalized_args_digest="sha256:args",
        effect_class=EffectClass.EXTERNAL_SIDE_EFFECT,
        contract_digest="sha256:contract",
        actor_kind="worker",
        actor_id="worker-1",
        worker_id="worker-1",
        lease_epoch=3,
    )
    action = action.advance(ActionStatus.PREPARED).advance(ActionStatus.STARTED, started_at=2.0)
    action = action.advance(ActionStatus.OUTCOME_UNKNOWN, error_kind="worker_lost")
    with pytest.raises(ValueError, match="invalid action transition"):
        action.advance(ActionStatus.CANCELLED)
    assert action.advance(ActionStatus.SUCCEEDED).status is ActionStatus.SUCCEEDED


def test_approval_and_lease_are_explicit_values() -> None:
    approval = ApprovalRequest("approval-1", "run-1", "action-1", "sha256:args", ("network",))
    approval = approval.decide(ApprovalStatus.GRANTED, "user-1")
    assert approval.status is ApprovalStatus.GRANTED
    with pytest.raises(ValueError, match="already been decided"):
        approval.decide(ApprovalStatus.REJECTED, "user-2")

    lease = Lease("run-1", "worker-1", 1, acquired_at=10.0, expires_at=20.0)
    assert not lease.is_expired(19.9)
    assert lease.is_expired(20.0)


def test_run_and_candidate_are_model_values() -> None:
    run = Run("run-1", goal(), runtime_contract())
    assert run.status is RunStatus.DRAFT
    assert "budget_exhausted" not in {item.value for item in RunStatus}

    candidate = CompletionCandidate(
        acceptance_criteria=goal().acceptance_criteria,
        evidence=(evidence(),),
    )
    candidate.validate(goal())

    with pytest.raises(CompletionEvidenceError):
        CompletionCandidate(
            acceptance_criteria=goal().acceptance_criteria,
            evidence=(),
        ).validate(goal())


def test_predecessor_gate_matches_decided_matrix() -> None:
    assert predecessor_gate(RunStatus.COMPLETED) is PredecessorGateStatus.READY
    assert predecessor_gate(RunStatus.CANCELLED) is PredecessorGateStatus.INCOMPLETE
    assert predecessor_gate(RunStatus.BLOCKED) is PredecessorGateStatus.WAITING
    assert predecessor_gate(RunStatus.BLOCKED, bypassed=True) is PredecessorGateStatus.BYPASSED


def test_stalled_predecessor_can_be_bypassed_for_durable_follow_up() -> None:
    machine = RunStateMachine()
    assert machine.transition(
        RunStatus.STALLED,
        "PredecessorBypassed",
    ) is RunStatus.STALLED
