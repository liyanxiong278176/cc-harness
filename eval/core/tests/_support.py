from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from eval.core import (
    AdapterIdentity,
    BudgetEnforcement,
    CapabilityDomain,
    DatasetSplit,
    EnvironmentSpec,
    EvalRunManifest,
    EvalStore,
    EvalTier,
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
    TrialRequest,
    TrialResult,
    content_fingerprint,
)
from eval.core.workspace import EMPTY_WORKSPACE_MEDIA_TYPE


def digest(character: str) -> str:
    return f"sha256:{character * 64}"


def budget(
    *,
    wall_time_seconds: int = 30,
    enforcement: BudgetEnforcement = BudgetEnforcement.ENFORCED,
    emergency_watchdog_seconds: int | None = None,
) -> ResourceBudget:
    return ResourceBudget(
        wall_time_seconds=wall_time_seconds,
        max_steps=100,
        max_model_calls=50,
        max_tool_calls=200,
        max_input_tokens=200_000,
        max_output_tokens=50_000,
        max_cost_microusd=2_000_000,
        enforcement=enforcement,
        emergency_watchdog_seconds=emergency_watchdog_seconds,
    )


async def prepared_case(
    root: Path,
    *,
    adapter: AdapterIdentity | None = None,
    initial_state: bytes = b"",
    initial_state_media_type: str = EMPTY_WORKSPACE_MEDIA_TYPE,
    wall_time_seconds: int = 30,
    enforcement: BudgetEnforcement = BudgetEnforcement.ENFORCED,
    emergency_watchdog_seconds: int | None = None,
) -> tuple[EvalStore, EvalRunManifest, TaskContract, TrialRequest]:
    store = EvalStore(root)
    await store.open()
    instruction_ref = await store.put_artifact(b"Fix the project", "text/plain")
    initial_state_ref = await store.put_artifact(initial_state, initial_state_media_type)
    task = TaskContract(
        task_id="native.runner-contract",
        task_version="1.0.0",
        suite_id="native-contracts",
        suite_version="1.0.0",
        title="Exercise the durable local runner",
        risk=RiskLevel.HIGH,
        state_profile=StateProfile.CLEAN_CODING,
        domains=(CapabilityDomain.RELIABILITY_AND_RECOVERY,),
        instruction_ref=instruction_ref,
        initial_state_ref=initial_state_ref,
        budget=budget(
            wall_time_seconds=wall_time_seconds,
            enforcement=enforcement,
            emergency_watchdog_seconds=emergency_watchdog_seconds,
        ),
        graders=(
            GraderContract(
                grader_id="outcome",
                grader_type=GraderType.DETERMINISTIC,
                implementation="eval.native:grade_outcome",
                version="1.0.0",
                domains=(CapabilityDomain.RELIABILITY_AND_RECOVERY,),
            ),
        ),
    )
    model = ModelConfiguration(
        provider="deepseek",
        requested_model="deepseek-v4-flash",
        resolved_model="deepseek-v4-flash-20260801",
        api_protocol="openai-compatible",
        parameters_digest=digest("a"),
    )
    manifest = EvalRunManifest(
        run_id="run-durable-001",
        created_at=datetime(2026, 8, 3, 9, 0, tzinfo=UTC),
        tier=EvalTier.L2_NIGHTLY,
        split=DatasetSplit.REGRESSION,
        subject=SubjectUnderTest(
            subject_id="cc-harness",
            product_version="0.1.0",
            source_commit="b" * 40,
            executable_digest=digest("b"),
            harness_profile_digest=digest("c"),
            model=model,
        ),
        task_contract_digests=(content_fingerprint(task),),
        environment=EnvironmentSpec(
            environment_id="test-sandbox-v1",
            isolation=IsolationType.PROCESS,
            os_name="windows",
            os_version="test",
            architecture="x86_64",
            image_digest=digest("d"),
            dependencies_digest=digest("e"),
            network_mode=NetworkMode.DISABLED,
            locale="en-US",
            timezone="UTC",
        ),
        default_budget=task.budget,
        random_seed=11,
        repetitions=1,
        orchestration_version="1.0.0",
        evidence_store_uri="file:///test/evidence",
    )
    manifest_digest = await store.create_run(manifest)
    request = TrialRequest(
        trial_id="trial-durable-001",
        run_id=manifest.run_id,
        run_manifest_digest=manifest_digest,
        task=task,
        adapter=adapter or AdapterIdentity(adapter_id="fake", adapter_version="1.0.0"),
        seed=11,
    )
    await store.enqueue_trial(request)
    return store, manifest, task, request


async def passing_result(store: EvalStore, lease) -> TrialResult:
    outcome_ref = await store.put_artifact(b'{"ok":true}', "application/json")
    return TrialResult(
        trial_id=lease.trial_id,
        run_id=lease.request.run_id,
        run_manifest_digest=lease.request.run_manifest_digest,
        task_id=lease.request.task.task_id,
        task_contract_digest=content_fingerprint(lease.request.task),
        attempt=lease.attempt,
        adapter=lease.request.adapter,
        status=ResultStatus.PASS,
        started_at=lease.claimed_at,
        finished_at=datetime.now(UTC),
        usage=ResourceUsage(
            wall_time_ms=10,
            steps=2,
            model_calls=1,
            tool_calls=1,
            input_tokens=10,
            output_tokens=5,
            cost_microusd=20,
        ),
        grader_results=(GraderResult(grader_id="outcome", status=ResultStatus.PASS, score=1.0),),
        outcome_ref=outcome_ref,
    )
