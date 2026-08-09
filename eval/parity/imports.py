"""Validated normalized evidence imports for external paired suites."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from eval.core import CapabilityDomain, ResourceUsage, ResultStatus
from eval.core.models import EvidenceModel, Identifier
from eval.launch import PARITY_MODEL, HarnessKind

from .catalog import DOMAIN_DEFINITIONS, EVIDENCE_SOURCES, EvidenceMode
from .validation import DEFAULT_CLAUDE_CODE_VERSION, semantic_version


class ImportedHarnessResult(EvidenceModel):
    schema_version: Literal["eval.imported-harness-result.v1"] = (
        "eval.imported-harness-result.v1"
    )
    status: ResultStatus
    usage: ResourceUsage
    trajectory_path: str | None = None
    patch_path: str | None = None
    grader_path: str | None = None
    invalid_reason: str | None = None

    @field_validator("trajectory_path", "patch_path", "grader_path")
    @classmethod
    def validate_artifact_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("import artifact paths must be relative and cannot traverse")
        return value

    @model_validator(mode="after")
    def validate_invalid_reason(self) -> ImportedHarnessResult:
        if self.status is ResultStatus.INVALID and self.invalid_reason is None:
            raise ValueError("invalid imported results require invalid_reason")
        if self.status is not ResultStatus.INVALID and self.invalid_reason is not None:
            raise ValueError("pass/fail imported results cannot include invalid_reason")
        return self


class NormalizedPairRecord(EvidenceModel):
    schema_version: Literal["eval.normalized-pair-record.v1"] = (
        "eval.normalized-pair-record.v1"
    )
    pair_id: Identifier
    task_id: Identifier
    repetition: Annotated[int, Field(gt=0)]
    order: tuple[HarnessKind, HarnessKind]
    domains: Annotated[tuple[CapabilityDomain, ...], Field(min_length=1)]
    candidate: ImportedHarnessResult
    baseline: ImportedHarnessResult
    veto_regression: bool = False

    @model_validator(mode="after")
    def validate_pair(self) -> NormalizedPairRecord:
        if set(self.order) != {HarnessKind.CC_HARNESS, HarnessKind.CLAUDE_CODE}:
            raise ValueError("imported pair order must contain both comparison harnesses")
        if len(set(self.domains)) != len(self.domains):
            raise ValueError("imported pair domains must be unique")
        return self


class NormalizedPairBundle(EvidenceModel):
    schema_version: Literal["eval.normalized-pair-bundle.v1"] = (
        "eval.normalized-pair-bundle.v1"
    )
    source_id: Identifier
    generated_at: datetime
    candidate_id: Literal["cc-harness"]
    baseline_id: Literal["claude-code"]
    candidate_version: str
    baseline_version: str
    requested_model: str
    resolved_model: str
    environment_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    records: Annotated[tuple[NormalizedPairRecord, ...], Field(min_length=1)]

    @field_validator("generated_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("import bundle timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_unique_pairs(self) -> NormalizedPairBundle:
        pair_ids = [item.pair_id for item in self.records]
        if len(set(pair_ids)) != len(pair_ids):
            raise ValueError("imported pair ids must be unique within a bundle")
        identities = [(item.task_id, item.repetition) for item in self.records]
        if len(set(identities)) != len(identities):
            raise ValueError("imported task/repetition identities must be unique")
        for task_id in {item.task_id for item in self.records}:
            starts = Counter(
                item.order[0] for item in self.records if item.task_id == task_id
            )
            if abs(starts[HarnessKind.CC_HARNESS] - starts[HarnessKind.CLAUDE_CODE]) > 1:
                raise ValueError("imported within-task order must be AB/BA balanced")
        return self


class LoadedPairBundle(EvidenceModel):
    schema_version: Literal["eval.loaded-pair-bundle.v1"] = "eval.loaded-pair-bundle.v1"
    path: str
    bundle: NormalizedPairBundle


def load_normalized_bundle(
    path: Path,
    *,
    expected_claude_code_version: str = DEFAULT_CLAUDE_CODE_VERSION,
) -> LoadedPairBundle:
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError(f"normalized evidence bundle is missing: {resolved}")
    bundle = NormalizedPairBundle.model_validate_json(resolved.read_bytes())
    sources = {item.source_id: item for item in EVIDENCE_SOURCES}
    source = sources.get(bundle.source_id)
    if source is None:
        raise ValueError(f"unknown normalized evidence source: {bundle.source_id}")
    if source.mode is not EvidenceMode.PAIRED_IMPORT:
        raise ValueError(f"source is not a paired import source: {bundle.source_id}")
    if bundle.requested_model != PARITY_MODEL or bundle.resolved_model != PARITY_MODEL:
        raise ValueError("imported evidence must request and resolve deepseek-v4-flash")
    resolved_version = semantic_version(bundle.baseline_version)
    if resolved_version != expected_claude_code_version:
        raise ValueError(
            f"Claude Code version drift: expected {expected_claude_code_version}, "
            f"resolved {resolved_version or 'unknown'}"
        )
    allowed_domains = set(source.domains)
    hard_gate_domains = {item.domain for item in DOMAIN_DEFINITIONS if item.hard_gate}
    for record in bundle.records:
        if not set(record.domains).issubset(allowed_domains):
            raise ValueError(f"{record.pair_id} declares domains outside {bundle.source_id}")
        if (
            record.candidate.status is ResultStatus.FAIL
            and record.baseline.status is ResultStatus.PASS
            and set(record.domains) & hard_gate_domains
            and not record.veto_regression
        ):
            raise ValueError(f"{record.pair_id} omits a required hard-gate veto")
        for result in (record.candidate, record.baseline):
            for relative in (result.trajectory_path, result.patch_path, result.grader_path):
                if relative is not None and not (resolved.parent / relative).resolve().is_file():
                    raise ValueError(f"import artifact is missing: {relative}")
    return LoadedPairBundle(path=str(resolved), bundle=bundle)
