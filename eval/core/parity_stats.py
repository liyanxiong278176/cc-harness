"""Release-grade paired statistics for cc-harness versus Claude Code."""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from .comparison import PairedComparison, PairedObservation, evaluate_paired_comparison
from .models import EvidenceModel, Identifier, ResourceUsage, ResultStatus


class ParityConclusion(StrEnum):
    EXCEEDS = "exceeds"
    PARITY = "parity"
    BELOW = "below"
    INCONCLUSIVE = "inconclusive"
    INVALID = "invalid"


class EfficiencyMetric(StrEnum):
    WALL_TIME = "wall_time"
    TOTAL_TOKENS = "total_tokens"
    COST = "cost"


class BootstrapInterval(EvidenceModel):
    schema_version: Literal["eval.bootstrap-interval.v1"] = "eval.bootstrap-interval.v1"
    estimate: float
    confidence_low: float
    confidence_high: float
    confidence_level: Literal[0.95] = 0.95
    cluster_count: Annotated[int, Field(gt=0)]
    sample_count: Annotated[int, Field(gt=0)]
    bootstrap_iterations: Annotated[int, Field(ge=100)]


class EfficiencyComparison(EvidenceModel):
    schema_version: Literal["eval.efficiency-comparison.v1"] = (
        "eval.efficiency-comparison.v1"
    )
    metric: EfficiencyMetric
    candidate_to_baseline_ratio: BootstrapInterval | None
    comparable_pair_count: Annotated[int, Field(ge=0)]
    missing_pair_count: Annotated[int, Field(ge=0)]
    qualifies_for_twenty_percent_reduction: bool


class ParityDecisionPolicy(EvidenceModel):
    schema_version: Literal["eval.parity-policy.v1"] = "eval.parity-policy.v1"
    superiority_margin: Annotated[float, Field(gt=0.0, le=1.0)] = 0.05
    noninferiority_margin: Annotated[float, Field(gt=0.0, le=1.0)] = 0.03
    efficiency_ratio: Annotated[float, Field(gt=0.0, lt=1.0)] = 0.80
    minimum_task_clusters: Annotated[int, Field(gt=0)] = 10
    minimum_repetitions: Annotated[int, Field(gt=0)] = 3
    maximum_invalid_fraction: Annotated[float, Field(ge=0.0, lt=1.0)] = 0.05
    bootstrap_iterations: Annotated[int, Field(ge=100)] = 10_000
    random_seed: Annotated[int, Field(ge=0)] = 20260805


class ParityTrialObservation(EvidenceModel):
    schema_version: Literal["eval.parity-observation.v1"] = "eval.parity-observation.v1"
    pair_id: Identifier
    task_id: Identifier
    repetition: Annotated[int, Field(gt=0)]
    candidate_status: ResultStatus
    baseline_status: ResultStatus
    candidate_usage: ResourceUsage
    baseline_usage: ResourceUsage
    veto_regression: bool = False


class ParityDecision(EvidenceModel):
    schema_version: Literal["eval.parity-decision.v1"] = "eval.parity-decision.v1"
    comparison_id: Identifier
    conclusion: ParityConclusion
    policy: ParityDecisionPolicy
    observation_count: Annotated[int, Field(ge=0)]
    valid_pair_count: Annotated[int, Field(ge=0)]
    invalid_pair_count: Annotated[int, Field(ge=0)]
    task_cluster_count: Annotated[int, Field(ge=0)]
    under_repeated_tasks: tuple[Identifier, ...]
    success_difference: BootstrapInterval | None
    discordant_diagnostic: PairedComparison
    efficiency: tuple[EfficiencyComparison, ...]
    veto_regressions: tuple[Identifier, ...]
    errors: tuple[str, ...]


