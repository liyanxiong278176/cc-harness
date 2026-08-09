from __future__ import annotations

import json

import pytest

from eval.core import ResultStatus, TrialExecutionContext
from eval.core.framework_artifacts import FrameworkArtifactError
from eval.core.tests._support import prepared_case
from eval.swebench import SwebenchEvidenceAdapter, SwebenchImportSpec


async def _context(tmp_path, report, *, predictions=None):
    store, _, task, request = await prepared_case(
        tmp_path / "store", adapter=SwebenchEvidenceAdapter.identity
    )
    grader = task.graders[0].model_copy(
        update={"implementation": "eval.swebench.adapter:SwebenchEvidenceAdapter"}
    )
    task = task.model_copy(update={"task_id": "swebench.django-1", "graders": (grader,)})
    request = request.model_copy(update={"task": task, "adapter": SwebenchEvidenceAdapter.identity})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prediction = predictions or [
        {
            "instance_id": "django-1",
            "model_name_or_path": "deepseek-v4-flash",
            "model_patch": "diff --git a/a b/a",
        }
    ]
    (workspace / "predictions.jsonl").write_text(
        "\n".join(json.dumps(item) for item in prediction), encoding="utf-8"
    )
    (workspace / "report.json").write_text(json.dumps(report), encoding="utf-8")
    spec = SwebenchImportSpec(
        contract_id=task.task_id,
        instance_id="django-1",
        prediction_path="predictions.jsonl",
        report_path="report.json",
    )
    return store, TrialExecutionContext(
        request=request,
        attempt_id="attempt-swe",
        attempt=1,
        workspace=workspace,
        instruction=spec.model_dump_json().encode(),
        artifacts=store,
    )


def _report(resolved=True):
    return {
        "django-1": {
            "patch_is_None": False,
            "patch_exists": True,
            "patch_successfully_applied": True,
            "resolved": resolved,
        }
    }


@pytest.mark.asyncio
async def test_imports_swebench_verified_report_and_prediction(tmp_path):
    store, context = await _context(tmp_path, _report())
    try:
        result = await SwebenchEvidenceAdapter().run_trial(context)
        assert result.status is ResultStatus.PASS
        assert len(result.artifacts) == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_rejects_extra_swebench_instances(tmp_path):
    report = _report()
    report["other-2"] = report["django-1"]
    store, context = await _context(tmp_path, report)
    try:
        with pytest.raises(FrameworkArtifactError, match="exactly"):
            await SwebenchEvidenceAdapter().run_trial(context)
    finally:
        await store.close()
