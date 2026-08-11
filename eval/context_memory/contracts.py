"""Contracts for treatment-only context-memory benchmark runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

MODEL = "deepseek-v4-flash"


class Arm(StrEnum):
    TREATMENT = "treatment"


class EvalProfile(StrEnum):
    PORTFOLIO = "portfolio"
    FULL = "full"


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    INVALID = "invalid"
    INTERRUPTED = "interrupted"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class BenchmarkTask:
    task_id: str
    group: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "group": self.group,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class NativeEvent:
    """One immutable model-visible event in the upstream interaction order."""

    event_id: str
    kind: str
    content: str
    timestamp: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "content": self.content,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class NativeQuestion:
    question_id: str
    question: str
    gold: Any
    metadata: Mapping[str, Any] = field(default_factory=dict)
    image: Path | None = None


@dataclass(frozen=True)
class NativeCase:
    events: tuple[NativeEvent, ...]
    questions: tuple[NativeQuestion, ...]


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
class TrialContext:
    project_root: Path
    output_root: Path
    attempt_root: Path
    active_root: Path
    workspace: Path
    home: Path
    task: BenchmarkTask
    profile: EvalProfile
    arm: Arm
    namespace: str
    watchdog_seconds: int
    snapshot_root: Path | None = None


@dataclass(frozen=True)
class ArmOutcome:
    status: ExecutionStatus
    prediction: Any = None
    metrics: Mapping[str, Any] = field(default_factory=dict)
    usage: Mapping[str, int | None] = field(default_factory=dict)
    protocol: Mapping[str, Any] = field(default_factory=dict)
    invalid_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "eval.context-memory-arm-outcome.v1",
            "status": self.status.value,
            "prediction": self.prediction,
            "metrics": dict(self.metrics),
            "usage": dict(self.usage),
            "protocol": dict(self.protocol),
            "invalid_reason": self.invalid_reason,
        }


class BenchmarkAdapter(Protocol):
    slug: str
    title: str
    protocol_version: str
    adaptations: Sequence[str]
    requires_images: bool

    def dataset_contract(self, project_root: Path) -> Mapping[str, Any]: ...

    def catalog(self, project_root: Path, profile: EvalProfile) -> Sequence[BenchmarkTask]: ...

    def check(
        self,
        project_root: Path,
        profile: EvalProfile,
        tasks: Sequence[BenchmarkTask],
    ) -> CheckResult: ...

    def case(self, project_root: Path, task: BenchmarkTask) -> NativeCase: ...

    async def execute(self, context: TrialContext) -> ArmOutcome: ...

    def summarize(self, results: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]: ...
