from __future__ import annotations

from datetime import UTC, datetime

from eval.core import (
    CapabilityDomain,
    EvalStore,
    GraderContract,
    GraderType,
    ResourceBudget,
    ResultStatus,
    RiskLevel,
    StateProfile,
    TaskContract,
    TrialExecutionContext,
    TrialRequest,
    canonical_json_bytes,
)
from eval.core.workspace import EMPTY_WORKSPACE_MEDIA_TYPE
from eval.native import NativePytestAdapter, NativePytestSpec


async def make_context(
    store: EvalStore, workspace, test_source: str, *, output_limit: int = 10_000
):
    tests = workspace / "tests"
    tests.mkdir(parents=True)
    (tests / "test_contract.py").write_text(test_source, encoding="utf-8")
    spec = NativePytestSpec(
        contract_id="native.test-contract",
        test_targets=("tests/test_contract.py",),
        output_limit_bytes=output_limit,
    )
    instruction = canonical_json_bytes(spec)
    instruction_ref = await store.put_artifact(
        instruction,
        "application/vnd.cc-harness.native-pytest+json",
    )
    initial = await store.put_artifact(b"", EMPTY_WORKSPACE_MEDIA_TYPE)
    task = TaskContract(
        task_id=spec.contract_id,
        task_version="1.0.0",
        suite_id="native-test",
        suite_version="1.0.0",
        title="Native adapter fixture",
        risk=RiskLevel.CRITICAL,
        state_profile=StateProfile.CLEAN_CODING,
        domains=(CapabilityDomain.CODING_OUTCOME,),
        instruction_ref=instruction_ref,
        initial_state_ref=initial,
        budget=ResourceBudget(
            wall_time_seconds=30,
            max_steps=1,
            max_model_calls=1,
            max_tool_calls=1,
            max_input_tokens=1,
            max_output_tokens=1,
            max_cost_microusd=0,
        ),
        graders=(
            GraderContract(
                grader_id="pytest-exit",
                grader_type=GraderType.DETERMINISTIC,
                implementation="eval.native.adapter:NativePytestAdapter",
                version="1.0.0",
                domains=(CapabilityDomain.CODING_OUTCOME,),
                veto=True,
            ),
        ),
    )
    request = TrialRequest(
        trial_id="trial-native-001",
        run_id="run-native-001",
        run_manifest_digest=f"sha256:{'a' * 64}",
        task=task,
        adapter=NativePytestAdapter.identity,
        seed=1,
    )
    return TrialExecutionContext(
        request=request,
        attempt_id="attempt-native-001",
        attempt=1,
        workspace=workspace,
        instruction=instruction,
        artifacts=store,
    )


async def test_native_adapter_passes_on_expected_pytest_exit(tmp_path) -> None:
    store = EvalStore(tmp_path / "evidence")
    await store.open()
    try:
        context = await make_context(store, tmp_path / "workspace", "def test_ok(): assert True\n")
        result = await NativePytestAdapter().run_trial(context)
        assert result.status is ResultStatus.PASS
        assert result.grader_results[0].status is ResultStatus.PASS
        assert result.usage.model_calls == 0
        assert await store.read_artifact(result.outcome_ref)
    finally:
        await store.close()


async def test_native_adapter_records_objective_failure(tmp_path) -> None:
    store = EvalStore(tmp_path / "evidence")
    await store.open()
    try:
        context = await make_context(
            store, tmp_path / "workspace", "def test_bad(): assert False\n"
        )
        result = await NativePytestAdapter().run_trial(context)
        assert result.status is ResultStatus.FAIL
        assert result.failure is not None and result.failure.category == "pytest-exit"
        assert result.grader_results[0].status is ResultStatus.FAIL
    finally:
        await store.close()


async def test_native_adapter_invalidates_truncated_evidence(tmp_path) -> None:
    store = EvalStore(tmp_path / "evidence")
    await store.open()
    try:
        source = "def test_output():\n    assert False, 'x' * 10000\n"
        context = await make_context(
            store,
            tmp_path / "workspace",
            source,
            output_limit=100,
        )
        started = datetime.now(UTC)
        result = await NativePytestAdapter().run_trial(context)
        assert result.finished_at >= started
        assert result.status is ResultStatus.INVALID
        assert "capture limit" in result.invalid_reason
    finally:
        await store.close()
