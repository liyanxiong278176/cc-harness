"""Frozen task matrix for four controlled Claude Code comparison suites."""

from __future__ import annotations

from eval.core import CapabilityDomain, StateProfile

from .models import (
    ContextProfile,
    FaultOutcome,
    FaultRule,
    FixtureKind,
    MemoryPhase,
    SpecialistCatalog,
    SpecialistSuite,
    SpecialistTaskDefinition,
)

_LOCOMO_SAMPLE_IDS = (
    "conv-26",
    "conv-30",
    "conv-41",
    "conv-42",
    "conv-43",
    "conv-44",
    "conv-47",
    "conv-48",
    "conv-49",
    "conv-50",
)

_LOOP_METRICS = (
    "task-success",
    "recovery-rate",
    "repeated-call-rate",
    "stall-rate",
    "correct-stop-rate",
    "duplicate-effect-rate",
)
_CONTEXT_METRICS = (
    "task-success",
    "fact-retention",
    "source-accuracy",
    "post-compaction-success",
    "resume-integrity",
    "token-saving",
)
_MEMORY_METRICS = (
    "recall",
    "timeliness",
    "utilization",
    "consistency",
    "source-accuracy",
    "contamination-rate",
)
_TOOLS_METRICS = (
    "task-success",
    "tool-selection",
    "argument-accuracy",
    "edit-completeness",
    "continuation-rate",
    "boundary-rate",
    "recovery-rate",
)


def _loop_tasks() -> list[SpecialistTaskDefinition]:
    scenarios = {
        "transient-tool-failure": FaultRule(
            operation="flaky-read",
            fail_first=2,
            outcome=FaultOutcome.ERROR,
            payload="transient fixture failure",
        ),
        "permanent-tool-failure": FaultRule(
            operation="permanent-failure",
            always_fail=True,
            outcome=FaultOutcome.ERROR,
            payload="permanent fixture failure; use the documented alternate path",
        ),
        "failed-test-recovery": FaultRule(
            operation="test-probe",
            fail_first=1,
            outcome=FaultOutcome.ERROR,
            payload="the first plausible patch passes only the visible probe",
        ),
        "wrong-hypothesis-recovery": FaultRule(
            operation="misleading-read",
            fail_first=1,
            outcome=FaultOutcome.MALFORMED,
            payload="stale metadata; verify against the authoritative source",
        ),
        "no-progress-stop": FaultRule(
            operation="no-progress",
            always_fail=True,
            outcome=FaultOutcome.ERROR,
            payload="this operation never produces new evidence",
        ),
        "checkpoint-session-resume": FaultRule(
            operation="checkpoint-side-effect",
            fail_first=1,
            outcome=FaultOutcome.TIMEOUT,
            delay_ms=2_000,
            payload="resume with the same idempotency key",
        ),
    }
    tasks: list[SpecialistTaskDefinition] = []
    for scenario, rule in scenarios.items():
        for variant in range(1, 5):
            fixtures = [FixtureKind.WORKSPACE, FixtureKind.STATEFUL_PROCESS]
            if scenario in {
                "transient-tool-failure",
                "permanent-tool-failure",
                "no-progress-stop",
                "interrupt-resume",
            }:
                fixtures.append(FixtureKind.STDIO_MCP)
            tasks.append(
                SpecialistTaskDefinition(
                    task_id=f"specialist.agent-loop.{scenario}.v{variant}",
                    suite=SpecialistSuite.AGENT_LOOP,
                    scenario=scenario,
                    variant=variant,
                    primary_domain=CapabilityDomain.AGENT_LOOP,
                    state_profile=(
                        StateProfile.RECOVERY
                        if scenario == "checkpoint-session-resume"
                        else StateProfile.CLEAN_CODING
                    ),
                    fixtures=tuple(fixtures),
                    primary_metrics=_LOOP_METRICS,
                    fault_plan=(rule,),
                    tags=("controlled", "paired", "deterministic-fault"),
                )
            )
    return tasks


def _context_tasks() -> list[SpecialistTaskDefinition]:
    tasks: list[SpecialistTaskDefinition] = []
    scenarios = ("conflicting-sources", "distributed-constraints", "compact-offload-resume")
    for pressure in (0.50, 0.75, 0.90):
        for position in (0.20, 0.50, 0.80):
            for variant, scenario in enumerate(scenarios, start=1):
                tasks.append(
                    SpecialistTaskDefinition(
                        task_id=(
                            f"specialist.context.{scenario}."
                            f"p{round(pressure * 100)}-n{round(position * 100)}"
                        ),
                        suite=SpecialistSuite.CONTEXT,
                        scenario=scenario,
                        variant=variant,
                        primary_domain=CapabilityDomain.CONTEXT_MANAGEMENT,
                        state_profile=StateProfile.CONTEXT,
                        fixtures=(FixtureKind.WORKSPACE, FixtureKind.PERSISTENT_SESSIONS),
                        primary_metrics=_CONTEXT_METRICS,
                        context_profile=ContextProfile(
                            pressure_ratio=pressure,
                            fact_position_ratio=position,
                            required_fact_count=5 if variant < 3 else 8,
                            conflicting_source_count=4 if scenario == "conflicting-sources" else 0,
                            require_compaction=scenario == "compact-offload-resume",
                            require_offload=scenario == "compact-offload-resume",
                            require_resume=scenario == "compact-offload-resume",
                        ),
                        tags=("controlled", "paired", "deterministic-grader"),
                    )
                )
    return tasks


