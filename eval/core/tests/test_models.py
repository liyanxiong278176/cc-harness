from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from eval.core import (
    AdapterIdentity,
    ArtifactRef,
    BudgetEnforcement,
    CapabilityDomain,
    DatasetSplit,
    EnvironmentSpec,
    EvalRunManifest,
    EvalTier,
    FailureRecord,
    GraderContract,
    GraderResult,
    GraderType,
    IsolationType,
    ModelConfiguration,
    NetworkMode,
    ResourceBudget,
    ResourceUsage,
    ResultStatus,
    RiskLevel,
    StateProfile,
    SubjectUnderTest,
    TaskContract,
    TrialResult,
    content_fingerprint,
)


def digest(character: str) -> str:
    return f"sha256:{character * 64}"


def artifact(character: str = "a") -> ArtifactRef:
    return ArtifactRef(
        digest=digest(character),
        media_type="application/json",
        size_bytes=12,
    )


def budget() -> ResourceBudget:
    return ResourceBudget(
        wall_time_seconds=600,
        max_steps=100,
        max_model_calls=50,
        max_tool_calls=200,
        max_input_tokens=200_000,
        max_output_tokens=50_000,
        max_cost_microusd=2_000_000,
    )


def test_observational_budget_requires_and_uses_emergency_watchdog() -> None:
    with pytest.raises(ValidationError, match="emergency watchdog"):
        ResourceBudget.model_validate(
            {
                **budget().model_dump(),
                "enforcement": BudgetEnforcement.OBSERVE,
                "emergency_watchdog_seconds": None,
            }
        )

    observed = ResourceBudget(
        wall_time_seconds=1,
        max_steps=1,
        max_model_calls=1,
        max_tool_calls=1,
        max_input_tokens=1,
        max_output_tokens=1,
        max_cost_microusd=0,
        enforcement=BudgetEnforcement.OBSERVE,
        emergency_watchdog_seconds=600,
    )

    assert observed.execution_timeout_seconds == 600


def task_contract() -> TaskContract:
    return TaskContract(
        task_id="native.tui-scroll",
        task_version="1.0.0",
        suite_id="native-contracts",
        suite_version="1.0.0",
        title="Scroll through a long terminal transcript",
        risk=RiskLevel.HIGH,
        state_profile=StateProfile.CONTEXT,
        domains=(CapabilityDomain.HUMAN_INTERACTION,),
        instruction_ref=artifact("a"),
        initial_state_ref=artifact("b"),
        budget=budget(),
        graders=(
            GraderContract(
                grader_id="terminal-state",
                grader_type=GraderType.DETERMINISTIC,
                implementation="eval.native.tui:grade_scroll",
                version="1.0.0",
                domains=(CapabilityDomain.HUMAN_INTERACTION,),
                veto=True,
            ),
        ),
        tags=("tui", "release"),
    )


def manifest(*, tier: EvalTier = EvalTier.L2_NIGHTLY) -> EvalRunManifest:
    model = ModelConfiguration(
        provider="deepseek",
        requested_model="deepseek-v4-flash",
        resolved_model="deepseek-v4-flash-20260801",
        api_protocol="openai-compatible",
        parameters_digest=digest("c"),
    )
    return EvalRunManifest(
        run_id="run-20260803-001",
        created_at=datetime(2026, 8, 3, 9, 0, tzinfo=UTC),
        tier=tier,
        split=DatasetSplit.REGRESSION,
        comparison_group_id="parity-001",
        subject=SubjectUnderTest(
            subject_id="cc-harness",
            product_version="0.1.0",
            source_commit="d" * 40,
            executable_digest=digest("d"),
            harness_profile_digest=digest("e"),
            model=model,
        ),
        task_contract_digests=(content_fingerprint(task_contract()),),
        environment=EnvironmentSpec(
            environment_id="windows-sandbox-v1",
            isolation=IsolationType.VIRTUAL_MACHINE,
            os_name="windows",
            os_version="11-24H2",
            architecture="x86_64",
            image_digest=digest("f"),
            dependencies_digest=digest("0"),
            network_mode=NetworkMode.ALLOWLIST,
            locale="zh-CN",
            timezone="Asia/Shanghai",
        ),
        default_budget=budget(),
        random_seed=7,
        repetitions=3,
        orchestration_version="1.0.0",
        evidence_store_uri="file:///eval/evidence/run-20260803-001",
    )


def usage() -> ResourceUsage:
    return ResourceUsage(
        wall_time_ms=1_200,
        steps=2,
        model_calls=3,
        tool_calls=4,
        input_tokens=100,
        output_tokens=30,
        cost_microusd=400,
    )


def trial_fields() -> dict:
    run = manifest()
    task = task_contract()
    return {
        "trial_id": "trial-001",
        "run_id": run.run_id,
        "run_manifest_digest": content_fingerprint(run),
        "task_id": task.task_id,
        "task_contract_digest": content_fingerprint(task),
        "attempt": 1,
        "adapter": AdapterIdentity(adapter_id="native", adapter_version="1.0.0"),
        "started_at": datetime(2026, 8, 3, 9, 1, tzinfo=UTC),
        "finished_at": datetime(2026, 8, 3, 9, 2, tzinfo=UTC),
        "usage": usage(),
    }


def test_task_contract_is_frozen_and_forbids_unknown_fields() -> None:
    task = task_contract()

    with pytest.raises(ValidationError, match="frozen"):
        task.title = "Changed after the run"  # type: ignore[misc]

    payload = task.model_dump(mode="python")
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TaskContract.model_validate(payload)


