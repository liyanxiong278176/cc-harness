"""Protected context and advisory-memory projection for the new runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .run_model import GoalContract, digest_json
from .run_projection import RunProjection


class ContextProjectionError(ValueError):
    """Raised when advisory context lacks required provenance."""


@dataclass(frozen=True)
class AdvisoryMemoryEvidence:
    evidence_id: str
    content: str
    source: str
    project_scope: str
    timestamp: float
    confidence: float

    def __post_init__(self) -> None:
        if not self.evidence_id or not self.source or not self.project_scope:
            raise ContextProjectionError("memory evidence needs id, source, and project scope")
        if not 0.0 <= self.confidence <= 1.0:
            raise ContextProjectionError("memory confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "content": self.content,
            "source": self.source,
            "project_scope": self.project_scope,
            "timestamp": self.timestamp,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class ProjectedContext:
    goal: GoalContract | None
    pinned_facts: tuple[str, ...]
    messages: tuple[Mapping[str, Any], ...]
    memory: tuple[AdvisoryMemoryEvidence, ...]
    tool_result_refs: tuple[str, ...]

    @property
    def digest(self) -> str:
        return digest_json(
            {
                "goal": self.goal.to_dict() if self.goal else None,
                "pinned_facts": list(self.pinned_facts),
                "messages": [dict(item) for item in self.messages],
                "memory": [item.to_dict() for item in self.memory],
                "tool_result_refs": list(self.tool_result_refs),
            }
        )


class ContextProjector:
    """Build a model view without changing events or granting authority."""

    def project(
        self,
        projection: RunProjection,
        messages: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
        *,
        pinned_facts: list[str] | tuple[str, ...] = (),
        memory: list[AdvisoryMemoryEvidence] | tuple[AdvisoryMemoryEvidence, ...] = (),
    ) -> ProjectedContext:
        refs: list[str] = []
        for message in messages:
            if message.get("role") == "tool" and message.get("artifact_ref"):
                refs.append(str(message["artifact_ref"]))
        return ProjectedContext(
            goal=projection.goal,
            pinned_facts=tuple(str(item) for item in pinned_facts),
            messages=tuple(dict(item) for item in messages),
            memory=tuple(memory),
            tool_result_refs=tuple(refs),
        )


__all__ = [
    "AdvisoryMemoryEvidence",
    "ContextProjectionError",
    "ContextProjector",
    "ProjectedContext",
]
