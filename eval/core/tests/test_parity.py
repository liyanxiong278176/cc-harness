from __future__ import annotations

from eval.core import (
    AdaptiveSamplingPolicy,
    AggregateStatus,
    PairedObservation,
    ResourceUsage,
    ResultStatus,
    RiskLevel,
    SamplingStopReason,
    evaluate_adaptive_sampling,
    summarize_usage,
    validate_parity_manifests,
)
from eval.core.tests._support import passing_result, prepared_case
from eval.core.tests.test_models import manifest

DIGEST = f"sha256:{'a' * 64}"


def _wins(count: int) -> tuple[PairedObservation, ...]:
    return tuple(
        PairedObservation(
            pair_id=f"pair-{index}",
            task_contract_digest=DIGEST,
            candidate_status=ResultStatus.PASS,
            baseline_status=ResultStatus.FAIL,
        )
        for index in range(count)
    )


def test_parity_validation_rejects_resolved_model_and_budget_drift() -> None:
    baseline = manifest()
    candidate = baseline.model_copy(
        update={
            "run_id": "run-candidate",
            "subject": baseline.subject.model_copy(
                update={
                    "subject_id": "codex",
                    "model": baseline.subject.model.model_copy(
                        update={"resolved_model": "different-model"}
                    ),
                }
            ),
            "default_budget": baseline.default_budget.model_copy(
                update={"max_steps": baseline.default_budget.max_steps + 1}
            ),
        }
    )
    result = validate_parity_manifests((baseline, candidate))
    assert result.valid is False
    assert any("resolved model" in error for error in result.errors)
    assert any("resource budget" in error for error in result.errors)


def test_adaptive_sampling_fills_risk_strata_before_confident_stop() -> None:
    observations = _wins(10)
    risks = {item.pair_id: RiskLevel.CRITICAL for item in observations}
    policy = AdaptiveSamplingPolicy(
        minimum_discordant=5,
        maximum_pairs=20,
        minimum_by_risk={
            RiskLevel.CRITICAL: 2,
            RiskLevel.HIGH: 2,
            RiskLevel.MEDIUM: 0,
            RiskLevel.LOW: 0,
        },
    )
    decision = evaluate_adaptive_sampling("adaptive", observations, risks, policy)
    assert decision.comparison.status is AggregateStatus.PASS
    assert decision.stop_reason is SamplingStopReason.CONTINUE
    assert decision.next_risk is RiskLevel.HIGH

    all_risks = dict(risks)
    all_risks["pair-8"] = RiskLevel.HIGH
    all_risks["pair-9"] = RiskLevel.HIGH
    decision = evaluate_adaptive_sampling("adaptive", observations, all_risks, policy)
    assert decision.stop_reason is SamplingStopReason.CONFIDENT
    assert decision.next_risk is None


async def test_usage_summary_preserves_unknown_cost(tmp_path) -> None:
    store, _, _, _ = await prepared_case(tmp_path)
    try:
        lease = await store.claim_next("worker-1")
        assert lease is not None
        known = await passing_result(store, lease)
        unknown = known.model_copy(
            update={
                "trial_id": "trial-unknown-cost",
                "usage": ResourceUsage(
                    **{
                        **known.usage.model_dump(mode="python"),
                        "cost_microusd": None,
                    }
                ),
            }
        )

        summary = summarize_usage((known, unknown))

        assert summary.cost_microusd is None
        assert summary.cost_complete is False
    finally:
        await store.close()
