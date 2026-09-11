"""Common guard contract and ordered orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class GuardContext:
    """Immutable scope passed to every guard."""

    project_root: Path
    attempt_root: Path
    task_id: str
    official_dataset: str
    harbor_version: str


@dataclass
class GuardResult:
    """Result of a guard phase, retaining warnings and raw evidence."""

    guard: str
    phase: str
    ready: bool = True
    blocking: bool = False
    warnings: list[str] = field(default_factory=list)
    actions_taken: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    recorded_at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "guard": self.guard,
            "phase": self.phase,
            "ready": self.ready,
            "blocking": self.blocking,
            "warnings": list(self.warnings),
            "actions_taken": list(self.actions_taken),
            "details": dict(self.details),
            "error": self.error,
            "recorded_at": self.recorded_at,
        }


@dataclass
class RecoveryResult:
    """Bounded recovery result; a caller must still decide whether to resume."""

    guard: str
    recovered: bool
    actions_taken: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    recorded_at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "guard": self.guard,
            "recovered": self.recovered,
            "actions_taken": list(self.actions_taken),
            "details": dict(self.details),
            "error": self.error,
            "recorded_at": self.recorded_at,
        }


class InfraGuard(Protocol):
    """Four-phase guard interface implemented by each resource guard."""

    name: str

    def pre_check(self, context: GuardContext) -> GuardResult: ...

    def prevent(self, context: GuardContext) -> GuardResult: ...

    def recover(self, context: GuardContext, error_text: str) -> RecoveryResult: ...

    def post_cleanup(self, context: GuardContext) -> dict[str, Any]: ...


class GuardManager:
    """Run guards in a deterministic order and persist evidence per attempt."""

    def __init__(self, context: GuardContext, guards: tuple[InfraGuard, ...]) -> None:
        self.context = context
        self.guards = guards

    def pre_check(self) -> dict[str, Any]:
        results = [self._call_result(guard, "pre_check") for guard in self.guards]
        return self._envelope("pre_check", results)

    def prevent(self) -> dict[str, Any]:
        results = [self._call_result(guard, "prevent") for guard in self.guards]
        return self._envelope("prevent", results)

    def recover(self, error_text: str) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for guard in self.guards:
            try:
                value = guard.recover(self.context, str(error_text or ""))
                results.append(value.as_dict())
            except Exception as exc:  # noqa: BLE001 - guard failures are evidence
                results.append(
                    RecoveryResult(
                        guard=guard.name,
                        recovered=False,
                        error=f"{type(exc).__name__}: {exc}",
                    ).as_dict()
                )
        return {
            "phase": "recover",
            "task_id": self.context.task_id,
            "results": results,
            "recovered": all(item.get("recovered") is not False for item in results),
            "recorded_at": _now(),
        }

    def post_cleanup(self) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for guard in self.guards:
            try:
                value = guard.post_cleanup(self.context)
                results.append({"guard": guard.name, **dict(value)})
            except Exception as exc:  # noqa: BLE001 - cleanup must be best effort
                results.append(
                    {
                        "guard": guard.name,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        return {
            "phase": "post_cleanup",
            "task_id": self.context.task_id,
            "results": results,
            "recorded_at": _now(),
        }

    def write_evidence(self, filename: str, payload: dict[str, Any]) -> Path:
        """Atomically write bounded guard evidence within the attempt scope."""

        path = self.context.attempt_root / filename
        temporary = path.with_suffix(path.suffix + ".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
        return path

    def _call_result(self, guard: InfraGuard, phase: str) -> dict[str, Any]:
        try:
            value = getattr(guard, phase)(self.context)
            return value.as_dict()
        except Exception as exc:  # noqa: BLE001 - fail closed with evidence
            return GuardResult(
                guard=guard.name,
                phase=phase,
                ready=False,
                blocking=True,
                error=f"{type(exc).__name__}: {exc}",
            ).as_dict()

    def _envelope(self, phase: str, results: list[dict[str, Any]]) -> dict[str, Any]:
        # ``blocking`` describes the severity of a failed check, not a
        # permanently blocking guard.  Several guards mark their domain as
        # blocking so a failed dependency/API/protocol check fails closed;
        # healthy results must not stop every official task.
        blocking = [
            item for item in results if item.get("blocking") and not item.get("ready", True)
        ]
        return {
            "phase": phase,
            "task_id": self.context.task_id,
            "ready": not blocking and all(item.get("ready", True) for item in results),
            "blocking": bool(blocking),
            "results": results,
            "recorded_at": _now(),
        }
