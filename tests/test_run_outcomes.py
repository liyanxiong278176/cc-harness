from cc_harness.run_outcomes import (
    FailureClass,
    FailureEvidence,
    OutcomeKind,
    RunOutcome,
    classify_reason,
    diagnose_root_cause,
    outcome_for_event,
)


def test_local_environment_markers_win_over_generic_connection_marker() -> None:
    assert classify_reason("docker connection refused") is FailureClass.ENVIRONMENT_NOT_READY
    assert classify_reason("provider connection reset") is FailureClass.PROVIDER_TRANSPORT


def test_outcome_classifier_keeps_task_failures_non_retryable() -> None:
    outcome = outcome_for_event("RunFailed", {"reason": "assertion failed", "target_status": "failed_terminal"})
    assert outcome is not None
    assert outcome.outcome is OutcomeKind.FAIL
    assert outcome.primary_class is FailureClass.RUNTIME
    assert outcome.retryable is False


def test_environment_block_is_retryable_and_auditable() -> None:
    outcome = outcome_for_event(
        "RunBlocked",
        {"reason": "environment_not_ready: missing verifier dependency"},
    )
    assert outcome is not None
    assert outcome.outcome is OutcomeKind.BLOCKED
    assert outcome.primary_class is FailureClass.ENVIRONMENT_NOT_READY
    assert outcome.retryable is True
    assert outcome.details["reason"].startswith("environment_not_ready")


def test_failure_evidence_round_trips_with_provenance() -> None:
    evidence = FailureEvidence(
        primary_class=FailureClass.ENVIRONMENT_NOT_READY,
        reason="Docker daemon unavailable",
        source="terminal-bench.guard",
        model_phase_started=False,
        verifier_executed=False,
        retryable=True,
        evidence_refs=("attempt/logs/docker-health.json",),
        details={"probe_attempts": 3},
    )

    restored = FailureEvidence.from_dict(evidence.to_dict())
    assert restored == evidence
    assert restored.to_dict()["primary_class"] == "environment_not_ready"


def test_terminal_outcome_carries_failure_evidence_and_payload_context() -> None:
    outcome = outcome_for_event(
        "RunFailed",
        {
            "reason": "provider connection reset",
            "target_status": "failed_recoverable",
            "attempt": 2,
            "recovery_attempt": 1,
            "secondary_causes": ["proxy_restart"],
            "evidence_refs": ["events/17.json"],
            "model_phase_started": True,
        },
    )
    assert outcome is not None
    assert outcome.primary_class is FailureClass.PROVIDER_TRANSPORT
    assert outcome.secondary_causes == ("proxy_restart",)
    assert outcome.attempt == 2
    assert outcome.recovery_attempt == 1
    evidence = outcome.to_failure_evidence()
    assert evidence.primary_class is FailureClass.PROVIDER_TRANSPORT
    assert evidence.model_phase_started is True
    assert evidence.evidence_refs == ("events/17.json",)
    serialized = outcome.to_dict()
    assert serialized["details"]["failure_evidence"]["primary_class"] == "provider_transport"


def test_outcome_from_legacy_details_still_materializes_evidence() -> None:
    outcome = RunOutcome(
        OutcomeKind.FAIL,
        FailureClass.TASK_FAILURE,
        False,
        details={"reason": "assertion failed", "model_phase_started": True},
    )
    evidence = FailureEvidence.from_dict(outcome.to_dict()["details"]["failure_evidence"])
    assert evidence.primary_class is FailureClass.TASK_FAILURE
    assert evidence.reason == "assertion failed"
    assert evidence.model_phase_started is True


def test_root_cause_distinguishes_network_timeout_from_task_deadline() -> None:
    assert diagnose_root_cause("curl: (28) Operation timed out") == "network_dependency"
    assert diagnose_root_cause("LLM stream exceeded the task deadline") == "deadline_exhausted"
    assert diagnose_root_cause("readiness probe: connection refused") == "background_service"
