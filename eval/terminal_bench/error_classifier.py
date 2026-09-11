"""Structured error classification facade for Terminal-Bench evidence."""

from __future__ import annotations

from dataclasses import dataclass

from cc_harness.run_outcomes import FailureClass, classify_reason
from eval.cc_only.infrastructure import transient_infrastructure_text, verifier_execution_observed


@dataclass(frozen=True)
class ErrorClassification:
    category: str
    severity: str
    model_calls_consumed: bool
    verifier_executed: bool
    recommended_guard: str
    recovery_hints: tuple[str, ...] = ()
    # Canonical runtime class used by RunOutcome/reporting.  ``category`` is
    # retained as the benchmark-facing compatibility label (docker/network/
    # verifier/etc.).
    canonical_class: str = FailureClass.TASK_FAILURE.value

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "severity": self.severity,
            "model_calls_consumed": bool(self.model_calls_consumed),
            "verifier_executed": bool(self.verifier_executed),
            "recommended_guard": self.recommended_guard,
            "recovery_hints": list(self.recovery_hints),
            "canonical_class": self.canonical_class,
        }


def classify_error(text: str, *, model_calls: int = 0) -> ErrorClassification:
    """Classify raw evidence conservatively; unknown errors are not task fails."""

    raw = str(text or "")
    lower = raw.casefold()
    verifier = verifier_execution_observed(raw)
    consumed = int(model_calls or 0) > 0
    canonical = classify_reason(raw, event_type="TerminalBenchError")
    if any(marker in lower for marker in ("402", "insufficient balance", "invalid api key", "quota exceeded")):
        return ErrorClassification(
            "api",
            "blocking",
            consumed,
            verifier,
            "api",
            ("pause and request provider action",),
            FailureClass.PROVIDER_TRANSPORT.value,
        )
    markers = {
        "docker": ("docker daemon", "address pool", "fully subnetted", "container"),
        "network": ("connection timed out", "connection reset", "dns", "tls", "proxy"),
        "memory": ("out of memory", "cannot allocate memory", "errno 12"),
        "filesystem": ("no space left", "permission denied", "inode"),
        "dependency": ("module not found", "modulenotfounderror", "command not found"),
        "verifier": ("verifier", "test.sh"),
        "process": ("too many open files", "resource temporarily unavailable", "fork"),
    }
    for category, category_markers in markers.items():
        if any(marker in lower for marker in category_markers):
            severity = "deterministic" if verifier and category == "verifier" else ("recoverable" if transient_infrastructure_text(raw) else "blocking")
            category_class = {
                "docker": FailureClass.ENVIRONMENT_NOT_READY.value,
                "memory": FailureClass.ENVIRONMENT_NOT_READY.value,
                "filesystem": FailureClass.ENVIRONMENT_NOT_READY.value,
                "dependency": FailureClass.ENVIRONMENT_NOT_READY.value,
                "process": FailureClass.ENVIRONMENT_NOT_READY.value,
                "network": FailureClass.PROVIDER_TRANSPORT.value,
            }.get(category, canonical.value)
            return ErrorClassification(
                category,
                severity,
                consumed,
                verifier,
                category,
                ("retain raw evidence",),
                category_class,
            )
    if verifier:
        return ErrorClassification(
            "task",
            "deterministic",
            consumed,
            True,
            "none",
            (),
            FailureClass.TASK_FAILURE.value,
        )
    return ErrorClassification(
        "unknown",
        "recoverable",
        consumed,
        False,
        "manual-review",
        ("pause before starting another task",),
        FailureClass.OUTCOME_UNKNOWN.value,
    )


__all__ = ["ErrorClassification", "classify_error"]
