from __future__ import annotations

import asyncio
import shutil
import zipfile
from datetime import UTC, datetime
from io import BytesIO

from eval.core import (
    AdapterIdentity,
    ArtifactRef,
    BudgetEnforcement,
    GraderResult,
    LocalEvalRunner,
    ResourceUsage,
    ResultStatus,
    RunState,
    TrialExecutionContext,
    TrialResult,
    TrialState,
    content_fingerprint,
)
from eval.core.workspace import DisposableWorkspaceManager

from ._support import prepared_case


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class PassingAdapter:
    identity = AdapterIdentity(adapter_id="fake", adapter_version="1.0.0")

    def __init__(self) -> None:
        self.workspace = None
        self.saw_fixture = False

    async def run_trial(self, context: TrialExecutionContext) -> TrialResult:
        self.workspace = context.workspace
        self.saw_fixture = (context.workspace / "project" / "fixture.txt").read_text() == "ready"
        assert context.instruction == b"Fix the project"
        outcome = await context.artifacts.put_artifact(b'{"ok":true}', "application/json")
        return passing_trial_result(context, outcome)


class FailingAdapter:
    identity = AdapterIdentity(adapter_id="fake", adapter_version="1.0.0")

    async def run_trial(self, context: TrialExecutionContext) -> TrialResult:
        raise RuntimeError("adapter exploded")


class MissingEvidenceAdapter:
    identity = AdapterIdentity(adapter_id="fake", adapter_version="1.0.0")

    async def run_trial(self, context: TrialExecutionContext) -> TrialResult:
        missing = ArtifactRef(
            digest=f"sha256:{'f' * 64}",
            media_type="application/json",
            size_bytes=1,
        )
        return passing_trial_result(context, missing)


class OverBudgetAdapter:
    identity = AdapterIdentity(adapter_id="fake", adapter_version="1.0.0")

    async def run_trial(self, context: TrialExecutionContext) -> TrialResult:
        outcome = await context.artifacts.put_artifact(b'{"ok":true}', "application/json")
        result = passing_trial_result(context, outcome)
        usage = result.usage.model_copy(update={"model_calls": 51})
        return result.model_copy(update={"usage": usage})


class SlowAdapter:
    identity = AdapterIdentity(adapter_id="fake", adapter_version="1.0.0")

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run_trial(self, context: TrialExecutionContext) -> TrialResult:
        self.started.set()
        await asyncio.sleep(10)
        raise AssertionError("slow adapter should have been cancelled")


class MustNotRunAdapter:
    identity = AdapterIdentity(adapter_id="fake", adapter_version="1.0.0")

    async def run_trial(self, context: TrialExecutionContext) -> TrialResult:
        raise AssertionError("unsafe workspace should fail before adapter dispatch")


def passing_trial_result(
    context: TrialExecutionContext,
    outcome: ArtifactRef,
) -> TrialResult:
    request = context.request
    return TrialResult(
        trial_id=request.trial_id,
        run_id=request.run_id,
        run_manifest_digest=request.run_manifest_digest,
        task_id=request.task.task_id,
        task_contract_digest=content_fingerprint(request.task),
        attempt=context.attempt,
        adapter=request.adapter,
        status=ResultStatus.PASS,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        usage=ResourceUsage(
            wall_time_ms=1,
            steps=1,
            model_calls=1,
            tool_calls=1,
            input_tokens=1,
            output_tokens=1,
            cost_microusd=1,
        ),
        grader_results=(GraderResult(grader_id="outcome", status=ResultStatus.PASS, score=1.0),),
        outcome_ref=outcome,
    )


async def test_runner_materializes_and_removes_disposable_workspace(tmp_path) -> None:
    initial = zip_bytes({"project/fixture.txt": b"ready"})
    store, manifest, _, request = await prepared_case(
        tmp_path,
        initial_state=initial,
        initial_state_media_type="application/zip",
    )
    adapter = PassingAdapter()
    runner = LocalEvalRunner(
        store,
        (adapter,),
        worker_id="worker-1",
        heartbeat_interval_seconds=0.02,
        stale_after_seconds=1.0,
    )
    try:
        assert await runner.run_until_idle() == 1
        assert adapter.saw_fixture is True
        assert adapter.workspace is not None and not adapter.workspace.exists()
        result = await store.get_result(request.trial_id)
        assert result is not None and result.status is ResultStatus.PASS
        assert await store.get_run_state(manifest.run_id) is RunState.COMPLETED
    finally:
        await store.close()


def test_workspace_cleanup_retries_transient_windows_error(tmp_path, monkeypatch) -> None:
    path = tmp_path / "workspace"
    path.mkdir()
    calls = 0
    real_rmtree = shutil.rmtree

    def flaky_rmtree(target):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError("temporary handle")
        real_rmtree(target)

    monkeypatch.setattr(shutil, "rmtree", flaky_rmtree)

    DisposableWorkspaceManager._remove_workspace(path, attempts=3)

    assert calls == 3
    assert not path.exists()


