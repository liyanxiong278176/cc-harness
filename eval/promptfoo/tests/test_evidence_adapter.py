from __future__ import annotations

import json

import pytest

from eval.core import CapabilityDomain, EvalStore, ResultStatus, TrialExecutionContext
from eval.core.framework_artifacts import FrameworkArtifactError
from eval.core.tests._support import prepared_case
from eval.promptfoo import (
    PromptfooEvidenceAdapter,
    PromptfooImportSpec,
    install_promptfoo_security_contract,
)


async def _context(tmp_path, document: dict, *, threshold: float = 1.0):
    store, _, task, request = await prepared_case(
        tmp_path / "store", adapter=PromptfooEvidenceAdapter.identity
    )
    grader = task.graders[0].model_copy(
        update={
            "implementation": "eval.promptfoo.adapter:PromptfooEvidenceAdapter",
            "success_threshold": threshold,
        }
    )
    task = task.model_copy(update={"task_id": "promptfoo.security", "graders": (grader,)})
    request = request.model_copy(
        update={"task": task, "adapter": PromptfooEvidenceAdapter.identity}
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "promptfoo.json").write_text(json.dumps(document), encoding="utf-8")
    (workspace / "report.md").write_text("# Promptfoo report", encoding="utf-8")
    (workspace / "trajectory.jsonl").write_text('{"type":"result"}\n', encoding="utf-8")
    spec = PromptfooImportSpec(
        contract_id=task.task_id,
        results_path="promptfoo.json",
        artifact_paths=("report.md",),
        trajectory_path="trajectory.jsonl",
        minimum_test_count=2,
    )
    context = TrialExecutionContext(
        request=request,
        attempt_id="attempt-promptfoo-1",
        attempt=1,
        workspace=workspace,
        instruction=spec.model_dump_json().encode(),
        artifacts=store,
    )
    return store, context


def _document(*results: dict) -> dict:
    return {"results": {"results": list(results)}}


def _result(test_id: str, *, success: bool, severity: str, score: float) -> dict:
    return {
        "testId": test_id,
        "success": success,
        "score": score,
        "testCase": {"metadata": {"severity": severity}},
        "response": {
            "tokenUsage": {"prompt": 10, "completion": 4},
            "cost": 0.00002,
        },
    }


@pytest.mark.asyncio
async def test_imports_promptfoo_results_and_preserves_raw_artifacts(tmp_path):
    store, context = await _context(
        tmp_path,
        _document(
            _result("attack-1", success=True, severity="critical", score=1.0),
            _result("attack-2", success=True, severity="high", score=0.9),
        ),
    )
    try:
        result = await PromptfooEvidenceAdapter().run_trial(context)
        assert result.status is ResultStatus.PASS
        assert result.usage.input_tokens == 20
        assert result.usage.output_tokens == 8
        assert result.trajectory_ref is not None
        assert len(result.artifacts) == 2
        assert json.loads(await store.read_artifact(result.artifacts[0]))["results"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_critical_failure_is_a_veto_even_when_threshold_is_met(tmp_path):
    store, context = await _context(
        tmp_path,
        _document(
            _result("attack-1", success=False, severity="critical", score=0.0),
            _result("attack-2", success=True, severity="low", score=1.0),
        ),
        threshold=0.5,
    )
    try:
        result = await PromptfooEvidenceAdapter().run_trial(context)
        assert result.status is ResultStatus.FAIL
        assert result.grader_results[0].status is ResultStatus.FAIL
        assert result.failure is not None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_malformed_promptfoo_output_is_rejected(tmp_path):
    store, context = await _context(tmp_path, {"results": {"results": []}})
    try:
        with pytest.raises(FrameworkArtifactError, match="below the contract minimum"):
            await PromptfooEvidenceAdapter().run_trial(context)
    finally:
        await store.close()


def test_promptfoo_spec_rejects_workspace_escape():
    with pytest.raises(ValueError, match="unsafe framework artifact path"):
        PromptfooImportSpec(contract_id="promptfoo.security", results_path="../result.json")


@pytest.mark.asyncio
async def test_promptfoo_contract_connects_specialized_evidence_to_capability_domains(tmp_path):
    store = EvalStore(tmp_path / "catalog")
    await store.open()
    try:
        initial_state_ref = await store.put_artifact(b"", "application/x-empty-workspace")
        contract = await install_promptfoo_security_contract(store, initial_state_ref)
        assert set(contract.domains) == {
            CapabilityDomain.AGENT_LOOP,
            CapabilityDomain.SAFETY_AND_PRIVACY,
            CapabilityDomain.TOOLS_AND_PROTOCOLS,
        }
        assert contract.graders[0].veto is True
        instruction = await store.read_artifact(contract.instruction_ref)
        assert PromptfooImportSpec.model_validate_json(instruction).minimum_test_count == 40
    finally:
        await store.close()
