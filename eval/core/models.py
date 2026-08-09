"""Versioned, framework-neutral evaluation evidence contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Identifier = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._/-]*$"),
]
Version = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$",
    ),
]


class EvidenceModel(BaseModel):
    """Base class for immutable persisted evidence."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class CapabilityDomain(StrEnum):
    CODING_OUTCOME = "coding_outcome"
    AGENT_LOOP = "agent_loop"
    CONTEXT_MANAGEMENT = "context_management"
    MEMORY = "memory"
    TOOLS_AND_PROTOCOLS = "tools_and_protocols"
    SAFETY_AND_PRIVACY = "safety_and_privacy"
    RELIABILITY_AND_RECOVERY = "reliability_and_recovery"
    HUMAN_INTERACTION = "human_interaction"
    OPERATIONAL_FITNESS = "operational_fitness"


class EvalTier(StrEnum):
    L0_LOCAL = "l0_local"
    L1_PR = "l1_pr"
    L2_NIGHTLY = "l2_nightly"
    L3_WEEKLY = "l3_weekly"
    L4_RELEASE = "l4_release"


class ResultStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INVALID = "invalid"


class AggregateStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class RunState(StrEnum):
    PREPARED = "prepared"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TrialState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


class AttemptState(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


class StateProfile(StrEnum):
    CLEAN_CODING = "clean_coding"
    WARM_CODING = "warm_coding"
    CONTEXT = "context"
    MEMORY = "memory"
    RECOVERY = "recovery"
    SECURITY = "security"


class RiskLevel(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class GraderType(StrEnum):
    DETERMINISTIC = "deterministic"
    LLM_JUDGE = "llm_judge"
    HUMAN = "human"


class IsolationType(StrEnum):
    CONTAINER = "container"
    VIRTUAL_MACHINE = "virtual_machine"
    PROCESS = "process"


class NetworkMode(StrEnum):
    DISABLED = "disabled"
    ALLOWLIST = "allowlist"
    UNRESTRICTED = "unrestricted"


class DatasetSplit(StrEnum):
    DEVELOPMENT = "development"
    REGRESSION = "regression"
    HOLDOUT = "holdout"
    PUBLIC = "public"


class BudgetEnforcement(StrEnum):
    ENFORCED = "enforced"
    OBSERVE = "observe"


class ArtifactRef(EvidenceModel):
    schema_version: Literal["eval.artifact.v1"] = "eval.artifact.v1"
    digest: Digest
    media_type: Annotated[str, Field(min_length=1, max_length=255)]
    size_bytes: Annotated[int, Field(ge=0)]


class ResourceBudget(EvidenceModel):
    wall_time_seconds: Annotated[int, Field(gt=0)]
    max_steps: Annotated[int, Field(gt=0)]
    max_model_calls: Annotated[int, Field(gt=0)]
    max_tool_calls: Annotated[int, Field(gt=0)]
    max_input_tokens: Annotated[int, Field(gt=0)]
    max_output_tokens: Annotated[int, Field(gt=0)]
    max_cost_microusd: Annotated[int, Field(ge=0)]
    enforcement: BudgetEnforcement = BudgetEnforcement.ENFORCED
    emergency_watchdog_seconds: Annotated[int, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def validate_enforcement(self) -> ResourceBudget:
        if (
            self.enforcement is BudgetEnforcement.OBSERVE
            and self.emergency_watchdog_seconds is None
        ):
            raise ValueError("observational budgets require an emergency watchdog")
        if (
            self.enforcement is BudgetEnforcement.ENFORCED
            and self.emergency_watchdog_seconds is not None
        ):
            raise ValueError("enforced budgets cannot define an emergency watchdog")
        return self

    @property
    def execution_timeout_seconds(self) -> int:
        if self.enforcement is BudgetEnforcement.OBSERVE:
            assert self.emergency_watchdog_seconds is not None
            return self.emergency_watchdog_seconds
        return self.wall_time_seconds


class GraderContract(EvidenceModel):
    grader_id: Identifier
    grader_type: GraderType
    implementation: Annotated[str, Field(min_length=1, max_length=255)]
    version: Version
    domains: Annotated[tuple[CapabilityDomain, ...], Field(min_length=1)]
    success_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    required: bool = True
    veto: bool = False
    rubric_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def validate_grader(self) -> GraderContract:
        if len(set(self.domains)) != len(self.domains):
            raise ValueError("grader domains must be unique")
        if self.grader_type is not GraderType.DETERMINISTIC and self.rubric_ref is None:
            raise ValueError("non-deterministic graders require a rubric_ref")
        return self


class TaskContract(EvidenceModel):
    schema_version: Literal["eval.task.v1"] = "eval.task.v1"
    task_id: Identifier
    task_version: Version
    suite_id: Identifier
    suite_version: Version
    title: Annotated[str, Field(min_length=1, max_length=255)]
    risk: RiskLevel
    state_profile: StateProfile
    domains: Annotated[tuple[CapabilityDomain, ...], Field(min_length=1)]
    instruction_ref: ArtifactRef
    initial_state_ref: ArtifactRef
    budget: ResourceBudget
    graders: Annotated[tuple[GraderContract, ...], Field(min_length=1)]
    tags: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def validate_contract(self) -> TaskContract:
        if len(set(self.domains)) != len(self.domains):
            raise ValueError("task domains must be unique")
        grader_ids = [grader.grader_id for grader in self.graders]
        if len(set(grader_ids)) != len(grader_ids):
            raise ValueError("grader_id values must be unique within a task")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("task tags must be unique")
        grader_domains = {domain for grader in self.graders for domain in grader.domains}
        if not grader_domains.issubset(set(self.domains)):
            raise ValueError("grader domains must be declared by the task")
        return self


class ModelConfiguration(EvidenceModel):
    provider: Identifier
    requested_model: Annotated[str, Field(min_length=1, max_length=255)]
    resolved_model: Annotated[str, Field(min_length=1, max_length=255)]
    api_protocol: Identifier
    parameters_digest: Digest


class SubjectUnderTest(EvidenceModel):
    subject_id: Identifier
    product_version: Annotated[str, Field(min_length=1, max_length=128)]
    source_commit: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
    source_dirty: bool = False
    source_patch_ref: ArtifactRef | None = None
    executable_digest: Digest
    harness_profile_digest: Digest
    model: ModelConfiguration

    @model_validator(mode="after")
    def validate_source_state(self) -> SubjectUnderTest:
        if self.source_dirty and self.source_patch_ref is None:
            raise ValueError("dirty source requires source_patch_ref")
        if not self.source_dirty and self.source_patch_ref is not None:
            raise ValueError("clean source cannot include source_patch_ref")
        return self


class JudgeConfiguration(EvidenceModel):
    judge_id: Identifier
    model: ModelConfiguration
    rubric_bundle_digest: Digest
    calibration_set_digest: Digest


class EnvironmentSpec(EvidenceModel):
    environment_id: Identifier
    isolation: IsolationType
    os_name: Identifier
    os_version: Annotated[str, Field(min_length=1, max_length=128)]
    architecture: Identifier
    image_digest: Digest
    dependencies_digest: Digest
    network_mode: NetworkMode
    locale: Annotated[str, Field(min_length=1, max_length=64)]
    timezone: Annotated[str, Field(min_length=1, max_length=64)]


class EvalRunManifest(EvidenceModel):
    schema_version: Literal["eval.run.v1"] = "eval.run.v1"
    run_id: Identifier
    created_at: datetime
    tier: EvalTier
    split: DatasetSplit
    comparison_group_id: Identifier | None = None
    subject: SubjectUnderTest
    judge: JudgeConfiguration | None = None
    task_contract_digests: Annotated[tuple[Digest, ...], Field(min_length=1)]
    environment: EnvironmentSpec
    default_budget: ResourceBudget
    random_seed: Annotated[int, Field(ge=0)]
    repetitions: Annotated[int, Field(gt=0)]
    orchestration_version: Version
    evidence_store_uri: Annotated[str, Field(min_length=1, max_length=2048)]

    @field_validator("created_at")
    @classmethod
    def require_aware_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_manifest(self) -> EvalRunManifest:
        if len(set(self.task_contract_digests)) != len(self.task_contract_digests):
            raise ValueError("task contract digests must be unique")
        if self.tier is EvalTier.L4_RELEASE and self.split is DatasetSplit.DEVELOPMENT:
            raise ValueError("L4 release runs cannot use the development split")
        return self


class AdapterIdentity(EvidenceModel):
    adapter_id: Identifier
    adapter_version: Version


class MetricValue(EvidenceModel):
    name: Identifier
    value: float
    unit: Annotated[str, Field(min_length=1, max_length=64)] | None = None


class ResourceUsage(EvidenceModel):
    wall_time_ms: Annotated[int, Field(ge=0)]
    steps: Annotated[int, Field(ge=0)]
    model_calls: Annotated[int, Field(ge=0)]
    tool_calls: Annotated[int, Field(ge=0)]
    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]
    cost_microusd: Annotated[int, Field(ge=0)] | None = None


class GraderResult(EvidenceModel):
    grader_id: Identifier
    status: ResultStatus
    score: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    message: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    details_ref: ArtifactRef | None = None


class FailureRecord(EvidenceModel):
    category: Identifier
    message: Annotated[str, Field(min_length=1, max_length=4000)]
    evidence_ref: ArtifactRef | None = None


class TrialResult(EvidenceModel):
    schema_version: Literal["eval.trial.v1"] = "eval.trial.v1"
    trial_id: Identifier
    run_id: Identifier
    run_manifest_digest: Digest
    task_id: Identifier
    task_contract_digest: Digest
    attempt: Annotated[int, Field(gt=0)]
    adapter: AdapterIdentity
    status: ResultStatus
    started_at: datetime
    finished_at: datetime
    usage: ResourceUsage
    grader_results: tuple[GraderResult, ...] = ()
    metrics: tuple[MetricValue, ...] = ()
    outcome_ref: ArtifactRef | None = None
    trajectory_ref: ArtifactRef | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    failure: FailureRecord | None = None
    invalid_reason: Annotated[str, Field(min_length=1, max_length=4000)] | None = None

    @field_validator("started_at", "finished_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("trial timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_result(self) -> TrialResult:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        grader_ids = [result.grader_id for result in self.grader_results]
        if len(set(grader_ids)) != len(grader_ids):
            raise ValueError("grader results must have unique grader_id values")
        metric_names = [metric.name for metric in self.metrics]
        if len(set(metric_names)) != len(metric_names):
            raise ValueError("metric names must be unique")
        artifact_digests = [artifact.digest for artifact in self.artifacts]
        if len(set(artifact_digests)) != len(artifact_digests):
            raise ValueError("artifact references must be unique")

        if self.status is ResultStatus.INVALID:
            if self.invalid_reason is None:
                raise ValueError("invalid trials require invalid_reason")
            if self.failure is not None:
                raise ValueError("invalid trials use invalid_reason, not failure")
            return self

        if self.invalid_reason is not None:
            raise ValueError("pass/fail trials cannot include invalid_reason")
        if self.outcome_ref is None:
            raise ValueError("pass/fail trials require outcome_ref")
        if not self.grader_results:
            raise ValueError("pass/fail trials require grader results")

        statuses = {result.status for result in self.grader_results}
        if ResultStatus.INVALID in statuses:
            raise ValueError("pass/fail trials cannot contain invalid grader results")
        if self.status is ResultStatus.PASS:
            if statuses != {ResultStatus.PASS}:
                raise ValueError("passed trials can contain only passed grader results")
            if self.failure is not None:
                raise ValueError("passed trials cannot include failure")
        elif ResultStatus.FAIL not in statuses:
            raise ValueError("failed trials require at least one failed grader result")
        elif self.failure is None:
            raise ValueError("failed trials require failure details")
        return self


class TrialRequest(EvidenceModel):
    schema_version: Literal["eval.trial-request.v1"] = "eval.trial-request.v1"
    trial_id: Identifier
    run_id: Identifier
    run_manifest_digest: Digest
    task: TaskContract
    adapter: AdapterIdentity
    seed: Annotated[int, Field(ge=0)]


class AttemptLease(EvidenceModel):
    schema_version: Literal["eval.attempt-lease.v1"] = "eval.attempt-lease.v1"
    attempt_id: Identifier
    trial_id: Identifier
    attempt: Annotated[int, Field(gt=0)]
    worker_id: Identifier
    request: TrialRequest
    claimed_at: datetime
    heartbeat_at: datetime

    @field_validator("claimed_at", "heartbeat_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("attempt timestamps must be timezone-aware")
        return value.astimezone(UTC)
