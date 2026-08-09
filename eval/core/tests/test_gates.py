from __future__ import annotations

import json
from datetime import UTC, datetime

from eval.core import (
    AggregateStatus,
    CapabilityDomain,
    DatasetSplit,
    EvalTier,
    FailureRecord,
    GraderResult,
    ResultStatus,
    TrialResult,
    content_fingerprint,
    evaluate_release,
    render_release_markdown,
    write_release_decision,
)

from .test_models import artifact, manifest, task_contract, usage


def trial_result(run, task, index: int, status: ResultStatus) -> TrialResult:
    common = {
        "trial_id": f"trial-{index:03d}",
        "run_id": run.run_id,
        "run_manifest_digest": content_fingerprint(run),
        "task_id": task.task_id,
        "task_contract_digest": content_fingerprint(task),
        "attempt": 1,
        "adapter": task_adapter(),
        "status": status,
        "started_at": datetime(2026, 8, 3, 9, index, tzinfo=UTC),
        "finished_at": datetime(2026, 8, 3, 9, index, 1, tzinfo=UTC),
        "usage": usage(),
    }
    if status is ResultStatus.INVALID:
        return TrialResult(**common, invalid_reason="worker evidence was incomplete")
    grader = GraderResult(
        grader_id=task.graders[0].grader_id,
        status=status,
        score=1.0 if status is ResultStatus.PASS else 0.0,
    )
    if status is ResultStatus.PASS:
        return TrialResult(
            **common,
            grader_results=(grader,),
            outcome_ref=artifact("8"),
        )
    return TrialResult(
        **common,
        grader_results=(grader,),
        outcome_ref=artifact("9"),
        failure=FailureRecord(category="assertion", message="deterministic outcome failed"),
    )


def task_adapter():
    from eval.core import AdapterIdentity

    return AdapterIdentity(adapter_id="native", adapter_version="1.0.0")


def test_complete_passing_regression_run_passes_without_findings() -> None:
    task = task_contract()
    run = manifest()
    results = tuple(trial_result(run, task, index, ResultStatus.PASS) for index in range(3))
    decision = evaluate_release(
        run,
        (task,),
        results,
        evaluated_at=datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
    )
    assert decision.status is AggregateStatus.PASS
    assert decision.valid is True
    assert decision.complete is True
    assert decision.findings == ()


def test_veto_failure_blocks_release_without_invalidating_evidence() -> None:
    task = task_contract()
    run = manifest()
    results = (
        trial_result(run, task, 0, ResultStatus.PASS),
        trial_result(run, task, 1, ResultStatus.FAIL),
        trial_result(run, task, 2, ResultStatus.PASS),
    )
    decision = evaluate_release(run, (task,), results)
    assert decision.status is AggregateStatus.FAIL
    assert decision.valid is True
    assert decision.complete is True
    assert any(
        finding.veto and finding.kind.value == "veto_failure" for finding in decision.findings
    )


def test_missing_or_invalid_lower_tier_evidence_is_inconclusive() -> None:
    task = task_contract()
    run = manifest()
    missing = evaluate_release(
        run,
        (task,),
        (trial_result(run, task, 0, ResultStatus.PASS),),
    )
    invalid = evaluate_release(
        run,
        (task,),
        (
            trial_result(run, task, 0, ResultStatus.PASS),
            trial_result(run, task, 1, ResultStatus.INVALID),
            trial_result(run, task, 2, ResultStatus.PASS),
        ),
    )
    assert missing.status is AggregateStatus.INCONCLUSIVE
    assert missing.valid is False and missing.complete is False
    assert invalid.status is AggregateStatus.INCONCLUSIVE
    assert invalid.valid is False and invalid.complete is False


def test_l4_requires_all_nine_domains_and_valid_trials() -> None:
    base = task_contract()
    tasks = []
    for domain in CapabilityDomain:
        grader = base.graders[0].model_copy(update={"domains": (domain,)})
        tasks.append(
            base.model_copy(
                update={
                    "task_id": f"l4.{domain.value}",
                    "title": f"L4 {domain.value}",
                    "domains": (domain,),
                    "graders": (grader,),
                }
            )
        )
    run = manifest(tier=EvalTier.L4_RELEASE).model_copy(
        update={
            "split": DatasetSplit.HOLDOUT,
            "task_contract_digests": tuple(content_fingerprint(task) for task in tasks),
            "repetitions": 1,
        }
    )
    results = tuple(
        trial_result(run, task, index, ResultStatus.PASS) for index, task in enumerate(tasks)
    )
    passed = evaluate_release(run, tuple(tasks), results)
    assert passed.status is AggregateStatus.PASS
    assert passed.valid and passed.complete

    invalid_results = list(results)
    invalid_results[0] = trial_result(run, tasks[0], 0, ResultStatus.INVALID)
    blocked = evaluate_release(run, tuple(tasks), tuple(invalid_results))
    assert blocked.status is AggregateStatus.FAIL
    assert any(
        finding.veto and finding.kind.value == "invalid_trial" for finding in blocked.findings
    )


def test_unknown_grader_is_invalid_evidence_not_a_pass() -> None:
    task = task_contract()
    run = manifest().model_copy(update={"repetitions": 1})
    valid = trial_result(run, task, 0, ResultStatus.PASS)
    unknown = valid.model_copy(
        update={
            "grader_results": (
                GraderResult(grader_id="unknown", status=ResultStatus.PASS, score=1.0),
            )
        }
    )
    decision = evaluate_release(run, (task,), (unknown,))
    assert decision.status is AggregateStatus.INCONCLUSIVE
    assert decision.valid is False
    assert decision.task_results[0].invalid_count == 1


def test_undeclared_contract_cannot_leave_a_valid_pass_decision() -> None:
    task = task_contract()
    run = manifest().model_copy(update={"repetitions": 1})
    extra = task.model_copy(update={"task_id": "native.extra-contract"})
    decision = evaluate_release(
        run,
        (task, extra),
        (trial_result(run, task, 0, ResultStatus.PASS),),
    )
    assert decision.status is AggregateStatus.INCONCLUSIVE
    assert decision.valid is False and decision.complete is False


def test_release_decision_is_independent_of_trial_input_order() -> None:
    task = task_contract()
    run = manifest()
    results = tuple(trial_result(run, task, index, ResultStatus.PASS) for index in range(3))
    evaluated_at = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
    forward = evaluate_release(run, (task,), results, evaluated_at=evaluated_at)
    reversed_order = evaluate_release(
        run,
        (task,),
        tuple(reversed(results)),
        evaluated_at=evaluated_at,
    )
    assert forward == reversed_order
    assert content_fingerprint(forward) == content_fingerprint(reversed_order)


def test_release_report_is_projection_of_machine_decision(tmp_path) -> None:
    task = task_contract()
    run = manifest().model_copy(update={"repetitions": 1})
    decision = evaluate_release(
        run,
        (task,),
        (trial_result(run, task, 0, ResultStatus.FAIL),),
        evaluated_at=datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
    )
    written = write_release_decision(decision, tmp_path)
    machine = json.loads(written.json_path.read_text(encoding="utf-8"))
    markdown = written.markdown_path.read_text(encoding="utf-8")
    assert machine["status"] == "fail"
    assert machine["run_manifest_digest"] == decision.run_manifest_digest
    assert markdown == render_release_markdown(decision)
    assert "## Vetoes" in markdown
    assert "VETO_FAILURE" not in markdown
    assert "`veto_failure`" in markdown
    assert written.decision_digest == content_fingerprint(decision)
