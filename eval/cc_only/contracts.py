"""Contracts shared by all cc-harness-only benchmark integrations."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

MODEL = "deepseek-v4-flash"


class EvalProfile(StrEnum):
    CHECK = "check"
    PORTFOLIO = "portfolio"
    FULL = "full"


class TrialStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INVALID = "invalid"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class BenchmarkTask:
    task_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    group: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "group": self.group,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class CheckResult:
    ready: bool
    details: Mapping[str, Any]
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "details": dict(self.details),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class TrialOutcome:
    status: TrialStatus
    metrics: Mapping[str, Any] = field(default_factory=dict)
    # Usage is intentionally extensible: token counters remain integers while
    # provider billing telemetry also carries a currency, status, and source.
    # Keeping this as ``Any`` avoids coercing or dropping those audit facts.
    usage: Mapping[str, Any] = field(default_factory=dict)
    invalid_reason: str | None = None
    failure_reason: str | None = None
    critical_failure: bool = False
    protocol: Mapping[str, Any] = field(default_factory=dict)
    # Official benchmark grading and the runtime's own lifecycle diagnostics
    # are deliberately separate ledgers.  A runtime can stall after Harbor
    # has already persisted an official verifier reward; collapsing the two
    # would turn valid grades into infrastructure failures (or vice versa).
    official_result: Mapping[str, Any] = field(default_factory=dict)
    runtime_diagnostic: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        protocol = dict(self.protocol)
        runtime_diagnostic = dict(self.runtime_diagnostic)
        # Keep the stable ``failure_class`` compatibility field untouched,
        # while promoting the non-scoring root-cause label into the protocol
        # envelope so reports can group deadline/network/service/persistence
        # failures without parsing free-form diagnostics.
        if "failure_root_cause" not in protocol and protocol.get("failure_class"):
            root_cause = runtime_diagnostic.get("root_cause")
            evidence = protocol.get("failure_evidence")
            if not root_cause and isinstance(evidence, Mapping):
                details = evidence.get("details")
                if isinstance(details, Mapping):
                    root_cause = details.get("root_cause")
            if root_cause:
                protocol["failure_root_cause"] = str(root_cause)
        return {
            "schema_version": "eval.cc-only-trial-outcome.v1",
            "status": self.status.value,
            "metrics": dict(self.metrics),
            "usage": dict(self.usage),
            "invalid_reason": self.invalid_reason,
            "failure_reason": self.failure_reason,
            "critical_failure": self.critical_failure,
            "protocol": protocol,
            "official_result": dict(self.official_result),
            "runtime_diagnostic": runtime_diagnostic,
        }


@dataclass(frozen=True)
class TrialContext:
    project_root: Path
    output_root: Path
    attempt_root: Path
    task: BenchmarkTask
    profile: EvalProfile
    attempt: int
    watchdog_seconds: int
    # Live progress is deliberately outside the immutable benchmark contract.
    # Adapters may use it for phase/session/QA visualization without affecting
    # scoring or resumability.
    progress: Callable[[str], None] = print
    task_index: int = 1
    task_total: int = 1
    task_limit: int | None = None
    qa_limit: int | None = None
    cache_only: bool = False
    cache_refresh: bool = False
    # Number of official Harbor trials requested for this task.  Adapters may
    # use this to pass ``--n-attempts`` without changing the task/verifier
    # contract; the default preserves the historical single-trial behavior.
    trials_per_task: int = 1


class BenchmarkAdapter(Protocol):
    slug: str
    title: str
    protocol_version: str
    capability_profile: str
    adaptations: Sequence[str]

    def catalog(self, project_root: Path, profile: EvalProfile) -> Sequence[BenchmarkTask]: ...

    def check(
        self, project_root: Path, profile: EvalProfile, tasks: Sequence[BenchmarkTask]
    ) -> CheckResult: ...

    async def execute(self, context: TrialContext) -> TrialOutcome: ...

    def summarize(self, outcomes: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]: ...
