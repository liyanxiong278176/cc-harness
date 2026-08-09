from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime

import pytest

from eval.canary import HarnessCanaryAdapter, install_canary_contracts
from eval.canary.adapter import (
    _launch_failure_reason,
    _owned_sandbox_environment,
    is_transient_provider_result,
)
from eval.core import (
    AdapterIdentity,
    DatasetSplit,
    EvalRunManifest,
    EvalStore,
    EvalTier,
    LocalEvalRunner,
    ResourceUsage,
    ResultStatus,
    TrialRequest,
    TrialResult,
    content_fingerprint,
)
from eval.core.tests.test_models import manifest
from eval.launch import HarnessKind, LaunchEvidence, LaunchInvocation, standard_profiles


async def _prepared_adapter_case(tmp_path, monkeypatch, script: str):
    store = EvalStore(tmp_path / "evidence")
    await store.open()
    contract = (await install_canary_contracts(store))[0]
    profile = standard_profiles(cc_harness=sys.executable)[0]
    adapter = HarnessCanaryAdapter(profile, source_environment=os.environ)
    base = manifest(tier=EvalTier.L2_NIGHTLY)
    model = base.subject.model.model_copy(update={"resolved_model": "deepseek-v4-flash"})
    run = EvalRunManifest(
        **{
            **base.model_dump(mode="python"),
            "run_id": "canary-adapter-test",
            "created_at": datetime.now(UTC),
            "split": DatasetSplit.REGRESSION,
            "subject": base.subject.model_copy(update={"model": model}),
            "task_contract_digests": (content_fingerprint(contract),),
            "default_budget": contract.budget,
            "repetitions": 1,
        }
    )
    run_digest = await store.create_run(run)
    request = TrialRequest(
        trial_id="canary-adapter-trial",
        run_id=run.run_id,
        run_manifest_digest=run_digest,
        task=contract,
        adapter=adapter.identity,
        seed=7,
    )
    await store.enqueue_trial(request)

    def fake_invocation(profile, request, workspace, **kwargs):
        return LaunchInvocation(
            argv=(sys.executable, "-c", script),
            cwd=workspace,
            environment=dict(os.environ),
            stdin=request.prompt.encode("utf-8"),
        )

    monkeypatch.setattr("eval.canary.adapter.build_invocation", fake_invocation)
    runner = LocalEvalRunner(store, (adapter,), worker_id="canary-test-worker")
    return store, runner, request


async def test_adapter_persists_launch_grade_and_unknown_cost(tmp_path, monkeypatch) -> None:
    result_document = json.dumps(
        {
            "schema_version": "cc-harness.print-result.v1",
            "type": "result",
            "resolved_model": "deepseek-v4-flash",
            "usage": {
                "input_tokens": 13,
                "output_tokens": 5,
                "model_calls": 1,
                "tool_calls": 1,
            },
        }
    )
    script = (
        "from pathlib import Path; import sys; "
        "sys.stdin.read(); Path('solution.py').write_text("
        "'def add(a, b):\\n    return a + b\\n', encoding='utf-8'); "
        f"print({result_document!r})"
    )
    store, runner, request = await _prepared_adapter_case(tmp_path, monkeypatch, script)
    try:
        assert await runner.run_until_idle() == 1
        result = await store.get_result(request.trial_id)
        assert result is not None and result.status is ResultStatus.PASS
        assert result.usage.input_tokens == 13
        assert result.usage.cost_microusd is None
        assert result.outcome_ref is not None
        outcome = json.loads(await store.read_artifact(result.outcome_ref))
        assert outcome["protected_files_unchanged"] is True
        assert len(result.artifacts) >= 5
        assert {artifact.media_type for artifact in result.artifacts} >= {
            "application/vnd.cc-harness.launch-evidence+json",
            "application/x-ndjson",
            "text/plain",
        }
    finally:
        await store.close()


async def test_adapter_rejects_test_tampering_without_executing_it(tmp_path, monkeypatch) -> None:
    result_document = json.dumps(
        {
            "schema_version": "cc-harness.print-result.v1",
            "type": "result",
            "resolved_model": "deepseek-v4-flash",
            "usage": {"model_calls": 1},
        }
    )
    script = (
        "from pathlib import Path; import sys; sys.stdin.read(); "
        "Path('test_solution.py').write_text("
        "'raise RuntimeError(\\\"must not execute\\\")\\n', encoding='utf-8'); "
        f"print({result_document!r})"
    )
    store, runner, request = await _prepared_adapter_case(tmp_path, monkeypatch, script)
    try:
        await runner.run_until_idle()
        result = await store.get_result(request.trial_id)
        assert result is not None and result.status is ResultStatus.FAIL
        assert result.failure is not None
        assert "protected test files changed" in result.failure.message
    finally:
        await store.close()


