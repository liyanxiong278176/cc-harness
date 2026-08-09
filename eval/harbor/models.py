"""Instruction contract for importing Harbor/Terminal-Bench trial evidence."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from eval.core.models import EvidenceModel, Identifier


class HarborImportSpec(EvidenceModel):
    schema_version: Literal["eval.harbor-import.v1"] = "eval.harbor-import.v1"
    contract_id: Identifier
    result_path: str
    expected_task_name: str
    expected_task_checksum: Annotated[str, Field(min_length=1, max_length=255)]
    expected_trial_name: str | None = None
    reward_key: str = "reward"
    success_threshold: float = 1.0
    artifact_paths: tuple[str, ...] = ()
    trajectory_path: str | None = None
    max_artifact_bytes: Annotated[int, Field(gt=0, le=250_000_000)] = 50_000_000

    @field_validator("result_path", "trajectory_path")
    @classmethod
    def validate_optional_path(cls, value: str | None) -> str | None:
        if value is not None:
            cls._validate_path(value)
        return value

    @field_validator("artifact_paths")
    @classmethod
    def validate_artifact_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            cls._validate_path(value)
        if len(set(values)) != len(values):
            raise ValueError("artifact paths must be unique")
        return values

    @model_validator(mode="after")
    def validate_spec(self) -> HarborImportSpec:
        paths = (self.result_path, *self.artifact_paths)
        if self.trajectory_path:
            paths += (self.trajectory_path,)
        if len(set(paths)) != len(paths):
            raise ValueError("framework artifact roles must reference distinct paths")
        if not 0 <= self.success_threshold <= 1:
            raise ValueError("success_threshold must be between zero and one")
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
