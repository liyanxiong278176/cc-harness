"""Instruction contract for importing one SWE-bench Verified instance."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from eval.core.models import EvidenceModel, Identifier


class SwebenchImportSpec(EvidenceModel):
    schema_version: Literal["eval.swebench-import.v1"] = "eval.swebench-import.v1"
    contract_id: Identifier
    instance_id: Annotated[str, Field(min_length=1, max_length=255)]
    prediction_path: str
    report_path: str
    log_paths: tuple[str, ...] = ()
    trajectory_path: str | None = None
    max_artifact_bytes: Annotated[int, Field(gt=0, le=250_000_000)] = 50_000_000

    @field_validator("prediction_path", "report_path", "trajectory_path")
    @classmethod
    def validate_optional_path(cls, value: str | None) -> str | None:
        if value is not None:
            cls._validate_path(value)
        return value

    @field_validator("log_paths")
    @classmethod
    def validate_log_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            cls._validate_path(value)
        if len(set(values)) != len(values):
            raise ValueError("log paths must be unique")
        return values

    @model_validator(mode="after")
    def validate_spec(self) -> SwebenchImportSpec:
        paths = (self.prediction_path, self.report_path, *self.log_paths)
        if self.trajectory_path:
            paths += (self.trajectory_path,)
        if len(set(paths)) != len(paths):
            raise ValueError("framework artifact roles must reference distinct paths")
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