async def test_adapter_exception_becomes_invalid_trial(tmp_path) -> None:
    store, _, _, request = await prepared_case(tmp_path)
    runner = LocalEvalRunner(store, (FailingAdapter(),), worker_id="worker-1")
    try:
        await runner.run_until_idle()
        result = await store.get_result(request.trial_id)
        assert result is not None and result.status is ResultStatus.INVALID
        assert "RuntimeError" in result.invalid_reason
    finally:
        await store.close()


async def test_missing_adapter_artifact_fails_closed_as_invalid(tmp_path) -> None:
    store, _, _, request = await prepared_case(tmp_path)
    runner = LocalEvalRunner(store, (MissingEvidenceAdapter(),), worker_id="worker-1")
    try:
        await runner.run_until_idle()
        result = await store.get_result(request.trial_id)
        assert result is not None and result.status is ResultStatus.INVALID
        assert "invalid evidence" in result.invalid_reason
    finally:
        await store.close()


async def test_reported_usage_over_contract_budget_is_invalid(tmp_path) -> None:
    store, _, _, request = await prepared_case(tmp_path)
    runner = LocalEvalRunner(store, (OverBudgetAdapter(),), worker_id="worker-1")
    try:
        await runner.run_until_idle()
        result = await store.get_result(request.trial_id)
        assert result is not None and result.status is ResultStatus.INVALID
        assert "model_calls=51>50" in result.invalid_reason
        assert result.usage.model_calls == 51
        assert result.outcome_ref is not None
    finally:
        await store.close()


async def test_running_trial_cancellation_stops_adapter_and_records_invalid(tmp_path) -> None:
    store, _, _, request = await prepared_case(tmp_path)
    adapter = SlowAdapter()
    runner = LocalEvalRunner(
        store,
        (adapter,),
        worker_id="worker-1",
        heartbeat_interval_seconds=0.02,
        stale_after_seconds=1.0,
    )
    try:
        running = asyncio.create_task(runner.run_until_idle())
        await asyncio.wait_for(adapter.started.wait(), timeout=1)
        assert await store.request_cancel(request.trial_id) is True
        assert await running == 1
        assert await store.get_trial_state(request.trial_id) is TrialState.COMPLETED
        result = await store.get_result(request.trial_id)
        assert result is not None and result.status is ResultStatus.INVALID
        assert "cancelled" in result.invalid_reason
    finally:
        await store.close()


async def test_wall_time_budget_is_enforced(tmp_path) -> None:
    store, _, _, request = await prepared_case(tmp_path, wall_time_seconds=1)
    runner = LocalEvalRunner(
        store,
        (SlowAdapter(),),
        worker_id="worker-1",
        heartbeat_interval_seconds=0.02,
        stale_after_seconds=1.0,
    )
    try:
        await runner.run_until_idle()
        result = await store.get_result(request.trial_id)
        assert result is not None and result.status is ResultStatus.INVALID
        assert "wall-time" in result.invalid_reason
    finally:
        await store.close()


async def test_observational_budget_records_overage_without_invalidating(tmp_path) -> None:
    store, _, _, _ = await prepared_case(
        tmp_path,
        enforcement=BudgetEnforcement.OBSERVE,
        emergency_watchdog_seconds=5,
    )
    runner = LocalEvalRunner(store, (OverBudgetAdapter(),), worker_id="worker-1")
    try:
        await runner.run_until_idle()
        result = await store.get_result("trial-durable-001")
        assert result is not None and result.status is ResultStatus.PASS
        assert result.usage.model_calls == 51
    finally:
        await store.close()


async def test_observational_budget_still_enforces_emergency_watchdog(tmp_path) -> None:
    store, _, _, _ = await prepared_case(
        tmp_path,
        enforcement=BudgetEnforcement.OBSERVE,
        emergency_watchdog_seconds=1,
    )
    runner = LocalEvalRunner(
        store,
        (SlowAdapter(),),
        worker_id="worker-1",
        heartbeat_interval_seconds=0.02,
        stale_after_seconds=1.0,
    )
    try:
        await runner.run_until_idle()
        result = await store.get_result("trial-durable-001")
        assert result is not None and result.status is ResultStatus.INVALID
        assert "emergency watchdog" in result.invalid_reason
    finally:
        await store.close()


async def test_workspace_path_traversal_is_rejected_before_dispatch(tmp_path) -> None:
    initial = zip_bytes({"../escaped.txt": b"bad"})
    store, _, _, request = await prepared_case(
        tmp_path,
        initial_state=initial,
        initial_state_media_type="application/zip",
    )
    runner = LocalEvalRunner(store, (MustNotRunAdapter(),), worker_id="worker-1")
    try:
        await runner.run_until_idle()
        result = await store.get_result(request.trial_id)
        assert result is not None and result.status is ResultStatus.INVALID
        assert "escapes destination" in result.invalid_reason
        assert not (store.workspaces_root / "escaped.txt").exists()
    finally:
        await store.close()
