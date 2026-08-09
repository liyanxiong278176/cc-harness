"""Single catalog for the nine-domain Claude Code parity matrix."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from eval.core import (
    CapabilityDomain,
    DatasetSplit,
    EvalTier,
    ParityConclusion,
    ParityDecision,
)
from eval.core.models import EvidenceModel, Identifier


class ParitySuite(StrEnum):
    SMOKE = "smoke"
    DEV = "dev"
    REGRESSION = "regression"
    HOLDOUT = "holdout"
    RELEASE = "release"


class EvidenceMode(StrEnum):
    LIVE_PAIRED = "live_paired"
    PAIRED_IMPORT = "paired_import"
    CANDIDATE_ONLY = "candidate_only"


class DomainDefinition(EvidenceModel):
    schema_version: Literal["eval.domain-definition.v1"] = "eval.domain-definition.v1"
    domain: CapabilityDomain
    task_designs: Annotated[tuple[str, ...], Field(min_length=1)]
    primary_metrics: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    hard_gate: bool


class EvidenceSourceDefinition(EvidenceModel):
    schema_version: Literal["eval.evidence-source-definition.v1"] = (
        "eval.evidence-source-definition.v1"
    )
    source_id: Identifier
    adapter: str
    mode: EvidenceMode
    domains: Annotated[tuple[CapabilityDomain, ...], Field(min_length=1)]
    suites: Annotated[tuple[ParitySuite, ...], Field(min_length=1)]
    description: str

    @model_validator(mode="after")
    def validate_unique_values(self) -> EvidenceSourceDefinition:
        if len(set(self.domains)) != len(self.domains):
            raise ValueError("source domains must be unique")
        if len(set(self.suites)) != len(self.suites):
            raise ValueError("source suites must be unique")
        return self


class SuiteDefinition(EvidenceModel):
    schema_version: Literal["eval.suite-definition.v1"] = "eval.suite-definition.v1"
    suite: ParitySuite
    tier: EvalTier
    split: DatasetSplit
    minimum_repetitions: Annotated[int, Field(gt=0)]
    required_domains: tuple[CapabilityDomain, ...]
    default_source_ids: tuple[Identifier, ...]


class DomainCoverage(EvidenceModel):
    schema_version: Literal["eval.domain-coverage.v1"] = "eval.domain-coverage.v1"
    domain: CapabilityDomain
    paired_source_ids: tuple[Identifier, ...]
    candidate_only_source_ids: tuple[Identifier, ...]
    covered: bool


class CoverageAssessment(EvidenceModel):
    schema_version: Literal["eval.coverage-assessment.v1"] = "eval.coverage-assessment.v1"
    suite: ParitySuite
    complete: bool
    domains: tuple[DomainCoverage, ...]
    errors: tuple[str, ...]


DOMAIN_DEFINITIONS = (
    DomainDefinition(
        domain=CapabilityDomain.CODING_OUTCOME,
        task_designs=("swebench-verified", "harbor", "frozen-repository-bugfix"),
        primary_metrics=("task_success", "test_pass_rate", "data_corruption_rate"),
        hard_gate=True,
    ),
    DomainDefinition(
        domain=CapabilityDomain.AGENT_LOOP,
        task_designs=("multi-step-change", "failed-tool", "failed-test", "no-progress-stop"),
        primary_metrics=("completion_rate", "repeated_call_rate", "stall_rate", "recovery_rate"),
        hard_gate=False,
    ),
    DomainDefinition(
        domain=CapabilityDomain.CONTEXT_MANAGEMENT,
        task_designs=("context-20", "context-50", "context-80", "compaction-offload-resume"),
        primary_metrics=("fact_retention", "source_accuracy", "post_compaction_success", "token_saving"),
        hard_gate=False,
    ),
    DomainDefinition(
        domain=CapabilityDomain.MEMORY,
        task_designs=("locomo", "cross-session-facts", "conflict-update", "forget", "project-isolation"),
        primary_metrics=("recall", "timeliness", "utilization", "consistency", "contamination_rate"),
        hard_gate=False,
    ),
    DomainDefinition(
        domain=CapabilityDomain.TOOLS_AND_PROTOCOLS,
        task_designs=("native-tool-composition", "mcp-composition", "truncation-continuation"),
        primary_metrics=("argument_accuracy", "tool_selection", "edit_completeness", "boundary_rate"),
        hard_gate=False,
    ),
    DomainDefinition(
        domain=CapabilityDomain.SAFETY_AND_PRIVACY,
        task_designs=("credential", "path-escape", "prompt-injection", "network-exfiltration"),
        primary_metrics=("attack_success_rate", "hard_deny_coverage", "secret_exposure", "false_refusal"),
        hard_gate=True,
    ),
    DomainDefinition(
        domain=CapabilityDomain.RELIABILITY_AND_RECOVERY,
        task_designs=("timeout", "crash", "cancel", "checkpoint", "resume", "rollback"),
        primary_metrics=("recovery_rate", "data_loss_rate", "duplicate_effect_rate", "state_consistency"),
        hard_gate=True,
    ),
    DomainDefinition(
        domain=CapabilityDomain.HUMAN_INTERACTION,
        task_designs=("clarification", "permission-prompt", "actionable-failure", "terminal-navigation"),
        primary_metrics=("correct_escalation", "interaction_completion", "unsupported_claim_rate"),
        hard_gate=False,
    ),
    DomainDefinition(
        domain=CapabilityDomain.OPERATIONAL_FITNESS,
        task_designs=("cross-platform", "install", "upgrade", "version-pin", "diagnostics"),
        primary_metrics=("platform_pass_rate", "install_success", "version_drift", "evidence_completeness"),
        hard_gate=True,
    ),
)


EVIDENCE_SOURCES = (
    EvidenceSourceDefinition(
        source_id="canary.standard",
        adapter="eval.canary.adapter:HarnessCanaryAdapter",
        mode=EvidenceMode.LIVE_PAIRED,
        domains=(
            CapabilityDomain.CODING_OUTCOME,
            CapabilityDomain.AGENT_LOOP,
            CapabilityDomain.TOOLS_AND_PROTOCOLS,
        ),
        suites=(ParitySuite.SMOKE, ParitySuite.DEV, ParitySuite.REGRESSION),
        description="Ten deterministic single-file repair tasks.",
    ),
    EvidenceSourceDefinition(
        source_id="canary.advanced",
        adapter="eval.canary.adapter:HarnessCanaryAdapter",
        mode=EvidenceMode.LIVE_PAIRED,
        domains=(
            CapabilityDomain.CODING_OUTCOME,
            CapabilityDomain.AGENT_LOOP,
            CapabilityDomain.CONTEXT_MANAGEMENT,
            CapabilityDomain.TOOLS_AND_PROTOCOLS,
            CapabilityDomain.RELIABILITY_AND_RECOVERY,
        ),
        suites=(ParitySuite.SMOKE, ParitySuite.DEV, ParitySuite.REGRESSION),
        description="Cross-file, checkpoint recovery and conflicting-context tasks.",
    ),
    EvidenceSourceDefinition(
        source_id="swebench.verified",
        adapter="eval.swebench.adapter:SwebenchEvidenceAdapter",
        mode=EvidenceMode.PAIRED_IMPORT,
        domains=(
            CapabilityDomain.CODING_OUTCOME,
            CapabilityDomain.AGENT_LOOP,
            CapabilityDomain.TOOLS_AND_PROTOCOLS,
            CapabilityDomain.RELIABILITY_AND_RECOVERY,
        ),
        suites=(ParitySuite.DEV, ParitySuite.HOLDOUT, ParitySuite.RELEASE),
        description="Normalized paired SWE-bench Verified results and trajectories.",
    ),
    EvidenceSourceDefinition(
        source_id="harbor.terminal-bench",
        adapter="eval.harbor.adapter:HarborEvidenceAdapter",
        mode=EvidenceMode.PAIRED_IMPORT,
        domains=(
            CapabilityDomain.CODING_OUTCOME,
            CapabilityDomain.AGENT_LOOP,
            CapabilityDomain.TOOLS_AND_PROTOCOLS,
            CapabilityDomain.RELIABILITY_AND_RECOVERY,
        ),
        suites=(ParitySuite.DEV, ParitySuite.HOLDOUT, ParitySuite.RELEASE),
        description="Normalized paired Harbor/Terminal-Bench rewards and artifacts.",
    ),
    EvidenceSourceDefinition(
        source_id="locomo.memory",
        adapter="eval.locomo.adapter:LocomoEvidenceAdapter",
        mode=EvidenceMode.PAIRED_IMPORT,
        domains=(
            CapabilityDomain.MEMORY,
            CapabilityDomain.CONTEXT_MANAGEMENT,
            CapabilityDomain.RELIABILITY_AND_RECOVERY,
        ),
        suites=(ParitySuite.DEV, ParitySuite.HOLDOUT, ParitySuite.RELEASE),
        description="Separate, identically configured LoCoMo runs for both harnesses.",
    ),
    EvidenceSourceDefinition(
        source_id="specialist.controlled",
        adapter="eval.specialist.paired:run_specialist_parity",
        mode=EvidenceMode.PAIRED_IMPORT,
        domains=(
            CapabilityDomain.AGENT_LOOP,
            CapabilityDomain.CONTEXT_MANAGEMENT,
            CapabilityDomain.MEMORY,
            CapabilityDomain.TOOLS_AND_PROTOCOLS,
        ),
        suites=(ParitySuite.DEV, ParitySuite.HOLDOUT, ParitySuite.RELEASE),
        description="Controlled single-pass paired specialist tasks and semantic trajectories.",
    ),
    EvidenceSourceDefinition(
        source_id="promptfoo.safety",
        adapter="eval.promptfoo.adapter:PromptfooEvidenceAdapter",
        mode=EvidenceMode.PAIRED_IMPORT,
        domains=(CapabilityDomain.SAFETY_AND_PRIVACY,),
        suites=(ParitySuite.DEV, ParitySuite.REGRESSION, ParitySuite.RELEASE),
        description="Calibrated paired attack and false-refusal evidence.",
    ),
    EvidenceSourceDefinition(
        source_id="human.interaction",
        adapter="eval.parity.imports:load_normalized_bundle",
        mode=EvidenceMode.PAIRED_IMPORT,
        domains=(CapabilityDomain.HUMAN_INTERACTION,),
        suites=(ParitySuite.HOLDOUT, ParitySuite.RELEASE),
        description="Versioned deterministic interaction checks plus calibrated human judgments.",
    ),
    EvidenceSourceDefinition(
        source_id="operational.conformance",
        adapter="eval.parity.imports:load_normalized_bundle",
        mode=EvidenceMode.PAIRED_IMPORT,
        domains=(CapabilityDomain.OPERATIONAL_FITNESS,),
        suites=(ParitySuite.HOLDOUT, ParitySuite.RELEASE),
        description="Three-platform installation, upgrade, pinning and diagnostics evidence.",
    ),
    EvidenceSourceDefinition(
        source_id="native.regression",
        adapter="eval.native.adapter:NativePytestAdapter",
        mode=EvidenceMode.CANDIDATE_ONLY,
        domains=tuple(CapabilityDomain),
        suites=(ParitySuite.DEV, ParitySuite.REGRESSION, ParitySuite.RELEASE),
        description="Candidate release gate; never counted as Claude Code comparative evidence.",
    ),
)


SUITE_DEFINITIONS = (
    SuiteDefinition(
        suite=ParitySuite.SMOKE,
        tier=EvalTier.L0_LOCAL,
        split=DatasetSplit.REGRESSION,
        minimum_repetitions=1,
        required_domains=(CapabilityDomain.CODING_OUTCOME,),
        default_source_ids=("canary.advanced",),
    ),
    SuiteDefinition(
        suite=ParitySuite.DEV,
        tier=EvalTier.L1_PR,
        split=DatasetSplit.DEVELOPMENT,
        minimum_repetitions=1,
        required_domains=(),
        default_source_ids=("canary.standard", "canary.advanced"),
    ),
    SuiteDefinition(
        suite=ParitySuite.REGRESSION,
        tier=EvalTier.L2_NIGHTLY,
        split=DatasetSplit.REGRESSION,
        minimum_repetitions=3,
        required_domains=(
            CapabilityDomain.CODING_OUTCOME,
            CapabilityDomain.AGENT_LOOP,
            CapabilityDomain.CONTEXT_MANAGEMENT,
            CapabilityDomain.TOOLS_AND_PROTOCOLS,
            CapabilityDomain.RELIABILITY_AND_RECOVERY,
        ),
        default_source_ids=("canary.standard", "canary.advanced", "native.regression"),
    ),
    SuiteDefinition(
        suite=ParitySuite.HOLDOUT,
        tier=EvalTier.L3_WEEKLY,
        split=DatasetSplit.HOLDOUT,
        minimum_repetitions=3,
        required_domains=tuple(CapabilityDomain),
        default_source_ids=(
            "swebench.verified",
            "harbor.terminal-bench",
            "locomo.memory",
            "human.interaction",
            "operational.conformance",
        ),
    ),
    SuiteDefinition(
        suite=ParitySuite.RELEASE,
        tier=EvalTier.L4_RELEASE,
        split=DatasetSplit.HOLDOUT,
        minimum_repetitions=3,
        required_domains=tuple(CapabilityDomain),
        default_source_ids=tuple(source.source_id for source in EVIDENCE_SOURCES),
    ),
)


def suite_definition(suite: ParitySuite) -> SuiteDefinition:
    return next(item for item in SUITE_DEFINITIONS if item.suite is suite)


def assess_coverage(
    suite: ParitySuite,
    source_ids: tuple[str, ...] | None = None,
    *,
    observed_domains: set[CapabilityDomain] | None = None,
) -> CoverageAssessment:
    definition = suite_definition(suite)
    selected_ids = set(source_ids or definition.default_source_ids)
    known = {source.source_id: source for source in EVIDENCE_SOURCES}
    unknown = sorted(selected_ids - set(known))
    selected = tuple(known[source_id] for source_id in sorted(selected_ids & set(known)))
    coverages: list[DomainCoverage] = []
    for domain in definition.required_domains:
        paired = tuple(
            source.source_id
            for source in selected
            if domain in source.domains and source.mode is not EvidenceMode.CANDIDATE_ONLY
        )
        candidate_only = tuple(
            source.source_id
            for source in selected
            if domain in source.domains and source.mode is EvidenceMode.CANDIDATE_ONLY
        )
        coverages.append(
            DomainCoverage(
                domain=domain,
                paired_source_ids=paired,
                candidate_only_source_ids=candidate_only,
                covered=bool(paired)
                and (observed_domains is None or domain in observed_domains),
            )
        )
    errors = [f"unknown evidence source: {source_id}" for source_id in unknown]
    errors.extend(
        f"missing paired evidence records for domain: {item.domain.value}"
        for item in coverages
        if not item.covered
    )
    return CoverageAssessment(
        suite=suite,
        complete=not errors,
        domains=tuple(coverages),
        errors=tuple(errors),
    )


def apply_suite_claim_gate(suite: ParitySuite, decision: ParityDecision) -> ParityDecision:
    """Prevent diagnostic tiers from emitting public parity or superiority claims."""

    if suite is ParitySuite.RELEASE or decision.conclusion not in {
        ParityConclusion.EXCEEDS,
        ParityConclusion.PARITY,
    }:
        return decision
    return decision.model_copy(
        update={
            "conclusion": ParityConclusion.INCONCLUSIVE,
            "errors": decision.errors
            + (f"{suite.value} is diagnostic; only release may claim parity or exceeds",),
        }
    )
