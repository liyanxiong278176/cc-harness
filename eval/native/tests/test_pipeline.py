from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from io import BytesIO

from eval.core import (
    AdapterIdentity,
    AggregateStatus,
    CapabilityDomain,
    DatasetSplit,
    EnvironmentSpec,
    EvalRunManifest,
    EvalStore,
    EvalTier,
    GraderContract,
    GraderType,
    IsolationType,
    LocalEvalRunner,
    ModelConfiguration,
    NetworkMode,
    ResourceBudget,
    RiskLevel,
    StateProfile,
    SubjectUnderTest,
    TaskContract,
    TrialRequest,
    canonical_json_bytes,
    content_fingerprint,
    evaluate_release,
    write_release_decision,
)
from eval.native import NativePytestAdapter, NativePytestSpec


def digest(character: str) -> str:
    return f"sha256:{character * 64}"


def source_archive() -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "tests/test_release_contract.py", "def test_release(): assert 2 + 2 == 4\n"
        )
    return buffer.getvalue()


async def test_durable_native_pipeline_produces_release_decision(tmp_path) -> None:
    store = EvalStore(tmp_path / "evidence")
    await store.open()
    try:
        state_ref = await store.put_artifact(source_archive(), "application/zip")
        spec = NativePytestSpec(
            contract_id="native.pipeline",
            test_targets=("tests/test_release_contract.py",),
        )
        instruction_ref = await store.put_artifact(
            canonical_json_bytes(spec),
            "application/vnd.cc-harness.native-pytest+json",
        )
        budget = ResourceBudget(
            wall_time_seconds=30,
            max_steps=1,
            max_model_calls=1,
            max_tool_calls=1,
            max_input_tokens=1,
            max_output_tokens=1,
            max_cost_microusd=0,
        )
        task = TaskContract(
            task_id=spec.contract_id,
            task_version="1.0.0",
            suite_id="native-pipeline",
            suite_version="1.0.0",
            title="End-to-end native release pipeline",
            risk=RiskLevel.CRITICAL,
            state_profile=StateProfile.CLEAN_CODING,
            domains=(CapabilityDomain.CODING_OUTCOME,),
            instruction_ref=instruction_ref,
            initial_state_ref=state_ref,
            budget=budget,
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
        model = ModelConfiguration(
            provider="deepseek",
            requested_model="deepseek-v4-flash",
            resolved_model="deepseek-v4-flash-20260801",
            api_protocol="openai-compatible",
            parameters_digest=digest("a"),
        )
        manifest = EvalRunManifest(
            run_id="run-native-pipeline",
            created_at=datetime(2026, 8, 3, 11, 0, tzinfo=UTC),
            tier=EvalTier.L1_PR,
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
                environment_id="process-test",
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
            default_budget=budget,
            random_seed=1,
            repetitions=1,
            orchestration_version="1.0.0",
            evidence_store_uri="file:///test/evidence",
        )
        manifest_digest = await store.create_run(manifest)
        await store.enqueue_trial(
            TrialRequest(
                trial_id="trial-native-pipeline",
                run_id=manifest.run_id,
                run_manifest_digest=manifest_digest,
                task=task,
                adapter=AdapterIdentity(adapter_id="native-pytest", adapter_version="1.0.0"),
                seed=1,
            )
        )
        runner = LocalEvalRunner(store, (NativePytestAdapter(),), worker_id="pipeline-worker")
        assert await runner.run_until_idle() == 1
        results = await store.list_results(manifest.run_id)
        decision = evaluate_release(
            await store.get_manifest(manifest.run_id),
            (task,),
            results,
            evaluated_at=datetime(2026, 8, 3, 11, 1, tzinfo=UTC),
        )
        decision_digest = await store.record_release_decision(decision)
        written = write_release_decision(decision, tmp_path / "report")
        assert decision.status is AggregateStatus.PASS
        assert decision.valid and decision.complete
        assert await store.read_release_decision(decision_digest) == decision
        assert any(
            event["event_type"] == "release_decision_recorded"
            for event in await store.lifecycle_events(manifest.run_id)
        )
        assert json.loads(written.json_path.read_text(encoding="utf-8"))["status"] == "pass"
        assert "**PASS**" in written.markdown_path.read_text(encoding="utf-8")
    finally:
        await store.close()
