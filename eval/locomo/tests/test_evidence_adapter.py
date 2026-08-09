from __future__ import annotations

import json

import pytest

from eval.core import CapabilityDomain, EvalStore, ResultStatus, TrialExecutionContext
from eval.core.framework_artifacts import FrameworkArtifactError
from eval.core.tests._support import prepared_case
from eval.locomo import (
    LocomoEvidenceAdapter,
    LocomoImportSpec,
    install_locomo_memory_contract,
)


async def _context(tmp_path, records: list[dict], *, threshold: float = 0.5):
    store, _, task, request = await prepared_case(
        tmp_path / "store", adapter=LocomoEvidenceAdapter.identity
    )
    grader = task.graders[0].model_copy(
        update={
            "implementation": "eval.locomo.adapter:LocomoEvidenceAdapter",
            "success_threshold": threshold,
        }
    )
    task = task.model_copy(update={"task_id": "locomo.memory", "graders": (grader,)})
    request = request.model_copy(update={"task": task, "adapter": LocomoEvidenceAdapter.identity})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "locomo-results.json").write_text(json.dumps(records), encoding="utf-8")
    (workspace / "locomo-metrics.json").write_text(
        json.dumps({"1_recall": {"recall": 0.8}}), encoding="utf-8"
    )
    (workspace / "locomo-report.html").write_text("<html></html>", encoding="utf-8")
    (workspace / "conv-1.trace.jsonl").write_text('{"type":"turn"}\n', encoding="utf-8")
    spec = LocomoImportSpec(
        contract_id=task.task_id,
        results_path="locomo-results.json",
        metrics_path="locomo-metrics.json",
        report_path="locomo-report.html",
        trajectory_paths=("conv-1.trace.jsonl",),
        minimum_qa_count=2,
        required_q_types=("1", "2"),
    )
    context = TrialExecutionContext(
        request=request,
        attempt_id="attempt-locomo-1",
        attempt=1,
        workspace=workspace,
        instruction=spec.model_dump_json().encode(),
        artifacts=store,
    )
    return store, context


def _record(q_type: str, *, passed: bool, status: str = "ok") -> dict:
    return {
        "sample_id": "conv-1",
        "q_type": q_type,
        "status": status,
        "pass": passed,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "cost_usd": 0.0001,
        "tool_calls": [{"name": "memory_recall"}],
    }


@pytest.mark.asyncio
async def test_imports_locomo_results_metrics_report_and_trajectory(tmp_path):
    store, context = await _context(
        tmp_path,
        [_record("1", passed=True), _record("2", passed=True)],
    )
    try:
        result = await LocomoEvidenceAdapter().run_trial(context)
        assert result.status is ResultStatus.PASS
        assert result.usage.input_tokens == 200
        assert result.usage.tool_calls == 2
        assert result.trajectory_ref is not None
        assert len(result.artifacts) == 3
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fatal_locomo_record_blocks_even_when_threshold_is_met(tmp_path):
    store, context = await _context(
        tmp_path,
        [_record("1", passed=False, status="agent_crash"), _record("2", passed=True)],
        threshold=0.5,
    )
    try:
        result = await LocomoEvidenceAdapter().run_trial(context)
        assert result.status is ResultStatus.FAIL
        assert result.failure is not None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_missing_required_q_type_is_rejected(tmp_path):
    store, context = await _context(
        tmp_path,
        [_record("1", passed=True), _record("1", passed=True)],
    )
    try:
        with pytest.raises(FrameworkArtifactError, match="missing q types"):
            await LocomoEvidenceAdapter().run_trial(context)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_locomo_contract_connects_specialized_evidence_to_capability_domains(tmp_path):
    store = EvalStore(tmp_path / "catalog")
    await store.open()
    try:
        initial_state_ref = await store.put_artifact(b"", "application/x-empty-workspace")
        contract = await install_locomo_memory_contract(store, initial_state_ref)
        assert set(contract.domains) == {
            CapabilityDomain.CONTEXT_MANAGEMENT,
            CapabilityDomain.MEMORY,
            CapabilityDomain.RELIABILITY_AND_RECOVERY,
        }
        instruction = await store.read_artifact(contract.instruction_ref)
        assert LocomoImportSpec.model_validate_json(instruction).required_q_types == (
            "1",
            "2",
            "3",
            "4",
            "5",
        )
    finally:
        await store.close()
