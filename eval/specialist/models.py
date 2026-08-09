"""Versioned contracts for controlled agent-loop, context, memory and tool evals."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from eval.core import CapabilityDomain, StateProfile
from eval.core.models import EvidenceModel, Identifier, Version


class SpecialistSuite(StrEnum):
    AGENT_LOOP = "agent-loop"
    CONTEXT = "context"
    MEMORY = "memory"
    TOOLS_MCP = "tools-mcp"


class FixtureKind(StrEnum):
    WORKSPACE = "workspace"
    STATEFUL_PROCESS = "stateful-process"
    STDIO_MCP = "stdio-mcp"
    PERSISTENT_SESSIONS = "persistent-sessions"
    LOCOMO = "locomo"


class FaultOutcome(StrEnum):
    ERROR = "error"
    TIMEOUT = "timeout"
    MALFORMED = "malformed"
    SUCCESS = "success"


class MemoryPhase(StrEnum):
    ACQUIRE = "acquire"
    QUERY = "query"
    UPDATE = "update"
    FORGET = "forget"
    ISOLATE = "isolate"


class SemanticEventKind(StrEnum):
    TOOL_CALL = "tool-call"
    TOOL_RESULT = "tool-result"
    FINAL = "final"
    ERROR = "error"


class SemanticOutcome(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class FaultRule(EvidenceModel):
    schema_version: Literal["eval.specialist-fault-rule.v1"] = "eval.specialist-fault-rule.v1"
    operation: Identifier
    fail_first: Annotated[int, Field(ge=0)] = 0
    always_fail: bool = False
    outcome: FaultOutcome
    delay_ms: Annotated[int, Field(ge=0, le=60_000)] = 0
    payload: Annotated[str, Field(max_length=2_000)] = ""

    @model_validator(mode="after")
    def validate_rule(self) -> FaultRule:
        if self.always_fail and self.fail_first:
            raise ValueError("always-fail rules cannot also declare fail_first")
        if self.outcome is FaultOutcome.TIMEOUT and self.delay_ms == 0:
            raise ValueError("timeout rules require a positive delay")
        return self


class ContextProfile(EvidenceModel):
    schema_version: Literal["eval.specialist-context-profile.v1"] = (
        "eval.specialist-context-profile.v1"
    )
    pressure_ratio: Annotated[float, Field(ge=0.1, le=0.95)]
    fact_position_ratio: Annotated[float, Field(ge=0.05, le=0.95)]
    required_fact_count: Annotated[int, Field(gt=0, le=100)]
    conflicting_source_count: Annotated[int, Field(ge=0, le=100)] = 0
    require_compaction: bool = False
    require_offload: bool = False
    require_resume: bool = False


class SpecialistTaskDefinition(EvidenceModel):
    schema_version: Literal["eval.specialist-task-definition.v1"] = (
        "eval.specialist-task-definition.v1"
    )
    task_id: Identifier
    task_version: Version = "5.0.0"
    suite: SpecialistSuite
    scenario: Identifier
    variant: Annotated[int, Field(gt=0)]
    primary_domain: CapabilityDomain
    state_profile: StateProfile
    fixtures: Annotated[tuple[FixtureKind, ...], Field(min_length=1)]
    primary_metrics: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    repetitions: Annotated[int, Field(gt=0)] = 1
    fault_plan: tuple[FaultRule, ...] = ()
    context_profile: ContextProfile | None = None
    memory_phases: tuple[MemoryPhase, ...] = ()
    required_capabilities: tuple[Identifier, ...] = ()
    dataset_sample_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    tags: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def validate_suite_contract(self) -> SpecialistTaskDefinition:
        if len(set(self.fixtures)) != len(self.fixtures):
            raise ValueError("fixture kinds must be unique")
        if len(set(self.primary_metrics)) != len(self.primary_metrics):
            raise ValueError("primary metrics must be unique")
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise ValueError("required capabilities must be unique")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("tags must be unique")

        expected_domain = {
            SpecialistSuite.AGENT_LOOP: CapabilityDomain.AGENT_LOOP,
            SpecialistSuite.CONTEXT: CapabilityDomain.CONTEXT_MANAGEMENT,
            SpecialistSuite.MEMORY: CapabilityDomain.MEMORY,
            SpecialistSuite.TOOLS_MCP: CapabilityDomain.TOOLS_AND_PROTOCOLS,
        }[self.suite]
        if self.primary_domain is not expected_domain:
            raise ValueError("specialist suite declares the wrong primary domain")

        if self.suite is SpecialistSuite.AGENT_LOOP and not self.fault_plan:
            raise ValueError("agent-loop tasks require a deterministic fault plan")
        if self.suite is SpecialistSuite.CONTEXT and self.context_profile is None:
            raise ValueError("context tasks require a context profile")
        if self.suite is not SpecialistSuite.CONTEXT and self.context_profile is not None:
            raise ValueError("only context tasks may declare a context profile")
        if self.suite is SpecialistSuite.MEMORY and not self.memory_phases:
            raise ValueError("memory tasks require a multi-phase protocol")
        if self.suite is not SpecialistSuite.MEMORY and self.memory_phases:
            raise ValueError("only memory tasks may declare memory phases")
        if self.dataset_sample_id is not None and FixtureKind.LOCOMO not in self.fixtures:
            raise ValueError("dataset samples require the LoCoMo fixture")
        if self.suite is SpecialistSuite.TOOLS_MCP and not self.required_capabilities:
            raise ValueError("tools/MCP tasks require semantic capabilities")
        return self


class SpecialistCatalog(EvidenceModel):
    schema_version: Literal["eval.specialist-catalog.v1"] = "eval.specialist-catalog.v1"
    catalog_id: Identifier = "claude-code-specialist"
    catalog_version: Version = "5.0.0"
    tasks: Annotated[tuple[SpecialistTaskDefinition, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_catalog(self) -> SpecialistCatalog:
        identities = [task.task_id for task in self.tasks]
        if len(identities) != len(set(identities)):
            raise ValueError("specialist task ids must be unique")
        if {task.suite for task in self.tasks} != set(SpecialistSuite):
            raise ValueError("specialist catalog must cover every specialist suite")
        return self


class SemanticEvent(EvidenceModel):
    schema_version: Literal["eval.semantic-event.v1"] = "eval.semantic-event.v1"
    sequence: Annotated[int, Field(gt=0)]
    kind: SemanticEventKind
    capability: Identifier | None = None
    native_name: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    call_id: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    argument_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")] | None = None
    outcome: SemanticOutcome = SemanticOutcome.UNKNOWN
    duration_ms: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def validate_event(self) -> SemanticEvent:
        if self.kind is SemanticEventKind.TOOL_CALL and (
            self.capability is None or self.native_name is None or self.argument_digest is None
        ):
            raise ValueError("tool-call events require capability, name and argument digest")
        if self.kind is SemanticEventKind.TOOL_RESULT and self.outcome is SemanticOutcome.UNKNOWN:
            raise ValueError("tool-result events require a known outcome")
        return self


class SemanticTrajectory(EvidenceModel):
    schema_version: Literal["eval.semantic-trajectory.v1"] = "eval.semantic-trajectory.v1"
    harness: Identifier
    events: tuple[SemanticEvent, ...]

    @model_validator(mode="after")
    def validate_sequence(self) -> SemanticTrajectory:
        expected = list(range(1, len(self.events) + 1))
        if [event.sequence for event in self.events] != expected:
            raise ValueError("semantic trajectory event sequences must be contiguous")
        return self
