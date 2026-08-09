from __future__ import annotations

import json

import pytest

from eval.core import ResultStatus, TrialExecutionContext
from eval.core.framework_artifacts import FrameworkArtifactError
from eval.core.tests._support import prepared_case
from eval.harbor import HarborEvidenceAdapter, HarborImportSpec


async def _context(tmp_path, document):
    store, _, task, request = await prepared_case(
        tmp_path / "store", adapter=HarborEvidenceAdapter.identity
    )
    grader = task.graders[0].model_copy(
        update={"implementation": "eval.harbor.adapter:HarborEvidenceAdapter"}
    )
    task = task.model_copy(update={"task_id": "harbor.shell", "graders": (grader,)})
    request = request.model_copy(update={"task": task, "adapter": HarborEvidenceAdapter.identity})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "result.json").write_text(json.dumps(document), encoding="utf-8")
    spec = HarborImportSpec(
        contract_id=task.task_id,
        result_path="result.json",
        expected_task_name="shell",
        expected_task_checksum="sha256:task-v1",
        expected_trial_name="shell__abc",
    )
    return store, TrialExecutionContext(
        request=request,
        attempt_id="attempt-harbor",
        attempt=1,
        workspace=workspace,
        instruction=spec.model_dump_json().encode(),
        artifacts=store,
    )


def _result(*, model="deepseek-v4-flash", reward=1.0):
    return {
        "task_name": "shell",
        "task_checksum": "sha256:task-v1",
        "trial_name": "shell__abc",
        "agent_info": {
            "name": "cc-harness",
            "version": "0.1.0",
            "model_info": {"name": model, "provider": "deepseek"},
        },
        "agent_result": {"n_input_tokens": 100, "n_output_tokens": 20, "cost_usd": 0.001},
        "verifier_result": {"rewards": {"reward": reward}},
        "exception_info": None,
        "started_at": "2026-08-04T01:00:00Z",
        "finished_at": "2026-08-04T01:00:03Z",
    }


@pytest.mark.asyncio
async def test_imports_real_harbor_trial_shape(tmp_path):
    store, context = await _context(tmp_path, _result())
    try:
        result = await HarborEvidenceAdapter().run_trial(context)
        assert result.status is ResultStatus.PASS
        assert result.usage.input_tokens == 100
        assert result.usage.wall_time_ms == 3000
        assert result.artifacts
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_rejects_harbor_model_mismatch(tmp_path):
    store, context = await _context(tmp_path, _result(model="fallback-model"))
    try:
        with pytest.raises(FrameworkArtifactError, match="model mismatch"):
            await HarborEvidenceAdapter().run_trial(context)
    finally:
        await store.close()
