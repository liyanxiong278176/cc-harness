"""Canonical terminal outcome vocabulary for durable Runs.

The event stream is the source of truth.  This module only provides a small,
provider-neutral vocabulary and deterministic classification helpers so the
CLI, TUI, benchmark adapters, and reports do not each invent their own
interpretation of a terminal failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class OutcomeKind(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    STALLED = "stalled"
    CANCELLED = "cancelled"
    UNKNOWN = "outcome_unknown"


class FailureClass(str, Enum):
    NONE = "none"
    TASK_FAILURE = "task_failure"
    PROVIDER_PROTOCOL = "provider_protocol"
    PROVIDER_TRANSPORT = "provider_transport"
    VERIFIER_INFRASTRUCTURE = "verifier_infrastructure"
    ENVIRONMENT_NOT_READY = "environment_not_ready"
    OUTCOME_UNKNOWN = "outcome_unknown"
    PERMISSION = "permission"
    APPROVAL_REQUIRED = "approval_required"
    NO_PROGRESS = "no_progress"
    RUNTIME = "runtime"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RunOutcome:
    """A durable, immutable summary of one terminal boundary."""

    outcome: OutcomeKind | str
    primary_class: FailureClass | str = FailureClass.NONE
    retryable: bool = False
    secondary_causes: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    attempt: int = 1
    recovery_attempt: int = 0
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value if isinstance(self.outcome, Enum) else str(self.outcome),
            "primary_class": self.primary_class.value
            if isinstance(self.primary_class, Enum)
            else str(self.primary_class),
            "retryable": bool(self.retryable),
            "secondary_causes": list(self.secondary_causes),
            "evidence_refs": list(self.evidence_refs),
            "attempt": int(self.attempt),
            "recovery_attempt": int(self.recovery_attempt),
            "details": dict(self.details),
        }


def classify_reason(reason: str, *, event_type: str = "") -> FailureClass:
    """Classify a runtime reason without inspecting model prose.

    The classifier intentionally uses stable runtime markers only.  Unknown
    reasons remain ``runtime``/``task_failure`` rather than being silently
    treated as infrastructure failures.
    """

    text = f"{event_type} {reason}".casefold()
    if any(marker in text for marker in ("provider_protocol", "reasoning_content", "tool_call replay")):
        return FailureClass.PROVIDER_PROTOCOL
    if any(marker in text for marker in ("environment_not_ready", "docker", "verifier", "dependency", "import pytest")):
        return FailureClass.ENVIRONMENT_NOT_READY
    # Classify local harness/container failures before the generic transport
    # markers.  A Docker daemon connection error is an environment problem;
    # a provider HTTP connection error remains provider_transport below.
    if any(marker in text for marker in ("timeout", "connection", "connection reset", "503", "429", "transport")):
        return FailureClass.PROVIDER_TRANSPORT
    if any(marker in text for marker in ("outcome unknown", "outcome_unknown", "unknown side effect")):
        return FailureClass.OUTCOME_UNKNOWN
    if any(marker in text for marker in ("approval", "awaiting approval")):
        return FailureClass.APPROVAL_REQUIRED
    if any(marker in text for marker in ("permission", "security")):
        return FailureClass.PERMISSION
    if any(marker in text for marker in ("no verifiable progress", "no progress", "stalled")):
        return FailureClass.NO_PROGRESS
    if event_type == "RunFailed" or "worker exception" in text:
        return FailureClass.RUNTIME
    if event_type == "RunCancelled" or "cancel" in text:
        return FailureClass.CANCELLED
    return FailureClass.TASK_FAILURE


def outcome_for_event(event_type: str, payload: Mapping[str, Any]) -> RunOutcome | None:
    """Build a canonical outcome for a terminal lifecycle event."""

    if event_type == "CompletionAccepted":
        refs = tuple(str(item) for item in payload.get("evidence_refs") or ())
        return RunOutcome(OutcomeKind.PASS, FailureClass.NONE, False, evidence_refs=refs)
    if event_type == "RunStalled":
        reason = str(payload.get("reason") or "stalled")
        return RunOutcome(OutcomeKind.STALLED, FailureClass.NO_PROGRESS, True, details={"reason": reason})
    if event_type == "RunBlocked":
        reason = str(payload.get("reason") or "blocked")
        failure = classify_reason(reason, event_type=event_type)
        return RunOutcome(OutcomeKind.BLOCKED, failure, failure in {
            FailureClass.ENVIRONMENT_NOT_READY,
            FailureClass.PROVIDER_TRANSPORT,
            FailureClass.OUTCOME_UNKNOWN,
            FailureClass.NO_PROGRESS,
        }, details={"reason": reason})
    if event_type == "RunFailed":
        reason = str(payload.get("reason") or "run failed")
        failure = classify_reason(reason, event_type=event_type)
        target = str(payload.get("target_status") or "failed_terminal")
        return RunOutcome(
            OutcomeKind.FAIL,
            failure,
            target == "failed_recoverable",
            details={"reason": reason, "target_status": target},
        )
    if event_type == "RunCancelled":
        reason = str(payload.get("reason") or "cancelled")
        return RunOutcome(OutcomeKind.CANCELLED, FailureClass.CANCELLED, False, details={"reason": reason})
    return None


__all__ = ["FailureClass", "OutcomeKind", "RunOutcome", "classify_reason", "outcome_for_event"]