def evaluate_parity_decision(
    comparison_id: str,
    observations: tuple[ParityTrialObservation, ...],
    *,
    policy: ParityDecisionPolicy | None = None,
    contract_errors: tuple[str, ...] = (),
) -> ParityDecision:
    """Evaluate the normative parity claim while retaining diagnostic Wilson evidence."""

    policy = policy or ParityDecisionPolicy()
    pair_ids = [item.pair_id for item in observations]
    if len(set(pair_ids)) != len(pair_ids):
        raise ValueError("pair_id values must be unique")

    diagnostics = evaluate_paired_comparison(
        comparison_id,
        tuple(
            PairedObservation(
                pair_id=item.pair_id,
                task_contract_digest="sha256:" + "0" * 64,
                candidate_status=item.candidate_status,
                baseline_status=item.baseline_status,
            )
            for item in observations
        ),
        minimum_discordant=10,
    )
    valid = tuple(
        item
        for item in observations
        if ResultStatus.INVALID not in {item.candidate_status, item.baseline_status}
    )
    task_counts = Counter(item.task_id for item in valid)
    task_ids = tuple(sorted(task_counts))
    under_repeated = tuple(
        task_id
        for task_id in task_ids
        if task_counts[task_id] < policy.minimum_repetitions
    )
    invalid_count = len(observations) - len(valid)
    invalid_fraction = invalid_count / len(observations) if observations else 0.0
    vetoes = tuple(sorted({item.task_id for item in valid if item.veto_regression}))

    success_interval = _clustered_interval(
        valid,
        lambda sample: _success_difference(sample),
        policy=policy,
    )
    efficiency = tuple(
        _efficiency_comparison(metric, valid, policy)
        for metric in EfficiencyMetric
    )

    errors = list(contract_errors)
    if not observations:
        errors.append("comparison contains no paired observations")
    if invalid_fraction > policy.maximum_invalid_fraction:
        errors.append(
            f"invalid pair fraction {invalid_fraction:.6f} exceeds "
            f"{policy.maximum_invalid_fraction:.6f}"
        )
    if len(task_ids) < policy.minimum_task_clusters:
        errors.append(
            f"valid task clusters {len(task_ids)} below minimum "
            f"{policy.minimum_task_clusters}"
        )
    if under_repeated:
        errors.append("one or more tasks have fewer than the required repetitions")

    if contract_errors:
        conclusion = ParityConclusion.INVALID
    elif vetoes:
        conclusion = ParityConclusion.BELOW
    elif errors or success_interval is None:
        conclusion = ParityConclusion.INCONCLUSIVE
    else:
        efficiency_win = any(
            item.qualifies_for_twenty_percent_reduction for item in efficiency
        )
        low = success_interval.confidence_low
        high = success_interval.confidence_high
        if low >= policy.superiority_margin or (
            low >= -policy.noninferiority_margin and efficiency_win
        ):
            conclusion = ParityConclusion.EXCEEDS
        elif low >= -policy.noninferiority_margin and high <= policy.noninferiority_margin:
            conclusion = ParityConclusion.PARITY
        elif high < -policy.noninferiority_margin:
            conclusion = ParityConclusion.BELOW
        else:
            conclusion = ParityConclusion.INCONCLUSIVE

    return ParityDecision(
        comparison_id=comparison_id,
        conclusion=conclusion,
        policy=policy,
        observation_count=len(observations),
        valid_pair_count=len(valid),
        invalid_pair_count=invalid_count,
        task_cluster_count=len(task_ids),
        under_repeated_tasks=under_repeated,
        success_difference=success_interval,
        discordant_diagnostic=diagnostics,
        efficiency=efficiency,
        veto_regressions=vetoes,
        errors=tuple(errors),
    )


def _success_difference(observations: tuple[ParityTrialObservation, ...]) -> float:
    differences = [
        float(item.candidate_status is ResultStatus.PASS)
        - float(item.baseline_status is ResultStatus.PASS)
        for item in observations
    ]
    return sum(differences) / len(differences)


def _efficiency_comparison(
    metric: EfficiencyMetric,
    observations: tuple[ParityTrialObservation, ...],
    policy: ParityDecisionPolicy,
) -> EfficiencyComparison:
    comparable = tuple(
        item
        for item in observations
        if item.candidate_status is ResultStatus.PASS
        and item.baseline_status is ResultStatus.PASS
        and _usage_value(item.candidate_usage, metric) is not None
        and _usage_value(item.baseline_usage, metric) not in {None, 0}
    )
    interval = _clustered_interval(
        comparable,
        lambda sample: _usage_ratio(sample, metric),
        policy=policy,
    )
    qualifies = bool(
        interval is not None
        and interval.cluster_count >= policy.minimum_task_clusters
        and interval.confidence_high <= policy.efficiency_ratio
    )
    return EfficiencyComparison(
        metric=metric,
        candidate_to_baseline_ratio=interval,
        comparable_pair_count=len(comparable),
        missing_pair_count=len(observations) - len(comparable),
        qualifies_for_twenty_percent_reduction=qualifies,
    )


def _usage_value(usage: ResourceUsage, metric: EfficiencyMetric) -> int | None:
    if metric is EfficiencyMetric.WALL_TIME:
        return usage.wall_time_ms
    if metric is EfficiencyMetric.TOTAL_TOKENS:
        return usage.input_tokens + usage.output_tokens
    return usage.cost_microusd


def _usage_ratio(
    observations: tuple[ParityTrialObservation, ...],
    metric: EfficiencyMetric,
) -> float:
    candidate = sum(_usage_value(item.candidate_usage, metric) or 0 for item in observations)
    baseline = sum(_usage_value(item.baseline_usage, metric) or 0 for item in observations)
    if baseline <= 0:
        raise ValueError("baseline usage must be positive")
    return candidate / baseline


def _clustered_interval(
    observations: tuple[ParityTrialObservation, ...],
    statistic,
    *,
    policy: ParityDecisionPolicy,
) -> BootstrapInterval | None:
    if not observations:
        return None
    grouped: dict[str, list[ParityTrialObservation]] = defaultdict(list)
    for item in observations:
        grouped[item.task_id].append(item)
    task_ids = sorted(grouped)
    estimate = statistic(observations)
    rng = random.Random(policy.random_seed)
    samples: list[float] = []
    for _ in range(policy.bootstrap_iterations):
        selected = [task_ids[rng.randrange(len(task_ids))] for _ in task_ids]
        sample = tuple(item for task_id in selected for item in grouped[task_id])
        samples.append(statistic(sample))
    samples.sort()
    return BootstrapInterval(
        estimate=estimate,
        confidence_low=_percentile(samples, 0.025),
        confidence_high=_percentile(samples, 0.975),
        cluster_count=len(task_ids),
        sample_count=len(observations),
        bootstrap_iterations=policy.bootstrap_iterations,
    )


def _percentile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("cannot compute a percentile from an empty sample")
    position = (len(values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight
