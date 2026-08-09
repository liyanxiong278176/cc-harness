"""Instruction contract for importing LoCoMo evidence."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from eval.core.models import EvidenceModel, Identifier


class LocomoImportSpec(EvidenceModel):
    schema_version: Literal["eval.locomo-import.v1"] = "eval.locomo-import.v1"
    contract_id: Identifier
    results_path: str
    metrics_path: str | None = None
    report_path: str | None = None
    trajectory_paths: tuple[str, ...] = ()
    minimum_qa_count: Annotated[int, Field(gt=0)] = 1
    required_q_types: tuple[str, ...] = ()
    fatal_statuses: tuple[str, ...] = ("agent_crash", "timeout")
    max_artifact_bytes: Annotated[int, Field(gt=0, le=250_000_000)] = 50_000_000

    @field_validator("results_path", "metrics_path", "report_path")
    @classmethod
    def validate_optional_path(cls, value: str | None) -> str | None:
        if value is not None:
            cls._validate_path(value)
        return value

    @field_validator("trajectory_paths")
    @classmethod
    def validate_trajectory_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("trajectory paths must be unique")
        for value in values:
            cls._validate_path(value)
        return values

    @model_validator(mode="after")
    def validate_spec(self) -> LocomoImportSpec:
        paths = tuple(
            path
            for path in (
                self.results_path,
                self.metrics_path,
                self.report_path,
                *self.trajectory_paths,
            )
            if path is not None
        )
        if len(set(paths)) != len(paths):
            raise ValueError("framework artifact roles must reference distinct paths")
        if len(set(self.required_q_types)) != len(self.required_q_types):
            raise ValueError("required q types must be unique")
        if len(set(self.fatal_statuses)) != len(self.fatal_statuses):
            raise ValueError("fatal statuses must be unique")
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
