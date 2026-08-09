from __future__ import annotations

import pytest

from eval.core import (
    AggregateStatus,
    PairedObservation,
    ResultStatus,
    evaluate_paired_comparison,
)

DIGEST = f"sha256:{'a' * 64}"


def observations(candidate_wins: int, baseline_wins: int, *, invalid: int = 0):
    values = []
    for index in range(candidate_wins):
        values.append(
            PairedObservation(
                pair_id=f"candidate-{index}",
                task_contract_digest=DIGEST,
                candidate_status=ResultStatus.PASS,
                baseline_status=ResultStatus.FAIL,
            )
        )
    for index in range(baseline_wins):
        values.append(
            PairedObservation(
                pair_id=f"baseline-{index}",
                task_contract_digest=DIGEST,
                candidate_status=ResultStatus.FAIL,
                baseline_status=ResultStatus.PASS,
            )
        )
    for index in range(invalid):
        values.append(
            PairedObservation(
                pair_id=f"invalid-{index}",
                task_contract_digest=DIGEST,
                candidate_status=ResultStatus.INVALID,
                baseline_status=ResultStatus.PASS,
            )
        )
    return tuple(values)


def test_paired_comparison_requires_confident_discordant_advantage() -> None:
    better = evaluate_paired_comparison("better", observations(20, 0), minimum_discordant=10)
    worse = evaluate_paired_comparison("worse", observations(0, 20), minimum_discordant=10)
    uncertain = evaluate_paired_comparison("uncertain", observations(6, 4), minimum_discordant=10)
    assert better.status is AggregateStatus.PASS
    assert better.confidence_low > 0.5
    assert worse.status is AggregateStatus.FAIL
    assert worse.confidence_high < 0.5
    assert uncertain.status is AggregateStatus.INCONCLUSIVE


def test_invalid_or_underpowered_pairs_are_inconclusive() -> None:
    invalid = evaluate_paired_comparison(
        "invalid",
        observations(20, 0, invalid=1),
        minimum_discordant=10,
    )
    underpowered = evaluate_paired_comparison(
        "small",
        observations(3, 0),
        minimum_discordant=10,
    )
    assert invalid.status is AggregateStatus.INCONCLUSIVE
    assert underpowered.status is AggregateStatus.INCONCLUSIVE


def test_pair_identity_must_be_unique() -> None:
    pair = observations(1, 0)[0]
    with pytest.raises(ValueError, match="pair_id values must be unique"):
        evaluate_paired_comparison("duplicate", (pair, pair), minimum_discordant=1)
