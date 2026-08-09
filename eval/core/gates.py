"""Deterministic release-gate decisions over immutable evaluation evidence."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .models import (
    AggregateStatus,
    CapabilityDomain,
    Digest,
    EvalRunManifest,
    EvalTier,
    EvidenceModel,
    Identifier,
    ResultStatus,
    RiskLevel,
    TaskContract,
    TrialResult,
    Version,
)
from .serialization import content_fingerprint


class FindingKind(StrEnum):
    VETO_FAILURE = "veto_failure"
    TASK_FAILURE = "task_failure"
    INVALID_TRIAL = "invalid_trial"
    MISSING_TRIAL = "missing_trial"
    EXTRA_TRIAL = "extra_trial"
    EVIDENCE_MISMATCH = "evidence_mismatch"
    MISSING_DOMAIN = "missing_domain"


class GatePolicy(EvidenceModel):
    policy_id: Identifier
    policy_version: Version
    required_domains: tuple[CapabilityDomain, ...]
    invalid_is_veto: bool
    missing_is_veto: bool

    @model_validator(mode="after")
    def validate_policy(self) -> GatePolicy:
        if len(set(self.required_domains)) != len(self.required_domains):
            raise ValueError("required domains must be unique")
        return self

    @classmethod
    def for_manifest(
        cls,
        manifest: EvalRunManifest,
        tasks: tuple[TaskContract, ...],
    ) -> GatePolicy:
        if manifest.tier is EvalTier.L4_RELEASE:
            domains = tuple(CapabilityDomain)
            invalid_is_veto = True
            missing_is_veto = True
        else:
            domains = tuple(dict.fromkeys(domain for task in tasks for domain in task.domains))
            invalid_is_veto = False
            missing_is_veto = False
        return cls(
            policy_id=f"release-{manifest.tier.value}",
            policy_version="1.0.0",
            required_domains=domains,
            invalid_is_veto=invalid_is_veto,
            missing_is_veto=missing_is_veto,
        )


class GateFinding(EvidenceModel):
    finding_id: Identifier
    kind: FindingKind
    severity: RiskLevel
    veto: bool
    message: Annotated[str, Field(min_length=1, max_length=4000)]
    task_id: Identifier | None = None
    domain: CapabilityDomain | None = None
    trial_ids: tuple[Identifier, ...] = ()


class TaskGateResult(EvidenceModel):
    task_id: Identifier
    task_contract_digest: Digest
    domains: tuple[CapabilityDomain, ...]
    status: AggregateStatus
    expected_trials: Annotated[int, Field(gt=0)]
    pass_count: Annotated[int, Field(ge=0)]
    fail_count: Annotated[int, Field(ge=0)]
    invalid_count: Annotated[int, Field(ge=0)]
    missing_count: Annotated[int, Field(ge=0)]
    extra_count: Annotated[int, Field(ge=0)]


class DomainGateResult(EvidenceModel):
    domain: CapabilityDomain
    status: AggregateStatus
    task_ids: tuple[Identifier, ...]


class ReleaseDecision(EvidenceModel):
    schema_version: Literal["eval.release-decision.v1"] = "eval.release-decision.v1"
    run_id: Identifier
    run_manifest_digest: Digest
    policy_id: Identifier
    policy_version: Version
    evaluated_at: datetime
    status: AggregateStatus
    valid: bool
    complete: bool
    task_results: tuple[TaskGateResult, ...]
    domain_results: tuple[DomainGateResult, ...]
    findings: tuple[GateFinding, ...]
    trial_result_digests: tuple[Digest, ...]

    @field_validator("evaluated_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        return value.astimezone(UTC)


def evaluate_release(
    manifest: EvalRunManifest,
    tasks: tuple[TaskContract, ...],
    results: tuple[TrialResult, ...],
    *,
    policy: GatePolicy | None = None,
    evaluated_at: datetime | None = None,
) -> ReleaseDecision:
    """Build one deterministic gate decision without compensating scores."""

    manifest_digest = content_fingerprint(manifest)
    policy = policy or GatePolicy.for_manifest(manifest, tasks)
    findings: list[GateFinding] = []
    finding_number = 0

    def add_finding(
        kind: FindingKind,
        severity: RiskLevel,
        veto: bool,
        message: str,
        *,
        task_id: str | None = None,
        domain: CapabilityDomain | None = None,
        trial_ids: tuple[str, ...] = (),
    ) -> None:
        nonlocal finding_number
        finding_number += 1
        findings.append(
            GateFinding(
                finding_id=f"finding-{finding_number:04d}",
                kind=kind,
                severity=severity,
                veto=veto,
                message=message,
                task_id=task_id,
                domain=domain,
                trial_ids=trial_ids,
            )
        )

    contracts: dict[str, TaskContract] = {}
    task_ids: set[str] = set()
    for task in sorted(tasks, key=content_fingerprint):
        digest = content_fingerprint(task)
        if digest in contracts or task.task_id in task_ids:
            add_finding(
                FindingKind.EVIDENCE_MISMATCH,
                RiskLevel.CRITICAL,
                True,
                f"duplicate Task Contract identity: {task.task_id}",
                task_id=task.task_id,
            )
            continue
        contracts[digest] = task
        task_ids.add(task.task_id)

    declared = set(manifest.task_contract_digests)
    supplied = set(contracts)
    for missing_digest in sorted(declared - supplied):
        add_finding(
            FindingKind.EVIDENCE_MISMATCH,
            RiskLevel.CRITICAL,
            manifest.tier is EvalTier.L4_RELEASE,
            f"manifest Task Contract was not supplied: {missing_digest}",
        )
    for extra_digest in sorted(supplied - declared):
        task = contracts[extra_digest]
        add_finding(
            FindingKind.EVIDENCE_MISMATCH,
            RiskLevel.HIGH,
            manifest.tier is EvalTier.L4_RELEASE,
            f"supplied Task Contract is not declared by the manifest: {task.task_id}",
            task_id=task.task_id,
        )

    grouped: dict[str, list[TrialResult]] = defaultdict(list)
    seen_trial_ids: set[str] = set()
    accepted_results: list[TrialResult] = []
    for result in sorted(results, key=lambda item: item.trial_id):
        if result.trial_id in seen_trial_ids:
            add_finding(
                FindingKind.EVIDENCE_MISMATCH,
                RiskLevel.CRITICAL,
                policy.invalid_is_veto,
                f"duplicate trial result identity: {result.trial_id}",
                trial_ids=(result.trial_id,),
            )
            continue
        seen_trial_ids.add(result.trial_id)
        if result.run_id != manifest.run_id or result.run_manifest_digest != manifest_digest:
            add_finding(
                FindingKind.EVIDENCE_MISMATCH,
                RiskLevel.CRITICAL,
                policy.invalid_is_veto,
                f"trial result does not belong to this run: {result.trial_id}",
                trial_ids=(result.trial_id,),
            )
            continue
        task = contracts.get(result.task_contract_digest)
        if task is None or task.task_id != result.task_id:
            add_finding(
                FindingKind.EVIDENCE_MISMATCH,
                RiskLevel.CRITICAL,
                policy.invalid_is_veto,
                f"trial result references an unknown Task Contract: {result.trial_id}",
                trial_ids=(result.trial_id,),
            )
            continue
        grouped[result.task_contract_digest].append(result)
        accepted_results.append(result)

    task_results: list[TaskGateResult] = []
    for digest in manifest.task_contract_digests:
        task = contracts.get(digest)
        if task is None:
            continue
        task_trials = grouped.get(digest, [])
        expected = manifest.repetitions
        pass_count = fail_count = invalid_count = evidence_invalid = 0
        for result in task_trials:
            contract_graders = {grader.grader_id: grader for grader in task.graders}
            actual_graders = {grader.grader_id: grader for grader in result.grader_results}
            unknown_graders = set(actual_graders) - set(contract_graders)
            missing_graders = {
                grader_id
                for grader_id, grader in contract_graders.items()
                if grader.required and grader_id not in actual_graders
            }
            if result.status is not ResultStatus.INVALID and (unknown_graders or missing_graders):
                evidence_invalid += 1
                add_finding(
                    FindingKind.EVIDENCE_MISMATCH,
                    task.risk,
                    policy.invalid_is_veto,
                    "grader evidence does not match the Task Contract: "
                    f"missing={sorted(missing_graders)}, unknown={sorted(unknown_graders)}",
                    task_id=task.task_id,
                    domain=task.domains[0],
                    trial_ids=(result.trial_id,),
                )
                continue
            if result.status is ResultStatus.INVALID:
                invalid_count += 1
                add_finding(
                    FindingKind.INVALID_TRIAL,
                    task.risk,
                    policy.invalid_is_veto,
                    result.invalid_reason or "trial evidence is invalid",
                    task_id=task.task_id,
                    domain=task.domains[0],
                    trial_ids=(result.trial_id,),
                )
            elif result.status is ResultStatus.FAIL:
                fail_count += 1
                failed_vetoes = tuple(
                    grader_id
                    for grader_id, grader_result in actual_graders.items()
                    if grader_result.status is ResultStatus.FAIL
                    and contract_graders[grader_id].veto
                )
                add_finding(
                    FindingKind.VETO_FAILURE if failed_vetoes else FindingKind.TASK_FAILURE,
                    task.risk,
                    bool(failed_vetoes),
                    (
                        f"veto graders failed: {', '.join(failed_vetoes)}"
                        if failed_vetoes
                        else "required task outcome failed"
                    ),
                    task_id=task.task_id,
                    domain=task.domains[0],
                    trial_ids=(result.trial_id,),
                )
            else:
                pass_count += 1

        observed = len(task_trials)
        missing_count = max(0, expected - observed)
        extra_count = max(0, observed - expected)
        if missing_count:
            add_finding(
                FindingKind.MISSING_TRIAL,
                task.risk,
                policy.missing_is_veto,
                f"missing {missing_count} of {expected} required trials",
                task_id=task.task_id,
                domain=task.domains[0],
            )
        if extra_count:
            add_finding(
                FindingKind.EXTRA_TRIAL,
                RiskLevel.HIGH,
                policy.invalid_is_veto,
                f"received {extra_count} extra trials beyond the frozen manifest",
                task_id=task.task_id,
                domain=task.domains[0],
                trial_ids=tuple(result.trial_id for result in task_trials[expected:]),
            )

        invalid_total = invalid_count + evidence_invalid
        if (
            fail_count
            or (invalid_total and policy.invalid_is_veto)
            or (missing_count and policy.missing_is_veto)
        ):
            task_status = AggregateStatus.FAIL
        elif invalid_total or missing_count or extra_count:
            task_status = AggregateStatus.INCONCLUSIVE
        elif pass_count == expected:
            task_status = AggregateStatus.PASS
        else:
            task_status = AggregateStatus.INCONCLUSIVE
        task_results.append(
            TaskGateResult(
                task_id=task.task_id,
                task_contract_digest=digest,
                domains=task.domains,
                status=task_status,
                expected_trials=expected,
                pass_count=pass_count,
                fail_count=fail_count,
                invalid_count=invalid_total,
                missing_count=missing_count,
                extra_count=extra_count,
            )
        )

    by_domain: dict[CapabilityDomain, list[TaskGateResult]] = defaultdict(list)
    for task_result in task_results:
        for domain in task_result.domains:
            by_domain[domain].append(task_result)
    domain_results: list[DomainGateResult] = []
    for domain in policy.required_domains:
        domain_tasks = by_domain.get(domain, [])
        if not domain_tasks:
            add_finding(
                FindingKind.MISSING_DOMAIN,
                RiskLevel.CRITICAL,
                manifest.tier is EvalTier.L4_RELEASE,
                f"required capability domain has no Task Contract: {domain.value}",
                domain=domain,
            )
            domain_status = (
                AggregateStatus.FAIL
                if manifest.tier is EvalTier.L4_RELEASE
                else AggregateStatus.INCONCLUSIVE
            )
        elif any(task.status is AggregateStatus.FAIL for task in domain_tasks):
            domain_status = AggregateStatus.FAIL
        elif any(task.status is AggregateStatus.INCONCLUSIVE for task in domain_tasks):
            domain_status = AggregateStatus.INCONCLUSIVE
        else:
            domain_status = AggregateStatus.PASS
        domain_results.append(
            DomainGateResult(
                domain=domain,
                status=domain_status,
                task_ids=tuple(task.task_id for task in domain_tasks),
            )
        )

    evidence_kinds = {
        FindingKind.INVALID_TRIAL,
        FindingKind.MISSING_TRIAL,
        FindingKind.EXTRA_TRIAL,
        FindingKind.EVIDENCE_MISMATCH,
        FindingKind.MISSING_DOMAIN,
    }
    valid = not any(finding.kind in evidence_kinds for finding in findings)
    complete = (
        declared == supplied
        and len(accepted_results) == len(results)
        and all(
            result.missing_count == 0 and result.extra_count == 0 and result.invalid_count == 0
            for result in task_results
        )
        and all(result.task_ids for result in domain_results)
    )
    if any(finding.veto for finding in findings) or any(
        result.status is AggregateStatus.FAIL for result in task_results
    ):
        status = AggregateStatus.FAIL
    elif (
        not valid
        or not complete
        or any(result.status is AggregateStatus.INCONCLUSIVE for result in domain_results)
    ):
        status = AggregateStatus.INCONCLUSIVE
    else:
        status = AggregateStatus.PASS

    timestamp = evaluated_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("evaluated_at must be timezone-aware")
    return ReleaseDecision(
        run_id=manifest.run_id,
        run_manifest_digest=manifest_digest,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        evaluated_at=timestamp.astimezone(UTC),
        status=status,
        valid=valid,
        complete=complete,
        task_results=tuple(task_results),
        domain_results=tuple(domain_results),
        findings=tuple(findings),
        trial_result_digests=tuple(content_fingerprint(result) for result in accepted_results),
    )
