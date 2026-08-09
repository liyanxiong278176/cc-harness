"""Risk-neutral paired comparison evidence for harness ablations."""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import AggregateStatus, Digest, EvidenceModel, Identifier, ResultStatus


class PairOutcome(StrEnum):
    CANDIDATE_WIN = "candidate_win"
    BASELINE_WIN = "baseline_win"
    TIE = "tie"
    INVALID = "invalid"


class PairedObservation(EvidenceModel):
    pair_id: Identifier
    task_contract_digest: Digest
    candidate_status: ResultStatus
    baseline_status: ResultStatus

    @property
    def outcome(self) -> PairOutcome:
        if ResultStatus.INVALID in {self.candidate_status, self.baseline_status}:
            return PairOutcome.INVALID
        if self.candidate_status is self.baseline_status:
            return PairOutcome.TIE
        if self.candidate_status is ResultStatus.PASS:
            return PairOutcome.CANDIDATE_WIN
        return PairOutcome.BASELINE_WIN


class PairedComparison(EvidenceModel):
    schema_version: Literal["eval.paired-comparison.v1"] = "eval.paired-comparison.v1"
    comparison_id: Identifier
    status: AggregateStatus
    observation_count: Annotated[int, Field(ge=0)]
    candidate_wins: Annotated[int, Field(ge=0)]
    baseline_wins: Annotated[int, Field(ge=0)]
    ties: Annotated[int, Field(ge=0)]
    invalid: Annotated[int, Field(ge=0)]
    discordant_count: Annotated[int, Field(ge=0)]
    candidate_win_rate: Annotated[float, Field(ge=0.0, le=1.0)] | None
    confidence_low: Annotated[float, Field(ge=0.0, le=1.0)] | None
    confidence_high: Annotated[float, Field(ge=0.0, le=1.0)] | None
    minimum_discordant: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def validate_counts(self) -> PairedComparison:
        if self.candidate_wins + self.baseline_wins != self.discordant_count:
            raise ValueError("discordant count does not match win counts")
        if (
            self.candidate_wins + self.baseline_wins + self.ties + self.invalid
            != self.observation_count
        ):
            raise ValueError("paired comparison counts do not sum to observation_count")
        return self


def evaluate_paired_comparison(
    comparison_id: str,
    observations: tuple[PairedObservation, ...],
    *,
    minimum_discordant: int = 10,
    z_score: float = 1.959963984540054,
) -> PairedComparison:
    """Evaluate candidate superiority from paired discordant outcomes using Wilson CI."""

    if minimum_discordant <= 0:
        raise ValueError("minimum_discordant must be positive")
    if z_score <= 0:
        raise ValueError("z_score must be positive")
    pair_ids = [observation.pair_id for observation in observations]
    if len(set(pair_ids)) != len(pair_ids):
        raise ValueError("pair_id values must be unique")
    outcomes = [observation.outcome for observation in observations]
    candidate_wins = outcomes.count(PairOutcome.CANDIDATE_WIN)
    baseline_wins = outcomes.count(PairOutcome.BASELINE_WIN)
    ties = outcomes.count(PairOutcome.TIE)
    invalid = outcomes.count(PairOutcome.INVALID)
    discordant = candidate_wins + baseline_wins
    if discordant:
        rate = candidate_wins / discordant
        low, high = _wilson_interval(candidate_wins, discordant, z_score)
    else:
        rate = low = high = None

    if invalid or discordant < minimum_discordant:
        status = AggregateStatus.INCONCLUSIVE
    elif low is not None and low > 0.5:
        status = AggregateStatus.PASS
    elif high is not None and high < 0.5:
        status = AggregateStatus.FAIL
    else:
        status = AggregateStatus.INCONCLUSIVE
    return PairedComparison(
        comparison_id=comparison_id,
        status=status,
        observation_count=len(observations),
        candidate_wins=candidate_wins,
        baseline_wins=baseline_wins,
        ties=ties,
        invalid=invalid,
        discordant_count=discordant,
        candidate_win_rate=rate,
        confidence_low=low,
        confidence_high=high,
        minimum_discordant=minimum_discordant,
    )


def _wilson_interval(successes: int, total: int, z_score: float) -> tuple[float, float]:
    probability = successes / total
    z_squared = z_score**2
    denominator = 1 + z_squared / total
    center = (probability + z_squared / (2 * total)) / denominator
    margin = (
        z_score
        * math.sqrt(probability * (1 - probability) / total + z_squared / (4 * total * total))
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)
