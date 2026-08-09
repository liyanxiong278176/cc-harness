"""Boundary implemented by native and third-party evaluation adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import AdapterIdentity, ArtifactRef, TrialRequest, TrialResult


@runtime_checkable
class ArtifactRepository(Protocol):
    async def put_artifact(self, content: bytes, media_type: str) -> ArtifactRef:
        """Persist bytes before returning their immutable reference."""

    async def read_artifact(self, reference: ArtifactRef) -> bytes:
        """Read bytes and verify their digest and size."""


@dataclass(frozen=True)
class TrialExecutionContext:
    request: TrialRequest
    attempt_id: str
    attempt: int
    workspace: Path
    instruction: bytes
    artifacts: ArtifactRepository


@runtime_checkable
class EvidenceAdapter(Protocol):
    @property
    def identity(self) -> AdapterIdentity:
        """Return the persisted identity of this adapter implementation."""

    async def run_trial(self, context: TrialExecutionContext) -> TrialResult:
        """Execute or import one trial and return normalized evidence."""