def test_exported_task_schema_is_closed_and_versioned() -> None:
    schema = TaskContract.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == "eval.task.v1"


def test_task_contract_rejects_duplicate_or_undeclared_grader_domains() -> None:
    task = task_contract()
    duplicate = task.model_copy(update={"domains": (task.domains[0], task.domains[0])})
    with pytest.raises(ValidationError, match="task domains must be unique"):
        TaskContract.model_validate(duplicate.model_dump(mode="python"))

    foreign_grader = task.graders[0].model_copy(
        update={"domains": (CapabilityDomain.SAFETY_AND_PRIVACY,)}
    )
    payload = task.model_dump(mode="python")
    payload["graders"] = (foreign_grader,)
    with pytest.raises(ValidationError, match="declared by the task"):
        TaskContract.model_validate(payload)


def test_non_deterministic_grader_requires_versioned_rubric() -> None:
    with pytest.raises(ValidationError, match="require a rubric_ref"):
        GraderContract(
            grader_id="judge",
            grader_type=GraderType.LLM_JUDGE,
            implementation="eval.judges:score",
            version="1.0.0",
            domains=(CapabilityDomain.CODING_OUTCOME,),
        )


def test_dirty_subject_requires_content_addressed_patch() -> None:
    subject = manifest().subject
    payload = subject.model_dump(mode="python")
    payload["source_dirty"] = True
    with pytest.raises(ValidationError, match="requires source_patch_ref"):
        SubjectUnderTest.model_validate(payload)


def test_manifest_requires_aware_time_unique_tasks_and_valid_release_split() -> None:
    run = manifest()
    payload = run.model_dump(mode="python")
    payload["created_at"] = run.created_at.replace(tzinfo=None)
    with pytest.raises(ValidationError, match="timezone-aware"):
        EvalRunManifest.model_validate(payload)

    payload = run.model_dump(mode="python")
    payload["task_contract_digests"] = (
        run.task_contract_digests[0],
        run.task_contract_digests[0],
    )
    with pytest.raises(ValidationError, match="must be unique"):
        EvalRunManifest.model_validate(payload)

    payload = manifest(tier=EvalTier.L4_RELEASE).model_dump(mode="python")
    payload["split"] = DatasetSplit.DEVELOPMENT
    with pytest.raises(ValidationError, match="cannot use the development split"):
        EvalRunManifest.model_validate(payload)


def test_task_and_manifest_round_trip_without_fingerprint_drift() -> None:
    task = task_contract()
    restored_task = TaskContract.model_validate_json(task.model_dump_json())
    assert restored_task == task
    assert content_fingerprint(restored_task) == content_fingerprint(task)

    run = manifest()
    restored_run = EvalRunManifest.model_validate_json(run.model_dump_json())
    assert restored_run == run
    assert content_fingerprint(restored_run) == content_fingerprint(run)


def test_pass_trial_requires_objective_outcome_and_only_passing_graders() -> None:
    fields = trial_fields()
    passed = TrialResult(
        **fields,
        status=ResultStatus.PASS,
        outcome_ref=artifact("1"),
        grader_results=(
            GraderResult(grader_id="terminal-state", status=ResultStatus.PASS, score=1.0),
        ),
    )
    assert passed.status is ResultStatus.PASS

    with pytest.raises(ValidationError, match="require outcome_ref"):
        TrialResult(
            **fields,
            status=ResultStatus.PASS,
            grader_results=(GraderResult(grader_id="terminal-state", status=ResultStatus.PASS),),
        )

    with pytest.raises(ValidationError, match="only passed grader results"):
        TrialResult(
            **fields,
            status=ResultStatus.PASS,
            outcome_ref=artifact("1"),
            grader_results=(GraderResult(grader_id="terminal-state", status=ResultStatus.FAIL),),
        )

    with pytest.raises(ValidationError, match="cannot contain invalid grader results"):
        TrialResult(
            **fields,
            status=ResultStatus.FAIL,
            outcome_ref=artifact("1"),
            grader_results=(GraderResult(grader_id="terminal-state", status=ResultStatus.INVALID),),
            failure=FailureRecord(category="grader", message="Grader did not complete"),
        )


def test_fail_and_invalid_are_distinct_terminal_states() -> None:
    fields = trial_fields()
    failed = TrialResult(
        **fields,
        status=ResultStatus.FAIL,
        outcome_ref=artifact("2"),
        grader_results=(
            GraderResult(grader_id="terminal-state", status=ResultStatus.FAIL, score=0.0),
        ),
        failure=FailureRecord(category="assertion", message="Terminal state was not restored"),
    )
    assert failed.invalid_reason is None

    invalid = TrialResult(
        **fields,
        status=ResultStatus.INVALID,
        invalid_reason="Sandbox provisioning failed before agent launch",
    )
    assert invalid.failure is None

    with pytest.raises(ValidationError, match="invalid trials require invalid_reason"):
        TrialResult(**fields, status=ResultStatus.INVALID)


def test_trial_rejects_reversed_time_and_duplicate_evidence_names() -> None:
    fields = trial_fields()
    fields["finished_at"] = fields["started_at"] - timedelta(seconds=1)
    with pytest.raises(ValidationError, match="must not precede"):
        TrialResult(
            **fields,
            status=ResultStatus.INVALID,
            invalid_reason="Clock evidence is not valid",
        )
