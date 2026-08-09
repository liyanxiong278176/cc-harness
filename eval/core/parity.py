"""Fairness validation, adaptive sampling and isolated harness ablations."""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .comparison import PairedComparison, PairedObservation, evaluate_paired_comparison
from .models import (
    AggregateStatus,
    Digest,
    EvalRunManifest,
    EvidenceModel,
    Identifier,
    ResourceUsage,
    RiskLevel,
    TrialResult,
)


class ParityValidation(EvidenceModel):
    schema_version: Literal["eval.parity-validation.v1"] = "eval.parity-validation.v1"
    valid: bool
    comparison_group_id: Identifier | None
    requested_model: str | None
    resolved_model: str | None
    errors: tuple[str, ...] = ()


def validate_parity_manifests(
    manifests: tuple[EvalRunManifest, ...],
    *,
    required_model: str = "deepseek-v4-flash",
) -> ParityValidation:
    errors: list[str] = []
    if len(manifests) < 2:
        errors.append("parity comparison requires at least two run manifests")
    groups = {manifest.comparison_group_id for manifest in manifests}
    if None in groups or len(groups) != 1:
        errors.append("all runs must declare the same comparison_group_id")
    requested = {manifest.subject.model.requested_model for manifest in manifests}
    resolved = {manifest.subject.model.resolved_model for manifest in manifests}
    if requested != {required_model}:
        errors.append(f"all runs must request exactly {required_model}")
    if len(resolved) != 1:
        errors.append("all runs must report the same resolved model")
    elif resolved != {required_model}:
        errors.append(f"all runs must resolve exactly {required_model}")

    _require_equal(manifests, "task contracts", lambda item: item.task_contract_digests, errors)
    _require_equal(manifests, "resource budget", lambda item: item.default_budget, errors)
    _require_equal(manifests, "random seed", lambda item: item.random_seed, errors)
    _require_equal(manifests, "repetitions", lambda item: item.repetitions, errors)
    _require_equal(manifests, "dataset split", lambda item: item.split, errors)
    _require_equal(manifests, "environment", lambda item: item.environment, errors)
    return ParityValidation(
        valid=not errors,
        comparison_group_id=next(iter(groups)) if len(groups) == 1 else None,
        requested_model=next(iter(requested)) if len(requested) == 1 else None,
        resolved_model=next(iter(resolved)) if len(resolved) == 1 else None,
        errors=tuple(errors),
    )


def _require_equal(manifests, label, getter, errors: list[str]) -> None:
    if manifests and any(getter(item) != getter(manifests[0]) for item in manifests[1:]):
        errors.append(f"all runs must use the same {label}")


class SamplingStopReason(StrEnum):
    CONTINUE = "continue"
    CONFIDENT = "confident"
    MAX_PAIRS = "max_pairs"
    INVALID_LIMIT = "invalid_limit"


class AdaptiveSamplingPolicy(EvidenceModel):
    schema_version: Literal["eval.adaptive-policy.v1"] = "eval.adaptive-policy.v1"
    minimum_discordant: Annotated[int, Field(gt=0)] = 10
    maximum_pairs: Annotated[int, Field(gt=0)] = 200
    maximum_invalid: Annotated[int, Field(ge=0)] = 0
    minimum_by_risk: dict[RiskLevel, Annotated[int, Field(ge=0)]] = Field(
        default_factory=lambda: {
            RiskLevel.CRITICAL: 10,
            RiskLevel.HIGH: 10,
            RiskLevel.MEDIUM: 5,
            RiskLevel.LOW: 5,
        }
    )

    @model_validator(mode="after")
    def validate_limits(self) -> AdaptiveSamplingPolicy:
        if sum(self.minimum_by_risk.values()) > self.maximum_pairs:
            raise ValueError("risk-stratum minimums exceed maximum_pairs")
        return self


class AdaptiveSamplingDecision(EvidenceModel):
    schema_version: Literal["eval.adaptive-decision.v1"] = "eval.adaptive-decision.v1"
    stop_reason: SamplingStopReason
    comparison: PairedComparison
    next_risk: RiskLevel | None


def evaluate_adaptive_sampling(
    comparison_id: str,
    observations: tuple[PairedObservation, ...],
    observation_risks: dict[str, RiskLevel],
    policy: AdaptiveSamplingPolicy,
) -> AdaptiveSamplingDecision:
    unknown = {item.pair_id for item in observations} - set(observation_risks)
    if unknown:
        raise ValueError(f"paired observations have no risk stratum: {sorted(unknown)}")
    comparison = evaluate_paired_comparison(
        comparison_id,
        observations,
        minimum_discordant=policy.minimum_discordant,
    )
    if comparison.invalid > policy.maximum_invalid:
        return AdaptiveSamplingDecision(
            stop_reason=SamplingStopReason.INVALID_LIMIT,
            comparison=comparison,
            next_risk=None,
        )
    counts = Counter(observation_risks[item.pair_id] for item in observations)
    next_risk = _next_risk(counts, policy.minimum_by_risk)
    if next_risk is None and comparison.status is not AggregateStatus.INCONCLUSIVE:
        reason = SamplingStopReason.CONFIDENT
    elif len(observations) >= policy.maximum_pairs:
        reason = SamplingStopReason.MAX_PAIRS
        next_risk = None
    else:
        reason = SamplingStopReason.CONTINUE
        next_risk = next_risk or min(RiskLevel, key=lambda risk: counts[risk])
    return AdaptiveSamplingDecision(
        stop_reason=reason,
        comparison=comparison,
        next_risk=next_risk,
    )


def _next_risk(counts: Counter, minimums: dict[RiskLevel, int]) -> RiskLevel | None:
    order = (RiskLevel.CRITICAL, RiskLevel.HIGH, RiskLevel.MEDIUM, RiskLevel.LOW)
    deficits = [(minimums.get(risk, 0) - counts[risk], risk) for risk in order]
    largest, risk = max(deficits, key=lambda item: (item[0], -order.index(item[1])))
    return risk if largest > 0 else None


class AblationSpec(EvidenceModel):
    schema_version: Literal["eval.ablation.v1"] = "eval.ablation.v1"
    ablation_id: Identifier
    baseline_profile_digest: Digest
    candidate_profile_digest: Digest
    disabled_components: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=1)]


class UsageSummary(EvidenceModel):
    trial_count: Annotated[int, Field(ge=0)]
    wall_time_ms: Annotated[int, Field(ge=0)]
    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]
    cost_microusd: Annotated[int, Field(ge=0)] | None
    cost_complete: bool


def summarize_usage(results: tuple[TrialResult, ...]) -> UsageSummary:
    usages: tuple[ResourceUsage, ...] = tuple(item.usage for item in results)
    known_costs = tuple(item.cost_microusd for item in usages if item.cost_microusd is not None)
    cost_complete = len(known_costs) == len(usages)
    return UsageSummary(
        trial_count=len(results),
        wall_time_ms=sum(item.wall_time_ms for item in usages),
        input_tokens=sum(item.input_tokens for item in usages),
        output_tokens=sum(item.output_tokens for item in usages),
        cost_microusd=sum(known_costs) if cost_complete else None,
        cost_complete=cost_complete,
    )
