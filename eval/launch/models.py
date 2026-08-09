"""Immutable contracts for launching coding harnesses under evaluation."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from eval.core.models import Digest, EvidenceModel, Identifier, ResourceBudget

PARITY_MODEL = "deepseek-v4-flash"


class HarnessKind(StrEnum):
    CC_HARNESS = "cc-harness"
    CLAUDE_CODE = "claude-code"
    CODEX = "codex"


class LaunchProfile(EvidenceModel):
    schema_version: Literal["eval.launch-profile.v1"] = "eval.launch-profile.v1"
    profile_id: Identifier
    harness: HarnessKind
    executable: Annotated[str, Field(min_length=1, max_length=2048)]
    requested_model: Literal["deepseek-v4-flash"] = PARITY_MODEL
    provider_route_id: Identifier
    environment_allowlist: tuple[
        Annotated[str, Field(pattern=r"^[A-Z_][A-Z0-9_]*$", max_length=128)], ...
    ] = ()
    extra_args: tuple[str, ...] = ()
    max_stdout_bytes: Annotated[int, Field(gt=0, le=100_000_000)] = 10_000_000
    max_stderr_bytes: Annotated[int, Field(gt=0, le=100_000_000)] = 2_000_000

    @field_validator("executable", "extra_args")
    @classmethod
    def reject_unsafe_arguments(cls, value):
        values = (value,) if isinstance(value, str) else value
        if any("\x00" in item for item in values):
            raise ValueError("launch arguments cannot contain NUL bytes")
        return value

    @model_validator(mode="after")
    def validate_profile(self) -> LaunchProfile:
        if len(set(self.environment_allowlist)) != len(self.environment_allowlist):
            raise ValueError("environment allowlist entries must be unique")
        return self


class LaunchEvidence(EvidenceModel):
    schema_version: Literal["eval.launch-evidence.v1"] = "eval.launch-evidence.v1"
    harness: HarnessKind
    requested_model: str
    resolved_model: str | None = None
    exit_code: int
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    wall_time_ms: Annotated[int, Field(ge=0)]
    model_calls: Annotated[int, Field(ge=0)] = 0
    tool_calls: Annotated[int, Field(ge=0)] = 0
    input_tokens: Annotated[int, Field(ge=0)] = 0
    uncached_input_tokens: Annotated[int, Field(ge=0)] = 0
    cache_creation_input_tokens: Annotated[int, Field(ge=0)] = 0
    cache_read_input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    cost_microusd: Annotated[int, Field(ge=0)] | None = None
    reported_cost_microusd: Annotated[int, Field(ge=0)] | None = None
    cost_source: Literal["normalized_tariff"] | None = None
    pricing_contract_digest: Digest | None = None
    parse_error: str | None = None

    @property
    def valid_for_parity(self) -> bool:
        return (
            self.exit_code == 0
            and not self.timed_out
            and not self.stdout_truncated
            and not self.stderr_truncated
            and self.parse_error is None
            and self.resolved_model == PARITY_MODEL
            and self.requested_model == PARITY_MODEL
        )


class LaunchRequest(EvidenceModel):
    schema_version: Literal["eval.launch-request.v1"] = "eval.launch-request.v1"
    prompt: Annotated[str, Field(min_length=1, max_length=1_000_000)]
    budget: ResourceBudget
