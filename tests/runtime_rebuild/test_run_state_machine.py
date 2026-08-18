from __future__ import annotations

import pytest

from cc_harness.run_model import (
    CompletionCandidate,
    CompletionEvidenceError,
    EvidenceKind,
    EvidenceRef,
    GoalContract,
    InvalidRunTransition,
    RunStateMachine,
    RunStatus,
)


def completion() -> CompletionCandidate:
    evidence = EvidenceRef("e-1", EvidenceKind.TEST, "sha256:test", "pytest", 1.0)
    return CompletionCandidate(("tests pass",), (evidence,))


def test_lifecycle_whitelist_covers_normal_long_running_path() -> None:
    machine = RunStateMachine()
    status = RunStatus.DRAFT
    status = machine.transition(status, "RunQueued")
    status = machine.transition(status, "RunClaimed")
    status = machine.transition(status, "RunSegmentStarted")
    status = machine.transition(status, "RunSegmentFinished")
    assert status is RunStatus.RUNNING

    status = machine.transition(
        status,
        "CompletionAccepted",
        completion=completion(),
        goal=GoalContract("ship", ("tests pass",)),
    )
    assert status is RunStatus.COMPLETED


@pytest.mark.parametrize(
    ("current", "event", "expected"),
    [
        (RunStatus.RUNNING, "ApprovalRequested", RunStatus.AWAITING_APPROVAL),
        (RunStatus.AWAITING_APPROVAL, "ApprovalGranted", RunStatus.QUEUED),
        (RunStatus.AWAITING_APPROVAL, "ApprovalRejected", RunStatus.BLOCKED),
        (RunStatus.AWAITING_APPROVAL, "RunCancelled", RunStatus.CANCELLED),
        (RunStatus.RUNNING, "RunStalled", RunStatus.STALLED),
        (RunStatus.RUNNING, "InterruptRequested", RunStatus.CANCEL_REQUESTED),
        (RunStatus.CANCEL_REQUESTED, "RunCancelled", RunStatus.CANCELLED),
        (RunStatus.FAILED_RECOVERABLE, "RunResumed", RunStatus.QUEUED),
    ],
)
def test_decided_transitions(current: RunStatus, event: str, expected: RunStatus) -> None:
    assert RunStateMachine().transition(current, event) is expected


def test_completion_cannot_be_declared_without_evidence_or_from_wrong_state() -> None:
    machine = RunStateMachine()
    goal = GoalContract("ship", ("tests pass",))
    with pytest.raises(CompletionEvidenceError):
        machine.transition(
            RunStatus.RUNNING,
            "CompletionAccepted",
            completion=CompletionCandidate(("tests pass",), ()),
            goal=goal,
        )
    with pytest.raises(InvalidRunTransition):
        machine.transition(RunStatus.QUEUED, "CompletionAccepted", completion=completion(), goal=goal)
    with pytest.raises(InvalidRunTransition):
        machine.transition(RunStatus.RUNNING, "RunCancelled")


def test_unknown_or_terminal_transitions_are_rejected() -> None:
    machine = RunStateMachine()
    with pytest.raises(InvalidRunTransition):
        machine.transition(RunStatus.RUNNING, "NotARealEvent")
    with pytest.raises(InvalidRunTransition):
        machine.transition(RunStatus.COMPLETED, "RunResumed")
    with pytest.raises(InvalidRunTransition):
        machine.transition(RunStatus.CANCELLED, "RunQueued")
