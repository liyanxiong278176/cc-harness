"""Versioned contracts for same-model harness canaries."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from eval.core.models import Digest, EvidenceModel, Identifier
from eval.launch.models import PARITY_MODEL


class ProtectedFile(EvidenceModel):
    path: Annotated[str, Field(min_length=1, max_length=2048)]
    digest: Digest

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        _validate_relative_path(path)
        return path


class CanaryInstruction(EvidenceModel):
    schema_version: Literal["eval.canary-instruction.v1"] = "eval.canary-instruction.v1"
    contract_id: Identifier
    prompt: Annotated[str, Field(min_length=1, max_length=100_000)]
    test_targets: Annotated[tuple[str, ...], Field(min_length=1)]
    protected_files: Annotated[tuple[ProtectedFile, ...], Field(min_length=1)]
    outcome_paths: Annotated[tuple[str, ...], Field(min_length=1)]
    requested_model: Literal["deepseek-v4-flash"] = PARITY_MODEL
    grader_timeout_seconds: Annotated[int, Field(gt=0, le=300)] = 30
    output_limit_bytes: Annotated[int, Field(gt=0, le=10_000_000)] = 1_000_000

    @field_validator("test_targets", "outcome_paths")
    @classmethod
    def validate_paths(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        for path in paths:
            _validate_relative_path(path)
        if len(set(paths)) != len(paths):
            raise ValueError("canary paths must be unique")
        return paths

    @model_validator(mode="after")
    def require_protected_tests(self) -> CanaryInstruction:
        protected_paths = tuple(item.path for item in self.protected_files)
        if len(set(protected_paths)) != len(protected_paths):
            raise ValueError("protected canary paths must be unique")
        if not set(self.test_targets).issubset(protected_paths):
            raise ValueError("all canary test targets must be protected")
        return self


def _validate_relative_path(value: str) -> None:
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if not value or value.startswith(("/", "\\")) or ":" in parts[0]:
        raise ValueError("canary paths must be relative")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("canary paths cannot contain empty or traversal segments")
