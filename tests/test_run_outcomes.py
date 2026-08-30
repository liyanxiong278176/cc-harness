from cc_harness.run_outcomes import FailureClass, OutcomeKind, classify_reason, outcome_for_event


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