def test_owned_sandbox_environment_is_unique_and_outside_workspace(tmp_path) -> None:
    workspace = tmp_path / "workspaces" / "trial"
    workspace.mkdir(parents=True)

    first, first_config = _owned_sandbox_environment({"PATH": "bin"}, workspace)
    second, second_config = _owned_sandbox_environment({"PATH": "bin"}, workspace)

    assert first_config == second_config
    assert workspace not in first_config.parents
    assert first["CC_HARNESS_SANDBOX_SERVER_CONFIG_PATH"] == str(first_config)
    assert first["CC_HARNESS_SANDBOX_SERVER_PORT"] != second[
        "CC_HARNESS_SANDBOX_SERVER_PORT"
    ]


def test_outer_wall_timeout_is_invalid_but_not_provider_transient() -> None:
    evidence = LaunchEvidence(
        harness=HarnessKind.CLAUDE_CODE,
        requested_model="deepseek-v4-flash",
        exit_code=1,
        timed_out=True,
        wall_time_ms=149_000,
    )
    reason = _launch_failure_reason(evidence, b"", b"")

    assert reason.startswith("launch wall-time timeout:")
    assert is_transient_provider_result(_invalid_result_for_reason(reason)) is False


def test_observational_timeout_is_labeled_as_emergency_watchdog() -> None:
    evidence = LaunchEvidence(
        harness=HarnessKind.CLAUDE_CODE,
        requested_model="deepseek-v4-flash",
        exit_code=1,
        timed_out=True,
        wall_time_ms=3_569_000,
    )

    reason = _launch_failure_reason(evidence, b"", b"", emergency_watchdog=True)

    assert reason.startswith("emergency watchdog timeout:")
    assert is_transient_provider_result(_invalid_result_for_reason(reason)) is False


def test_provider_marker_remains_transient_even_when_process_times_out() -> None:
    evidence = LaunchEvidence(
        harness=HarnessKind.CLAUDE_CODE,
        requested_model="deepseek-v4-flash",
        exit_code=1,
        timed_out=True,
        wall_time_ms=149_000,
    )
    reason = _launch_failure_reason(evidence, b"", b"503 service unavailable")

    assert reason.startswith("transient provider failure:")
    assert "provider marker '503 service unavailable'" in reason


@pytest.mark.parametrize(
    "output",
    (
        b'{"uuid":"503cf02b-6c90-4939-8887-c326787b1bee"}',
        b'{"estimated_tokens":503,"estimated_tokens_delta":1}',
        b"the task validates a configured status code of 503",
    ),
)
def test_bare_provider_code_substrings_are_not_transient(output: bytes) -> None:
    evidence = LaunchEvidence(
        harness=HarnessKind.CLAUDE_CODE,
        requested_model="deepseek-v4-flash",
        exit_code=1,
        timed_out=True,
        wall_time_ms=149_000,
    )
    reason = _launch_failure_reason(evidence, output, b"")

    assert reason.startswith("launch wall-time timeout:")


@pytest.mark.parametrize(
    "output",
    (
        b"HTTP status 429",
        b"API error: 503",
        b"connection reset by peer",
        b'{"error":{"message":"Overloaded"}}',
    ),
)
def test_semantic_provider_errors_are_transient(output: bytes) -> None:
    evidence = LaunchEvidence(
        harness=HarnessKind.CLAUDE_CODE,
        requested_model="deepseek-v4-flash",
        exit_code=1,
        timed_out=True,
        wall_time_ms=149_000,
    )

    reason = _launch_failure_reason(evidence, b"", output)

    assert reason.startswith("transient provider failure:")


def _invalid_result_for_reason(reason: str) -> TrialResult:
    now = datetime.now(UTC)
    return TrialResult(
        trial_id="timeout-trial",
        run_id="timeout-run",
        run_manifest_digest="sha256:" + "a" * 64,
        task_id="timeout-task",
        task_contract_digest="sha256:" + "b" * 64,
        attempt=1,
        adapter=AdapterIdentity(adapter_id="timeout", adapter_version="1.0.0"),
        status=ResultStatus.INVALID,
        started_at=now,
        finished_at=now,
        usage=ResourceUsage(
            wall_time_ms=149_000,
            steps=0,
            model_calls=0,
            tool_calls=0,
            input_tokens=0,
            output_tokens=0,
            cost_microusd=None,
        ),
        invalid_reason=reason,
    )
