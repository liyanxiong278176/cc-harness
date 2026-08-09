"""Task Contract installer for SWE-bench Verified evidence."""

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

from .models import SwebenchImportSpec


async def install_swebench_contract(
    store: EvalStore,
    initial_state_ref: ArtifactRef,
    spec: SwebenchImportSpec,
    *,
    budget: ResourceBudget,
) -> TaskContract:
    instruction_ref = await store.put_artifact(
        canonical_json_bytes(spec), "application/vnd.cc-harness.swebench-import+json"
    )
    domains = (
        CapabilityDomain.CODING_OUTCOME,
        CapabilityDomain.AGENT_LOOP,
        CapabilityDomain.TOOLS_AND_PROTOCOLS,
        CapabilityDomain.RELIABILITY_AND_RECOVERY,
    )
    return TaskContract(
        task_id=spec.contract_id,
        task_version="1.0.0",
        suite_id="swebench-verified",
        suite_version="1.0.0",
        title=f"SWE-bench Verified {spec.instance_id}",
        risk=RiskLevel.HIGH,
        state_profile=StateProfile.CLEAN_CODING,
        domains=domains,
        instruction_ref=instruction_ref,
        initial_state_ref=initial_state_ref,
        budget=budget,
        graders=(
            GraderContract(
                grader_id="swebench-resolved",
                grader_type=GraderType.DETERMINISTIC,
                implementation="eval.swebench.adapter:SwebenchEvidenceAdapter",
                version="1.0.0",
                domains=domains,
                success_threshold=1.0,
                veto=True,
            ),
        ),
        tags=("coding", "imported", "swebench", "verified"),
    )
