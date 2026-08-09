"""Frozen internal regression definitions covering the nine harness domains."""

from __future__ import annotations

from dataclasses import dataclass

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

from .models import NativePytestSpec


@dataclass(frozen=True)
class NativeRegressionDefinition:
    task_id: str
    title: str
    domain: CapabilityDomain
    risk: RiskLevel
    state_profile: StateProfile
    test_targets: tuple[str, ...]
    veto: bool
    wall_time_seconds: int


NATIVE_REGRESSION_DEFINITIONS = (
    NativeRegressionDefinition(
        task_id="native.coding-outcome",
        title="First-party coding mutations preserve content integrity",
        domain=CapabilityDomain.CODING_OUTCOME,
        risk=RiskLevel.CRITICAL,
        state_profile=StateProfile.CLEAN_CODING,
        test_targets=("tests/test_native_tools.py", "tests/test_completion_gate.py"),
        veto=True,
        wall_time_seconds=180,
    ),
    NativeRegressionDefinition(
        task_id="native.agent-loop",
        title="Agent loop terminates, gates completion and preserves execution state",
        domain=CapabilityDomain.AGENT_LOOP,
        risk=RiskLevel.CRITICAL,
        state_profile=StateProfile.CLEAN_CODING,
        test_targets=(
            "tests/test_agent.py",
            "tests/test_b_integration.py",
            "tests/test_c_integration.py",
        ),
        veto=True,
        wall_time_seconds=240,
    ),
    NativeRegressionDefinition(
        task_id="native.context-management",
        title="Context accounting and compaction preserve required state",
        domain=CapabilityDomain.CONTEXT_MANAGEMENT,
        risk=RiskLevel.HIGH,
        state_profile=StateProfile.CONTEXT,
        test_targets=("tests/test_context.py", "tests/test_tokens.py"),
        veto=True,
        wall_time_seconds=180,
    ),
    NativeRegressionDefinition(
        task_id="native.memory",
        title="Layered memory recall and checkpoints remain attributable",
        domain=CapabilityDomain.MEMORY,
        risk=RiskLevel.HIGH,
        state_profile=StateProfile.MEMORY,
        test_targets=(
            "tests/test_memory_layered.py",
            "tests/test_memory_recall_retry.py",
            "tests/test_memory_checkpoint.py",
        ),
        veto=True,
        wall_time_seconds=240,
    ),
    NativeRegressionDefinition(
        task_id="native.tools-protocols",
        title="Native and MCP tool contracts validate arguments and outcomes",
        domain=CapabilityDomain.TOOLS_AND_PROTOCOLS,
        risk=RiskLevel.CRITICAL,
        state_profile=StateProfile.CLEAN_CODING,
        test_targets=(
            "tests/test_schema.py",
            "tests/test_tools.py",
            "tests/test_mcp_client.py",
        ),
        veto=True,
        wall_time_seconds=240,
    ),
    NativeRegressionDefinition(
        task_id="native.safety-privacy",
        title="Policy, sandbox and output controls fail closed",
        domain=CapabilityDomain.SAFETY_AND_PRIVACY,
        risk=RiskLevel.CRITICAL,
        state_profile=StateProfile.SECURITY,
        test_targets=(
            "tests/test_policy.py",
            "tests/test_l2.py",
            "tests/test_l5.py",
            "tests/test_sandbox.py",
        ),
        veto=True,
        wall_time_seconds=240,
    ),
    NativeRegressionDefinition(
        task_id="native.reliability-recovery",
        title="Immutable facts, sessions and resume survive interruption",
        domain=CapabilityDomain.RELIABILITY_AND_RECOVERY,
        risk=RiskLevel.CRITICAL,
        state_profile=StateProfile.RECOVERY,
        test_targets=(
            "tests/test_fact_store.py",
            "tests/test_session_store.py",
            "tests/test_cli_resume.py",
        ),
        veto=True,
        wall_time_seconds=240,
    ),
    NativeRegressionDefinition(
        task_id="native.human-interaction",
        title="Terminal input, scrolling and transcript projections remain usable",
        domain=CapabilityDomain.HUMAN_INTERACTION,
        risk=RiskLevel.CRITICAL,
        state_profile=StateProfile.CONTEXT,
        test_targets=(
            "tests/test_terminal_fullscreen.py",
            "tests/test_terminal_classic_ui.py",
            "tests/test_terminal_renderer.py",
            "tests/test_terminal_transcript.py",
        ),
        veto=True,
        wall_time_seconds=240,
    ),
    NativeRegressionDefinition(
        task_id="native.operational-fitness",
        title="Entrypoint, configuration and audit surfaces remain operable",
        domain=CapabilityDomain.OPERATIONAL_FITNESS,
        risk=RiskLevel.HIGH,
        state_profile=StateProfile.CLEAN_CODING,
        test_targets=(
            "tests/test_entrypoint.py",
            "tests/test_config.py",
            "tests/test_audit.py",
        ),
        veto=False,
        wall_time_seconds=180,
    ),
)


async def install_native_regression_contracts(
    store: EvalStore,
    initial_state_ref: ArtifactRef,
) -> tuple[TaskContract, ...]:
    """Persist instruction specs and return the frozen internal regression contracts."""

    contracts: list[TaskContract] = []
    for definition in NATIVE_REGRESSION_DEFINITIONS:
        spec = NativePytestSpec(
            contract_id=definition.task_id,
            test_targets=definition.test_targets,
            environment=("CI", "NO_COLOR", "PYTHONHASHSEED", "PYTHONIOENCODING"),
        )
        instruction_ref = await store.put_artifact(
            canonical_json_bytes(spec),
            "application/vnd.cc-harness.native-pytest+json",
        )
        contracts.append(
            TaskContract(
                task_id=definition.task_id,
                task_version="1.0.0",
                suite_id="native-regression",
                suite_version="1.0.0",
                title=definition.title,
                risk=definition.risk,
                state_profile=definition.state_profile,
                domains=(definition.domain,),
                instruction_ref=instruction_ref,
                initial_state_ref=initial_state_ref,
                budget=ResourceBudget(
                    wall_time_seconds=definition.wall_time_seconds,
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
                        domains=(definition.domain,),
                        veto=definition.veto,
                    ),
                ),
                tags=("internal", "regression", "deterministic"),
            )
        )
    return tuple(contracts)
