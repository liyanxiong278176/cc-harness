"""Analyze normalized paired evidence and write the standard parity bundle."""

from __future__ import annotations

import shutil
from pathlib import Path

from eval.core import (
    CapabilityDomain,
    ParityConclusion,
    ParityDecisionPolicy,
    ParityTrialObservation,
    evaluate_parity_decision,
)

from .catalog import ParitySuite, apply_suite_claim_gate, assess_coverage, suite_definition
from .imports import LoadedPairBundle, load_normalized_bundle
from .live import (
    LiveParityResult,
    _digest,
    _integrity_projection,
    _render_report,
    _safe,
    _write_json,
)
from .validation import DEFAULT_CLAUDE_CODE_VERSION


def analyze_imported_parity(
    evidence_paths: tuple[Path, ...],
    evidence_root: Path,
    *,
    suite: ParitySuite,
    expected_claude_code_version: str = DEFAULT_CLAUDE_CODE_VERSION,
) -> LiveParityResult:
    if not evidence_paths:
        raise ValueError("import analysis requires at least one normalized evidence bundle")
    root = evidence_root.resolve()
    loaded = tuple(
        load_normalized_bundle(
            path,
            expected_claude_code_version=expected_claude_code_version,
        )
        for path in evidence_paths
    )
    source_ids = tuple(sorted({item.bundle.source_id for item in loaded}))
    observed_domains = {
        domain
        for item in loaded
        for record in item.bundle.records
        for domain in record.domains
    }
    coverage = assess_coverage(suite, source_ids, observed_domains=observed_domains)
    observations, pairs = _project_observations(loaded)
    definition = suite_definition(suite)
    decision = evaluate_parity_decision(
        f"imported-{suite.value}",
        observations,
        policy=ParityDecisionPolicy(
            minimum_task_clusters=10,
            minimum_repetitions=definition.minimum_repetitions,
        ),
    )
    if not coverage.complete and decision.conclusion is not ParityConclusion.INVALID:
        decision = decision.model_copy(
            update={
                "conclusion": ParityConclusion.INCONCLUSIVE,
                "errors": decision.errors + coverage.errors,
            }
        )
    decision = apply_suite_claim_gate(suite, decision)
    for directory in ("trials", "trajectories", "patches", "scoring"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    _materialize_imports(loaded, root)
    _write_json(
        root / "manifest.json",
        {
            "schema_version": "eval.imported-parity-manifest.v1",
            "suite": suite.value,
            "source_ids": list(source_ids),
            "coverage": coverage.model_dump(mode="json"),
            "imports": [
                {"path": item.path, "source_id": item.bundle.source_id} for item in loaded
            ],
        },
    )
    _write_json(
        root / "schedule.json",
        {
            "schema_version": "eval.imported-schedule.v1",
            "pairs": [
                {
                    "pair_id": pair["pair_id"],
                    "task_id": pair["task_id"],
                    "repetition": pair["repetition"],
                    "order": pair["order"],
                }
                for pair in pairs
            ],
        },
    )
    summary = {
        "schema_version": "eval.claude-parity-summary.v1",
        "comparison_id": decision.comparison_id,
        "suite": suite.value,
        "candidate": "cc-harness",
        "baseline": "claude-code",
        "model": "deepseek-v4-flash",
        "execution_profile": {
            "mode": "imported",
            "emergency_watchdog_seconds": None,
        },
        "decision": decision.model_dump(mode="json"),
        "coverage": coverage.model_dump(mode="json"),
        "domains": _imported_domain_summaries(pairs),
        "pairs": pairs,
    }
    summary_path = root / "summary.json"
    report_path = root / "parity-report.md"
    integrity_path = root / "integrity.json"
    _write_json(summary_path, summary)
    report_path.write_text(_render_report(summary), encoding="utf-8", newline="\n")
    _write_json(integrity_path, _integrity_projection(root))
    return LiveParityResult(
        evidence_root=root,
        summary_path=summary_path,
        report_path=report_path,
        integrity_path=integrity_path,
        conclusion=decision.conclusion.value,
        summary_digest=_digest(summary_path.read_bytes()),
        report_digest=_digest(report_path.read_bytes()),
    )


def _project_observations(
    loaded: tuple[LoadedPairBundle, ...],
) -> tuple[tuple[ParityTrialObservation, ...], list[dict]]:
    observations: list[ParityTrialObservation] = []
    pairs: list[dict] = []
    seen: set[str] = set()
    for item in loaded:
        for record in item.bundle.records:
            pair_id = f"{item.bundle.source_id}.{record.pair_id}"
            task_id = f"{item.bundle.source_id}.{record.task_id}"
            if pair_id in seen:
                raise ValueError(f"duplicate imported pair identity: {pair_id}")
            seen.add(pair_id)
            observations.append(
                ParityTrialObservation(
                    pair_id=pair_id,
                    task_id=task_id,
                    repetition=record.repetition,
                    candidate_status=record.candidate.status,
                    baseline_status=record.baseline.status,
                    candidate_usage=record.candidate.usage,
                    baseline_usage=record.baseline.usage,
                    veto_regression=record.veto_regression,
                )
            )
            pairs.append(
                {
                    "pair_id": pair_id,
                    "task_id": task_id,
                    "title": record.task_id,
                    "source_id": item.bundle.source_id,
                    "domains": [domain.value for domain in record.domains],
                    "risk": "imported",
                    "repetition": record.repetition,
                    "order": [harness.value for harness in record.order],
                    "candidate": {
                        "status": record.candidate.status.value,
                        "usage": record.candidate.usage.model_dump(mode="json"),
                    },
                    "baseline": {
                        "status": record.baseline.status.value,
                        "usage": record.baseline.usage.model_dump(mode="json"),
                    },
                    "veto_regression": record.veto_regression,
                }
            )
    return tuple(observations), pairs


def _materialize_imports(loaded: tuple[LoadedPairBundle, ...], root: Path) -> None:
    for item in loaded:
        source_root = Path(item.path).parent
        source_id = _safe(item.bundle.source_id)
        shutil.copy2(item.path, root / "scoring" / f"{source_id}.bundle.json")
        for record in item.bundle.records:
            record_name = _safe(f"{item.bundle.source_id}.{record.pair_id}")
            _write_json(root / "trials" / f"{record_name}.json", record.model_dump(mode="json"))
            for harness, result in (("candidate", record.candidate), ("baseline", record.baseline)):
                for kind, relative in (
                    ("trajectories", result.trajectory_path),
                    ("patches", result.patch_path),
                    ("scoring", result.grader_path),
                ):
                    if relative is not None:
                        suffix = Path(relative).suffix or ".bin"
                        shutil.copy2(
                            source_root / relative,
                            root / kind / f"{record_name}.{harness}{suffix}",
                        )


def _imported_domain_summaries(pairs: list[dict]) -> dict:
    summaries = {}
    for domain in CapabilityDomain:
        matching = [pair for pair in pairs if domain.value in pair["domains"]]
        valid = [
            pair
            for pair in matching
            if "invalid" not in {pair["candidate"]["status"], pair["baseline"]["status"]}
        ]
        summaries[domain.value] = {
            "declared": bool(matching),
            "pair_count": len(matching),
            "valid_pair_count": len(valid),
            "candidate_passes": sum(pair["candidate"]["status"] == "pass" for pair in valid),
            "baseline_passes": sum(pair["baseline"]["status"] == "pass" for pair in valid),
            "veto_regressions": sum(pair["veto_regression"] for pair in matching),
        }
    return summaries
