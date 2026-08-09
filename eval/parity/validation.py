"""Pre-execution validation for a pinned and fair Claude Code parity run."""

from __future__ import annotations

import re
from typing import Literal

from eval.core import (
    EvalRunManifest,
    TaskContract,
    content_fingerprint,
    validate_parity_manifests,
)
from eval.core.models import EvidenceModel
from eval.launch import HarnessKind

from .schedule import ParitySchedule

DEFAULT_CLAUDE_CODE_VERSION = "2.1.221"
_VERSION_PATTERN = re.compile(r"(?<![0-9])([0-9]+\.[0-9]+\.[0-9]+)(?![0-9])")


class ExecutionContractValidation(EvidenceModel):
    schema_version: Literal["eval.execution-contract-validation.v1"] = (
        "eval.execution-contract-validation.v1"
    )
    valid: bool
    expected_claude_code_version: str
    resolved_claude_code_version: str | None
    errors: tuple[str, ...] = ()


def validate_execution_contract(
    manifests: dict[HarnessKind, EvalRunManifest],
    contracts: tuple[TaskContract, ...],
    schedule: ParitySchedule,
    *,
    expected_claude_code_version: str = DEFAULT_CLAUDE_CODE_VERSION,
) -> ExecutionContractValidation:
    errors: list[str] = []
    expected_harnesses = {HarnessKind.CC_HARNESS, HarnessKind.CLAUDE_CODE}
    if set(manifests) != expected_harnesses:
        errors.append("parity execution requires exactly cc-harness and Claude Code manifests")
        return ExecutionContractValidation(
            valid=False,
            expected_claude_code_version=expected_claude_code_version,
            resolved_claude_code_version=None,
            errors=tuple(errors),
        )

    parity = validate_parity_manifests(
        (manifests[HarnessKind.CC_HARNESS], manifests[HarnessKind.CLAUDE_CODE])
    )
    errors.extend(parity.errors)
    expected_digests = tuple(content_fingerprint(contract) for contract in contracts)
    if not contracts:
        errors.append("parity execution requires at least one task contract")
    elif len(set(expected_digests)) != len(expected_digests):
        errors.append("task contracts must be unique")
    for manifest in manifests.values():
        if manifest.task_contract_digests != expected_digests:
            errors.append(f"{manifest.run_id} task contracts do not match the execution catalog")
        if manifest.repetitions != schedule.repetitions:
            errors.append(f"{manifest.run_id} repetitions do not match the persisted schedule")

    scheduled_tasks = {item.task_id for item in schedule.pairs}
    contract_tasks = {item.task_id for item in contracts}
    if scheduled_tasks != contract_tasks:
        errors.append("scheduled tasks do not match the execution catalog")

    candidate = manifests[HarnessKind.CC_HARNESS].subject
    baseline = manifests[HarnessKind.CLAUDE_CODE].subject
    if candidate.subject_id != "cc-harness":
        errors.append("candidate manifest subject must be cc-harness")
    if baseline.subject_id != "claude-code":
        errors.append("baseline manifest subject must be claude-code")
    resolved_version = semantic_version(baseline.product_version)
    if resolved_version is None:
        errors.append("Claude Code product version does not contain a semantic version")
    elif resolved_version != expected_claude_code_version:
        errors.append(
            f"Claude Code version drift: expected {expected_claude_code_version}, "
            f"resolved {resolved_version}"
        )
    return ExecutionContractValidation(
        valid=not errors,
        expected_claude_code_version=expected_claude_code_version,
        resolved_claude_code_version=resolved_version,
        errors=tuple(errors),
    )


def semantic_version(value: str) -> str | None:
    matched = _VERSION_PATTERN.search(value)
    return matched.group(1) if matched else None
