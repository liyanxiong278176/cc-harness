"""Frozen LoCoMo contract for the unified capability report."""

from __future__ import annotations

from eval.core import (
    ArtifactRef,
    CapabilityDomain,
    EvalStore,
    GraderContract,
    GraderType,
    ResourceBudget,
    RiskLevel,
    StateProfile,
    TaskContract,
    canonical_json_bytes,
)

from .models import LocomoImportSpec


async def install_locomo_memory_contract(
    store: EvalStore,
    initial_state_ref: ArtifactRef,
    *,
    results_path: str = "evidence/locomo-results.json",
    metrics_path: str = "evidence/locomo-metrics.json",
    report_path: str = "evidence/locomo-report.html",
    trajectory_paths: tuple[str, ...] = (),
    minimum_qa_count: int = 100,
    success_threshold: float = 0.6,
) -> TaskContract:
    spec = LocomoImportSpec(
        contract_id="locomo.memory",
        results_path=results_path,
        metrics_path=metrics_path,
        report_path=report_path,
        trajectory_paths=trajectory_paths,
        minimum_qa_count=minimum_qa_count,
        required_q_types=("1", "2", "3", "4", "5"),
    )
    instruction_ref = await store.put_artifact(
        canonical_json_bytes(spec),
        "application/vnd.cc-harness.locomo-import+json",
    )
    domains = (
        CapabilityDomain.MEMORY,
        CapabilityDomain.CONTEXT_MANAGEMENT,
        CapabilityDomain.RELIABILITY_AND_RECOVERY,
    )
    return TaskContract(
        task_id=spec.contract_id,
        task_version="1.0.0",
        suite_id="locomo-memory",
        suite_version="1.0.0",
        title="LoCoMo long-conversation memory, context and consistency",
        risk=RiskLevel.HIGH,
        state_profile=StateProfile.MEMORY,
        domains=domains,
        instruction_ref=instruction_ref,
        initial_state_ref=initial_state_ref,
        budget=ResourceBudget(
            wall_time_seconds=120,
            max_steps=10_000,
            max_model_calls=10_000,
            max_tool_calls=50_000,
            max_input_tokens=500_000_000,
            max_output_tokens=50_000_000,
            max_cost_microusd=2_000_000_000,
        ),
        graders=(
            GraderContract(
                grader_id="locomo-pass-rate",
                grader_type=GraderType.DETERMINISTIC,
                implementation="eval.locomo.adapter:LocomoEvidenceAdapter",
                version="1.0.0",
                domains=domains,
                success_threshold=success_threshold,
                veto=True,
            ),
        ),
        tags=("context", "imported", "locomo", "memory"),
    )
