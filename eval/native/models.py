"""Instruction schema consumed by the deterministic native pytest adapter."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator

from eval.core.models import EvidenceModel, Identifier

SAFE_ENV_NAMES = frozenset(
    {
        "CI",
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "PYTHONHASHSEED",
        "PYTHONIOENCODING",
        "TZ",
    }
)


class NativePytestSpec(EvidenceModel):
    schema_version: Literal["eval.native-pytest.v1"] = "eval.native-pytest.v1"
    contract_id: Identifier
    test_targets: Annotated[tuple[str, ...], Field(min_length=1)]
    expected_exit_code: Annotated[int, Field(ge=0, le=255)] = 0
    output_limit_bytes: Annotated[int, Field(gt=0, le=100_000_000)] = 10_000_000
    environment: tuple[str, ...] = ()

    @field_validator("test_targets")
    @classmethod
    def validate_test_targets(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("test targets must be unique")
        for value in values:
            normalized = value.replace("\\", "/")
            path = normalized.split("::", 1)[0]
            if (
                not normalized
                or normalized.startswith("-")
                or not path.startswith("tests/")
                or "/../" in f"/{path}/"
                or path.startswith("/")
                or ":" in path
            ):
                raise ValueError(f"unsafe pytest target: {value}")
        return values

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("environment names must be unique")
        disallowed = set(values) - SAFE_ENV_NAMES
        if disallowed:
            raise ValueError(f"unsafe environment names: {sorted(disallowed)}")
        return values
