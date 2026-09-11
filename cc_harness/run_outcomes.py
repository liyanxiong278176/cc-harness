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
    # ``submission_ready`` is an explicit boundary for adapters that hand a
    # workspace to an external verifier (for example Harbor).  It is not a
    # second success state: the verifier must still record the final grade.
    SUBMISSION_READY = "submission_ready"
    FAIL = "fail"
    BLOCKED = "blocked"
    STALLED = "stalled"
    CANCELLED = "cancelled"
    UNKNOWN = "outcome_unknown"

    @property
    def terminal(self) -> bool:
        return self in {
            OutcomeKind.PASS,
            OutcomeKind.SUBMISSION_READY,
            OutcomeKind.FAIL,
            OutcomeKind.BLOCKED,
            OutcomeKind.STALLED,
            OutcomeKind.CANCELLED,
            OutcomeKind.UNKNOWN,
        }


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
class FailureEvidence:
    """Structured, provenance-aware evidence for a terminal outcome.

    Runtime, benchmark adapters, and reports used to carry a free-form
    ``failure_class`` plus unrelated diagnostic fields.  Keeping the primary
    class, the reason, execution boundaries, and evidence references together
    makes it possible to distinguish a task failure from an infrastructure
    failure without relying on model prose or a single truncated exception.
    ``details`` is intentionally extensible so adapters can retain raw facts
    without changing the canonical envelope.
    """

    primary_class: FailureClass | str
    reason: str
    source: str = "runtime"
    model_phase_started: bool = False
    verifier_executed: bool = False
    retryable: bool = False
    evidence_refs: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.reason or "").strip():
            raise ValueError("failure evidence reason is required")
        if not str(self.source or "").strip():
            raise ValueError("failure evidence source is required")
        object.__setattr__(
            self,
            "evidence_refs",
            tuple(str(item) for item in self.evidence_refs if str(item).strip()),
        )
        object.__setattr__(self, "details", dict(self.details or {}))

    def to_dict(self) -> dict[str, Any]:
        primary = (
            self.primary_class.value
            if isinstance(self.primary_class, Enum)
            else str(self.primary_class)
        )
        return {
            "primary_class": primary,
            "reason": str(self.reason),
            "source": str(self.source),
            "model_phase_started": bool(self.model_phase_started),
            "verifier_executed": bool(self.verifier_executed),
            "retryable": bool(self.retryable),
            "evidence_refs": list(self.evidence_refs),
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FailureEvidence":
        if not isinstance(data, Mapping):
            raise ValueError("failure evidence must be an object")
        raw_class = str(data.get("primary_class") or FailureClass.RUNTIME.value)
        try:
            primary: FailureClass | str = FailureClass(raw_class)
        except ValueError:
            primary = raw_class
        refs = data.get("evidence_refs") or ()
        if isinstance(refs, (str, bytes)):
            refs = (refs,)
        return cls(
            primary_class=primary,
            reason=str(data.get("reason") or "failure evidence"),
            source=str(data.get("source") or "runtime"),
            model_phase_started=bool(data.get("model_phase_started", False)),
            verifier_executed=bool(data.get("verifier_executed", False)),
            retryable=bool(data.get("retryable", False)),
            evidence_refs=tuple(str(item) for item in refs),
            details=dict(data.get("details") or {}),
        )


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

    @property
    def is_success(self) -> bool:
        return self.outcome in {OutcomeKind.PASS, OutcomeKind.SUBMISSION_READY}

    def to_failure_evidence(self, *, source: str = "runtime") -> FailureEvidence:
        """Return the canonical evidence envelope represented by this outcome."""

        nested = self.details.get("failure_evidence") if isinstance(self.details, Mapping) else None
        if isinstance(nested, Mapping):
            try:
                return FailureEvidence.from_dict(nested)
            except ValueError:
                pass
        reason = str(self.details.get("reason") or self.outcome)
        details = dict(self.details or {})
        details.pop("failure_evidence", None)
        return FailureEvidence(
            primary_class=self.primary_class,
            reason=reason,
            source=source,
            model_phase_started=bool(details.pop("model_phase_started", False)),
            verifier_executed=bool(details.pop("verifier_executed", False)),
            retryable=self.retryable,
            evidence_refs=self.evidence_refs,
            details=details,
        )

    @classmethod
    def from_status(
        cls,
        status: Any,
        *,
        reason: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> "RunOutcome | None":
        """Map a durable lifecycle status to one canonical outcome.

        The mapping is deliberately kept in one place.  Older callers often
        interpreted ``stalled``/``blocked`` differently (and the benchmark
        adapter treated a recoverable runtime boundary as a process failure),
        which made the same run appear to have multiple terminal meanings.
        """

        value = getattr(status, "value", status)
        value = str(value or "").strip().casefold()
        reason_text = str(reason or "")
        if value == "completed":
            return cls(OutcomeKind.PASS, FailureClass.NONE, False, details=dict(details or {}))
        if value in {
            "draft",
            "queued",
            "running",
            "awaiting_approval",
            "waiting_on_predecessor",
            "cancel_requested",
        }:
            return None
        if value == "stalled":
            return cls(
                OutcomeKind.STALLED,
                FailureClass.NO_PROGRESS,
                True,
                details={**dict(details or {}), "reason": reason_text or "stalled"},
            )
        if value == "blocked":
            primary = classify_reason(reason_text or "blocked", event_type="RunBlocked")
            return cls(
                OutcomeKind.BLOCKED,
                primary,
                primary
                in {
                    FailureClass.ENVIRONMENT_NOT_READY,
                    FailureClass.PROVIDER_TRANSPORT,
                    FailureClass.OUTCOME_UNKNOWN,
                    FailureClass.NO_PROGRESS,
                },
                details={**dict(details or {}), "reason": reason_text or "blocked"},
            )
        if value == "failed_recoverable":
            primary = classify_reason(reason_text or "run failed", event_type="RunFailed")
            return cls(
                OutcomeKind.FAIL,
                primary,
                True,
                details={
                    **dict(details or {}),
                    "reason": reason_text or "run failed",
                    "target_status": value,
                },
            )
        if value == "failed_terminal":
            primary = classify_reason(reason_text or "run failed", event_type="RunFailed")
            return cls(
                OutcomeKind.FAIL,
                primary,
                False,
                details={
                    **dict(details or {}),
                    "reason": reason_text or "run failed",
                    "target_status": value,
                },
            )
        if value == "cancelled":
            return cls(
                OutcomeKind.CANCELLED,
                FailureClass.CANCELLED,
                False,
                details={**dict(details or {}), "reason": reason_text or "cancelled"},
            )
        if value:
            return cls(
                OutcomeKind.UNKNOWN,
                FailureClass.OUTCOME_UNKNOWN,
                True,
                details={**dict(details or {}), "reason": reason_text or value},
            )
        return None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunOutcome":
        if not isinstance(data, Mapping):
            raise ValueError("run outcome must be an object")
        outcome = str(data.get("outcome") or "")
        primary = str(data.get("primary_class") or FailureClass.NONE.value)
        if not outcome:
            raise ValueError("run outcome is required")
        if not primary:
            raise ValueError("run outcome primary_class is required")
        try:
            outcome_value: OutcomeKind | str = OutcomeKind(outcome)
        except ValueError:
            outcome_value = outcome
        try:
            primary_value: FailureClass | str = FailureClass(primary)
        except ValueError:
            primary_value = primary
        return cls(
            outcome=outcome_value,
            primary_class=primary_value,
            retryable=bool(data.get("retryable", False)),
            secondary_causes=tuple(str(item) for item in data.get("secondary_causes") or ()),
            evidence_refs=tuple(str(item) for item in data.get("evidence_refs") or ()),
            attempt=max(0, int(data.get("attempt", 1))),
            recovery_attempt=max(0, int(data.get("recovery_attempt", 0))),
            details=dict(data.get("details") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        details = dict(self.details)
        if not isinstance(details.get("failure_evidence"), Mapping):
            details["failure_evidence"] = self.to_failure_evidence().to_dict()
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
            "details": details,
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
    if any(
        marker in text
        for marker in (
            "environment_not_ready",
            "docker daemon",
            "docker connection",
            "docker desktop",
            "address pool",
            "fully subnetted",
            "cannot allocate memory",
            "errno 12",
            "out of memory",
            "no space left on device",
            "verifier bootstrap",
            "missing module",
            "modulenotfounderror",
            "dependency",
            "import pytest",
        )
    ):
        return FailureClass.ENVIRONMENT_NOT_READY
    if any(marker in text for marker in ("outcome unknown", "outcome_unknown", "unknown side effect")):
        return FailureClass.OUTCOME_UNKNOWN
    # Provider transport failures are narrower than a generic ``connection``
    # word.  Keep a local command/network outage from being mislabeled as a
    # provider billable retry, while still recognizing OpenAI-compatible HTTP
    # gateways and proxy stream errors.
    if any(
        marker in text
        for marker in (
            "apiconnectionerror",
            "apitimeouterror",
            "provider connection",
            "provider proxy",
            "provider transport",
            "read timeout",
            "connection timed out",
            "connection timeout",
            "connection reset",
            "connection closed",
            "status code 429",
            "status code 500",
            "status code 502",
            "status code 503",
            "status code 504",
            "transport",
            "invalid api key",
            "authentication failed",
            "unauthorized",
            "insufficient balance",
            "quota exceeded",
            "rate limit exceeded",
        )
    ):
        return FailureClass.PROVIDER_TRANSPORT
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


def diagnose_root_cause(reason: str, *, phase: str = "") -> str:
    """Return a fine-grained, non-scoring diagnosis for a runtime failure.

    ``FailureClass`` is intentionally small and stable because benchmark
    reports consume it as a public compatibility field.  It is not specific
    enough to explain why a task failed, though: a timeout may be a provider
    stall, a local service that never became ready, or a hard task deadline.
    This helper adds a diagnostic label without changing the official class or
    reward.  The matcher only uses runtime/tool evidence, never model prose.
    """

    text = f"{phase} {reason}".casefold()
    explicit_deadline = any(
        marker in text
        for marker in (
            "deadline_exhausted",
            "task deadline",
            "deadline exceeded",
            "time limit",
        )
    )
    network_context = any(
        marker in text
        for marker in (
            "could not resolve",
            "name resolution",
            "dns",
            "network is unreachable",
            "no route to host",
            "tls",
            "ssl",
            "proxy",
            "http ",
            "status code",
            "curl",
            "wget",
            "apt-get",
            "pip install",
            "uv ",
            "npm install",
            "git clone",
            "git fetch",
            "connection reset",
            "connection closed",
            "connection refused",
            "read timeout",
        )
    )
    # A network read timeout is a dependency outage, not proof that the whole
    # task deadline was consumed.  Keep generic command/LLM timeouts mapped to
    # deadline_exhausted, while explicit deadline wording always wins.
    if explicit_deadline or (
        any(marker in text for marker in ("timed out", "timeout"))
        and not network_context
    ):
        return "deadline_exhausted"
    if any(
        marker in text
        for marker in (
            "out of memory",
            "cannot allocate memory",
            "errno 12",
            "oom",
            "no space left on device",
            "address pool",
            "fully subnetted",
        )
    ):
        return "resource_exhausted"
    if any(
        marker in text
        for marker in (
            "reasoning_content",
            "tool_call replay",
            "provider_protocol",
            "invalid tool call",
            "malformed response",
        )
    ):
        return "provider_protocol"
    if any(
        marker in text
        for marker in (
            "database is locked",
            "sqlite",
            "projection cursor race",
            "busy timeout",
            "concurrent write",
        )
    ):
        return "runtime_persistence"
    service_context = any(
        marker in text
        for marker in (
            "readiness",
            "health probe",
            "service_status",
            "not listening",
            "port is not",
            "daemon is not",
            "managed service",
        )
    )
    if service_context or (
        "connection refused" in text
        and any(marker in text for marker in ("localhost", "127.0.0.1", "port", "endpoint"))
    ):
        return "background_service"
    if any(
        marker in text
        for marker in (
            "docker",
            "opensandbox",
            "sandbox unavailable",
            "container runtime",
            "containerd",
        )
    ):
        return "container_runtime"
    if network_context or any(
        marker in text
        for marker in (
            "temporary failure in name resolution",
            "http 429",
            "status code 429",
            "status code 5",
            "502",
            "503",
            "504",
            "curl:",
            "wget:",
        )
    ):
        return "network_dependency"
    if any(
        marker in text
        for marker in (
            "no module named",
            "modulenotfounderror",
            "command not found",
            "missing dependency",
            "dependency",
            "import error",
        )
    ):
        return "dependency_missing"
    if any(marker in text for marker in ("permission denied", "access denied", "approval")):
        return "policy_or_permission"
    if any(marker in text for marker in ("no progress", "stalled", "no output")):
        return "no_progress"
    if any(
        marker in text
        for marker in (
            "assertionerror",
            "assertion failed",
            "test failed",
            "tests failed",
            "official harbor grader rejected",
        )
    ):
        return "task_assertion"
    return "unknown"


def outcome_for_event(event_type: str, payload: Mapping[str, Any]) -> RunOutcome | None:
    """Build a canonical outcome for a terminal lifecycle event."""

    payload_details = payload.get("details")
    details = dict(payload_details) if isinstance(payload_details, Mapping) else {}
    refs_value = payload.get("evidence_refs") or ()
    if isinstance(refs_value, (str, bytes)):
        refs_value = (refs_value,)
    refs = tuple(str(item) for item in refs_value if str(item).strip())
    causes_value = payload.get("secondary_causes") or ()
    if isinstance(causes_value, (str, bytes)):
        causes_value = (causes_value,)
    causes = tuple(str(item) for item in causes_value if str(item).strip())

    def nonnegative_int(value: Any, default: int) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return default

    def with_evidence(
        failure: FailureClass | str,
        reason: str,
        *,
        retryable: bool,
        verifier_executed: bool = False,
        source: str = "runtime",
    ) -> dict[str, Any]:
        existing = payload.get("failure_evidence")
        if not isinstance(existing, Mapping):
            existing = details.get("failure_evidence")
        if isinstance(existing, Mapping):
            try:
                evidence = FailureEvidence.from_dict(existing)
            except ValueError:
                evidence = FailureEvidence(
                    primary_class=failure,
                    reason=reason,
                    source=source,
                    model_phase_started=bool(payload.get("model_phase_started")),
                    verifier_executed=verifier_executed,
                    retryable=retryable,
                    evidence_refs=refs,
                )
        else:
            evidence = FailureEvidence(
                primary_class=failure,
                reason=reason,
                source=source,
                model_phase_started=bool(payload.get("model_phase_started")),
                verifier_executed=verifier_executed,
                retryable=retryable,
                evidence_refs=refs,
            )
        details["reason"] = reason
        details["failure_evidence"] = evidence.to_dict()
        return details

    if event_type == "CompletionAccepted":
        reason = "completion accepted"
        evidence_details = with_evidence(
            FailureClass.NONE,
            reason,
            retryable=False,
            verifier_executed=bool(payload.get("verifier_executed")),
            source="completion_gate",
        )
        return RunOutcome(
            OutcomeKind.PASS,
            FailureClass.NONE,
            False,
            evidence_refs=refs,
            details=evidence_details,
        )
    if event_type == "RunStalled":
        reason = str(payload.get("reason") or "stalled")
        return RunOutcome(
            OutcomeKind.STALLED,
            FailureClass.NO_PROGRESS,
            True,
            secondary_causes=causes,
            evidence_refs=refs,
            attempt=nonnegative_int(payload.get("attempt"), 1),
            recovery_attempt=nonnegative_int(payload.get("recovery_attempt"), 0),
            details=with_evidence(FailureClass.NO_PROGRESS, reason, retryable=True),
        )
    if event_type == "RunBlocked":
        reason = str(payload.get("reason") or "blocked")
        failure = classify_reason(reason, event_type=event_type)
        retryable = failure in {
            FailureClass.ENVIRONMENT_NOT_READY,
            FailureClass.PROVIDER_TRANSPORT,
            FailureClass.OUTCOME_UNKNOWN,
            FailureClass.NO_PROGRESS,
        }
        return RunOutcome(
            OutcomeKind.BLOCKED,
            failure,
            retryable,
            secondary_causes=causes,
            evidence_refs=refs,
            attempt=nonnegative_int(payload.get("attempt"), 1),
            recovery_attempt=nonnegative_int(payload.get("recovery_attempt"), 0),
            details=with_evidence(failure, reason, retryable=retryable),
        )
    if event_type == "RunFailed":
        reason = str(payload.get("reason") or "run failed")
        failure = classify_reason(reason, event_type=event_type)
        target = str(payload.get("target_status") or "failed_terminal")
        retryable = target == "failed_recoverable"
        details["target_status"] = target
        return RunOutcome(
            OutcomeKind.FAIL,
            failure,
            retryable,
            secondary_causes=causes,
            evidence_refs=refs,
            attempt=nonnegative_int(payload.get("attempt"), 1),
            recovery_attempt=nonnegative_int(payload.get("recovery_attempt"), 0),
            details=with_evidence(failure, reason, retryable=retryable),
        )
    if event_type == "RunCancelled":
        reason = str(payload.get("reason") or "cancelled")
        return RunOutcome(
            OutcomeKind.CANCELLED,
            FailureClass.CANCELLED,
            False,
            secondary_causes=causes,
            evidence_refs=refs,
            attempt=nonnegative_int(payload.get("attempt"), 1),
            recovery_attempt=nonnegative_int(payload.get("recovery_attempt"), 0),
            details=with_evidence(FailureClass.CANCELLED, reason, retryable=False),
        )
    return None


def outcome_for_status(status: Any, *, reason: str = "") -> RunOutcome | None:
    """Public status-to-outcome adapter used by projections and clients."""

    return RunOutcome.from_status(status, reason=reason)


__all__ = [
    "FailureEvidence",
    "FailureClass",
    "OutcomeKind",
    "RunOutcome",
    "classify_reason",
    "diagnose_root_cause",
    "outcome_for_event",
    "outcome_for_status",
]
