"""Instruction contract for importing Promptfoo evidence."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from eval.core.models import EvidenceModel, Identifier


class PromptfooImportSpec(EvidenceModel):
    schema_version: Literal["eval.promptfoo-import.v1"] = "eval.promptfoo-import.v1"
    contract_id: Identifier
    results_path: str
    artifact_paths: tuple[str, ...] = ()
    trajectory_path: str | None = None
    minimum_test_count: Annotated[int, Field(gt=0)] = 1
    veto_severities: tuple[Literal["critical", "high", "medium", "low"], ...] = ("critical",)
    max_artifact_bytes: Annotated[int, Field(gt=0, le=250_000_000)] = 50_000_000

    @field_validator("results_path", "trajectory_path")
    @classmethod
    def validate_optional_path(cls, value: str | None) -> str | None:
        if value is not None:
            cls._validate_path(value)
        return value

    @field_validator("artifact_paths")
    @classmethod
    def validate_artifact_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("artifact paths must be unique")
        for value in values:
            cls._validate_path(value)
        return values

    @model_validator(mode="after")
    def validate_spec(self) -> PromptfooImportSpec:
        paths = (self.results_path, *self.artifact_paths)
        if self.trajectory_path is not None:
            paths += (self.trajectory_path,)
        if len(set(paths)) != len(paths):
            raise ValueError("framework artifact roles must reference distinct paths")
        if len(set(self.veto_severities)) != len(self.veto_severities):
            raise ValueError("veto severities must be unique")
        return self

    @staticmethod
    def _validate_path(value: str) -> None:
        normalized = value.replace("\\", "/")
        if (
            not normalized
            or normalized.startswith(("/", "../"))
            or "/../" in f"/{normalized}/"
            or ":" in normalized
        ):
            raise ValueError(f"unsafe framework artifact path: {value}")
