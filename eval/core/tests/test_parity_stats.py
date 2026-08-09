from eval.core import (
    ParityConclusion,
    ParityDecisionPolicy,
    ParityTrialObservation,
    ResourceUsage,
    ResultStatus,
    evaluate_parity_decision,
)


def _usage(*, wall: int, tokens: int, cost: int | None) -> ResourceUsage:
    return ResourceUsage(
        wall_time_ms=wall,
        steps=2,
        model_calls=2,
        tool_calls=3,
        input_tokens=tokens,
        output_tokens=0,
        cost_microusd=cost,
    )


def _observations(
    candidate: ResultStatus,
    baseline: ResultStatus,
    *,
    candidate_ratio: float = 1.0,
    veto: bool = False,
    tasks: int = 10,
    repetitions: int = 3,
) -> tuple[ParityTrialObservation, ...]:
    return tuple(
        ParityTrialObservation(
            pair_id=f"task-{task}.rep-{repetition}",
            task_id=f"task-{task}",
            repetition=repetition,
            candidate_status=candidate,
            baseline_status=baseline,
            candidate_usage=_usage(
                wall=int(1_000 * candidate_ratio),
                tokens=int(1_000 * candidate_ratio),
                cost=int(10_000 * candidate_ratio),
            ),
            baseline_usage=_usage(wall=1_000, tokens=1_000, cost=10_000),
            veto_regression=veto,
        )
        for task in range(tasks)
        for repetition in range(1, repetitions + 1)
    )


def _policy(**updates) -> ParityDecisionPolicy:
    values = {
        "minimum_task_clusters": 10,
        "minimum_repetitions": 3,
        "bootstrap_iterations": 200,
    }
    values.update(updates)
    return ParityDecisionPolicy(**values)


def test_clear_success_advantage_exceeds() -> None:
    decision = evaluate_parity_decision(
        "comparison",
        _observations(ResultStatus.PASS, ResultStatus.FAIL),
        policy=_policy(),
    )

    assert decision.conclusion is ParityConclusion.EXCEEDS
    assert decision.success_difference is not None
    assert decision.success_difference.confidence_low == 1.0


def test_noninferior_and_twenty_percent_faster_exceeds() -> None:
    decision = evaluate_parity_decision(
        "comparison",
        _observations(ResultStatus.PASS, ResultStatus.PASS, candidate_ratio=0.7),
        policy=_policy(),
    )

    assert decision.conclusion is ParityConclusion.EXCEEDS
    wall_time = next(item for item in decision.efficiency if item.metric == "wall_time")
    assert wall_time.qualifies_for_twenty_percent_reduction is True


def test_equal_outcomes_without_efficiency_advantage_are_parity() -> None:
    decision = evaluate_parity_decision(
        "comparison",
        _observations(ResultStatus.PASS, ResultStatus.PASS),
        policy=_policy(),
    )

    assert decision.conclusion is ParityConclusion.PARITY


def test_material_regression_or_veto_is_below() -> None:
    regression = evaluate_parity_decision(
        "regression",
        _observations(ResultStatus.FAIL, ResultStatus.PASS),
        policy=_policy(),
    )
    veto = evaluate_parity_decision(
        "veto",
        _observations(ResultStatus.PASS, ResultStatus.PASS, veto=True),
        policy=_policy(),
    )

    assert regression.conclusion is ParityConclusion.BELOW
    assert veto.conclusion is ParityConclusion.BELOW


def test_underpowered_or_invalid_contract_does_not_claim_parity() -> None:
    underpowered = evaluate_parity_decision(
        "small",
        _observations(ResultStatus.PASS, ResultStatus.PASS, tasks=2),
        policy=_policy(),
    )
    invalid = evaluate_parity_decision(
        "invalid",
        _observations(ResultStatus.PASS, ResultStatus.PASS),
        policy=_policy(),
        contract_errors=("Claude Code version drift",),
    )

    assert underpowered.conclusion is ParityConclusion.INCONCLUSIVE
    assert invalid.conclusion is ParityConclusion.INVALID


def test_unknown_cost_is_not_treated_as_zero() -> None:
    observations = tuple(
        item.model_copy(
            update={
                "candidate_usage": item.candidate_usage.model_copy(
                    update={"cost_microusd": None}
                )
            }
        )
        for item in _observations(ResultStatus.PASS, ResultStatus.PASS)
    )
    decision = evaluate_parity_decision("cost", observations, policy=_policy())
    cost = next(item for item in decision.efficiency if item.metric == "cost")

    assert cost.candidate_to_baseline_ratio is None
    assert cost.qualifies_for_twenty_percent_reduction is False
