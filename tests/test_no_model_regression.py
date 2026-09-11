"""Staged regression coverage for the runtime control plane.

These tests deliberately use only in-process domain objects and temporary
files.  They are the cheap gate that can run before any provider or benchmark
invocation: a runtime change must not need a live model to prove its safety
invariants.
"""

from __future__ import annotations

from types import SimpleNamespace

from cc_harness.executor import ManagedServiceContract
from cc_harness.loop_control import (
    ArtifactRequirement,
    CompletionContract,
    StallController,
    TaskContract,
    artifact_validation_issues,
)
from cc_harness.run_model import (
    CompletionCandidate,
    CompletionGate,
    EvidenceKind,
    EvidenceRef,
    GoalContract,
)
from cc_harness.run_outcomes import FailureClass, FailureEvidence, outcome_for_event
from cc_harness.run_telemetry import aggregate_model_usage
from eval.cc_only.runner import RetryBudget


def test_completion_and_artifact_gates_are_provider_free(tmp_path) -> None:
    output = tmp_path / "dist" / "manifest.json"
    output.parent.mkdir()
    output.write_text('{"ok": true}\n', encoding="utf-8")
    requirement = ArtifactRequirement(
        path="dist/manifest.json",
        min_bytes=2,
    )
    contract = CompletionContract(
        required_paths=(requirement.path,),
        required_artifacts=(requirement,),
        verification_commands=("python -m pytest -q",),
    )
    task = TaskContract.from_goal(
        GoalContract("ship an artifact", ("manifest exists",)),
        completion=contract,
    )
    evidence = EvidenceRef(
        "manifest-test",
        EvidenceKind.TEST,
        "sha256:" + "a" * 64,
        "python -m pytest -q",
        1.0,
    )
    candidate = CompletionCandidate(("manifest exists",), (evidence,))
    gate = CompletionGate().evaluate(candidate, GoalContract("ship an artifact", ("manifest exists",)), strict_digests=True)
    assert gate.accepted is True
    assert artifact_validation_issues(tmp_path, task.completion.required_artifacts) == ()
    assert artifact_validation_issues(
        tmp_path,
        (ArtifactRequirement("dist/missing.json"),),
    ) == ("required artifact is missing: dist/missing.json",)


def test_task_contract_round_trip_preserves_executable_obligations() -> None:
    requirement = ArtifactRequirement("/app/result.json", min_bytes=4)
    task = TaskContract(
        objective="complete task",
        acceptance_criteria=("result is valid",),
        constraints=("do not modify tests",),
        completion=CompletionContract(
            required_paths=("/app/result.json",),
            required_artifacts=(requirement,),
            verification_commands=("pytest -q",),
            service_health_commands=("curl -fsS http://127.0.0.1:8000/health",),
            environment_retry_limit=10,
            deadline_seconds=900,
        ),
    )
    restored = TaskContract.from_dict(task.to_dict())
    assert restored == task


def test_managed_service_contract_is_explicit_and_serializable() -> None:
    contract = ManagedServiceContract(
        name="api",
        command="python -m app",
        readiness_command="curl -fsS http://127.0.0.1:8000/health",
        health_command="curl -fsS http://127.0.0.1:8000/health",
        readiness_timeout_s=20,
        health_timeout_s=3,
        shutdown_timeout_s=4,
    )
    assert ManagedServiceContract(**contract.to_dict()) == contract


def test_retry_budget_is_bounded_by_attempts_and_deadline() -> None:
    budget = RetryBudget(max_attempts=2, deadline_seconds=10, started_at=100)
    assert budget.can_retry(0, now=105)
    assert not budget.can_retry(2, now=105)
    assert not budget.can_retry(1, now=111)
    assert budget.exhaustion_reason(2, now=105) == "max_attempts"
    assert budget.exhaustion_reason(1, now=111) == "deadline"


def test_lossless_outcome_evidence_and_failure_classification() -> None:
    outcome = outcome_for_event(
        "RunBlocked",
        {
            "reason": "Docker daemon unavailable",
            "attempt": 3,
            "model_phase_started": False,
            "evidence_refs": ["guard/docker-health.json"],
        },
    )
    assert outcome is not None
    assert outcome.primary_class is FailureClass.ENVIRONMENT_NOT_READY
    envelope = outcome.to_dict()["details"]["failure_evidence"]
    assert envelope["primary_class"] == "environment_not_ready"
    assert envelope["evidence_refs"] == ["guard/docker-health.json"]
    assert FailureEvidence.from_dict(envelope).verifier_executed is False


def test_per_invocation_telemetry_has_direct_cost_and_cache_facts() -> None:
    event = SimpleNamespace(
        event_type="ModelInvocationFinished",
        payload={
            "invocation_id": "call-1",
            "status": "succeeded",
            "usage": {
                "provider": "example",
                "model": "model-a",
                "input_tokens": 100,
                "cache_read_input_tokens": 75,
                "output_tokens": 5,
                "model_calls": 1,
                "reported_cost": 0.02,
                "reported_cost_currency": "USD",
            },
        },
    )
    summary = aggregate_model_usage([event])
    assert summary["invocations"][0]["invocation_id"] == "call-1"
    assert summary["invocations"][0]["cache_hit_ratio"] == 0.75
    assert summary["invocations"][0]["cost_status"] == "reported"
    assert summary["reported_cost"] == 0.02


def test_repeated_action_requires_replan_before_repeating() -> None:
    controller = StallController(repeat_threshold=2)
    controller.observe("same", action_signature="run-tests")
    decision = controller.observe("same", action_signature="run-tests")
    assert decision.replan_required is True
    assert controller.should_block("run-tests") is True
    assert controller.should_block("inspect-logs") is False
    assert controller.replan_count == 1