def _memory_tasks() -> list[SpecialistTaskDefinition]:
    tasks: list[SpecialistTaskDefinition] = []
    protocols = {
        "cross-session-recall": (MemoryPhase.ACQUIRE, MemoryPhase.QUERY),
        "conflict-update": (MemoryPhase.ACQUIRE, MemoryPhase.UPDATE, MemoryPhase.QUERY),
        "forget": (MemoryPhase.ACQUIRE, MemoryPhase.FORGET, MemoryPhase.QUERY),
        "project-isolation": (MemoryPhase.ACQUIRE, MemoryPhase.ISOLATE, MemoryPhase.QUERY),
        "source-attribution": (MemoryPhase.ACQUIRE, MemoryPhase.QUERY),
        "persistence-confirmation": (MemoryPhase.ACQUIRE, MemoryPhase.QUERY),
    }
    for scenario, phases in protocols.items():
        for variant in range(1, 5):
            tasks.append(
                SpecialistTaskDefinition(
                    task_id=f"specialist.memory.{scenario}.v{variant}",
                    suite=SpecialistSuite.MEMORY,
                    scenario=scenario,
                    variant=variant,
                    primary_domain=CapabilityDomain.MEMORY,
                    state_profile=StateProfile.MEMORY,
                    fixtures=(FixtureKind.WORKSPACE, FixtureKind.PERSISTENT_SESSIONS),
                    primary_metrics=_MEMORY_METRICS,
                    memory_phases=phases,
                    tags=("controlled", "paired", "cross-session"),
                )
            )
    for index, sample_id in enumerate(_LOCOMO_SAMPLE_IDS, start=1):
        tasks.append(
            SpecialistTaskDefinition(
                task_id=f"specialist.memory.locomo.{sample_id}",
                suite=SpecialistSuite.MEMORY,
                scenario="locomo",
                variant=index,
                primary_domain=CapabilityDomain.MEMORY,
                state_profile=StateProfile.MEMORY,
                fixtures=(FixtureKind.LOCOMO, FixtureKind.PERSISTENT_SESSIONS),
                primary_metrics=_MEMORY_METRICS,
                memory_phases=(MemoryPhase.ACQUIRE, MemoryPhase.QUERY),
                dataset_sample_id=sample_id,
                tags=("public-dataset", "paired", "cross-session"),
            )
        )
    return tasks


def _tools_tasks() -> list[SpecialistTaskDefinition]:
    capabilities = {
        "read": ("read",),
        "glob": ("glob",),
        "grep": ("grep",),
        "edit": ("read", "edit"),
        "write": ("write",),
        "shell": ("shell",),
        "composed": ("glob", "grep", "read", "edit", "shell"),
        "mcp": ("mcp",),
    }
    tasks: list[SpecialistTaskDefinition] = []
    for scenario, required in capabilities.items():
        for variant in range(1, 5):
            fixtures = [FixtureKind.WORKSPACE]
            if scenario == "mcp":
                fixtures.extend((FixtureKind.STATEFUL_PROCESS, FixtureKind.STDIO_MCP))
            tasks.append(
                SpecialistTaskDefinition(
                    task_id=f"specialist.tools-mcp.{scenario}.v{variant}",
                    suite=SpecialistSuite.TOOLS_MCP,
                    scenario=scenario,
                    variant=variant,
                    primary_domain=CapabilityDomain.TOOLS_AND_PROTOCOLS,
                    state_profile=StateProfile.CLEAN_CODING,
                    fixtures=tuple(fixtures),
                    primary_metrics=_TOOLS_METRICS,
                    required_capabilities=required,
                    tags=("controlled", "paired", "deterministic-grader"),
                )
            )
    return tasks


def build_specialist_catalog() -> SpecialistCatalog:
    tasks = [*_loop_tasks(), *_context_tasks(), *_memory_tasks(), *_tools_tasks()]
    return SpecialistCatalog(tasks=tuple(sorted(tasks, key=lambda task: task.task_id)))


SPECIALIST_CATALOG = build_specialist_catalog()
