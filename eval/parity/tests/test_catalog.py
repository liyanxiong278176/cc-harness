from eval.core import (
    CapabilityDomain,
    ParityConclusion,
    ParityDecisionPolicy,
    ParityTrialObservation,
    ResourceUsage,
    ResultStatus,
    evaluate_parity_decision,
)
from eval.parity.catalog import (
    DOMAIN_DEFINITIONS,
    EVIDENCE_SOURCES,
    EvidenceMode,
    ParitySuite,
    apply_suite_claim_gate,
    assess_coverage,
)


def test_catalog_defines_every_domain_once() -> None:
    assert {item.domain for item in DOMAIN_DEFINITIONS} == set(CapabilityDomain)
    assert len(DOMAIN_DEFINITIONS) == len(CapabilityDomain)


def test_every_catalog_adapter_resolves() -> None:
    for source in EVIDENCE_SOURCES:
        module_name, attribute = source.adapter.split(":", maxsplit=1)
        assert hasattr(import_module(module_name), attribute), source.source_id


def test_release_catalog_has_a_paired_source_for_every_domain() -> None:
    coverage = assess_coverage(ParitySuite.RELEASE)

    assert coverage.complete is True
    assert {item.domain for item in coverage.domains} == set(CapabilityDomain)
    assert all(item.paired_source_ids for item in coverage.domains)


def test_source_declarations_do_not_replace_observed_domain_records() -> None:
    coverage = assess_coverage(
        ParitySuite.RELEASE,
        tuple(item.source_id for item in EVIDENCE_SOURCES),
        observed_domains={CapabilityDomain.CODING_OUTCOME},
    )

    assert coverage.complete is False
    assert next(
        item for item in coverage.domains if item.domain is CapabilityDomain.MEMORY
    ).covered is False


def test_candidate_only_native_regression_cannot_satisfy_comparative_coverage() -> None:
    coverage = assess_coverage(ParitySuite.RELEASE, ("native.regression",))

    assert coverage.complete is False
    assert all(not item.covered for item in coverage.domains)
    native = next(item for item in EVIDENCE_SOURCES if item.source_id == "native.regression")
    assert native.mode is EvidenceMode.CANDIDATE_ONLY


def test_diagnostic_suite_cannot_emit_positive_release_claim() -> None:
    usage = ResourceUsage(
        wall_time_ms=1,
        steps=1,
        model_calls=1,
        tool_calls=1,
        input_tokens=1,
        output_tokens=1,
        cost_microusd=1,
    )
    observations = tuple(
        ParityTrialObservation(
            pair_id=f"task-{index}.r1",
            task_id=f"task-{index}",
            repetition=1,
            candidate_status=ResultStatus.PASS,
            baseline_status=ResultStatus.PASS,
            candidate_usage=usage,
            baseline_usage=usage,
        )
        for index in range(10)
    )
    decision = evaluate_parity_decision(
        "diagnostic",
        observations,
        policy=ParityDecisionPolicy(
            minimum_task_clusters=10,
            minimum_repetitions=1,
            bootstrap_iterations=100,
        ),
    )

    assert decision.conclusion is ParityConclusion.PARITY
    gated = apply_suite_claim_gate(ParitySuite.DEV, decision)
    assert gated.conclusion is ParityConclusion.INCONCLUSIVE
from importlib import import_module
