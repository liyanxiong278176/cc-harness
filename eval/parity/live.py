"""Unified live cc-harness versus Claude Code parity pipeline."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from eval.canary import (
    HarnessCanaryAdapter,
    PairedRetryPolicy,
    install_advanced_canary_contracts,
    install_canary_contracts,
    is_transient_provider_result,
)
from eval.canary.live import (
    _environment_spec,
    _manifest,
    _preflight_profiles,
    _source_snapshot,
)
from eval.core import (
    BudgetEnforcement,
    CapabilityDomain,
    EvalStore,
    LocalEvalRunner,
    ParityDecisionPolicy,
    ParityTrialObservation,
    ResultStatus,
    RiskLevel,
    TaskContract,
    TrialResult,
    canonical_json_bytes,
    evaluate_parity_decision,
)
from eval.launch import PARITY_PRICING, HarnessKind, standard_profiles

from .catalog import ParitySuite, apply_suite_claim_gate, assess_coverage, suite_definition
from .runner import ScheduledPairedRunner, ScheduledPairSelection
from .schedule import build_balanced_schedule
from .validation import DEFAULT_CLAUDE_CODE_VERSION, validate_execution_contract

_HARNESSES = (HarnessKind.CC_HARNESS, HarnessKind.CLAUDE_CODE)
_OBSERVATIONAL_VERSION = "1.2.0-observe"


@dataclass(frozen=True)
class LiveParityResult:
    evidence_root: Path
    summary_path: Path
    report_path: Path
    integrity_path: Path
    conclusion: str
    summary_digest: str
    report_digest: str


def default_parity_result_root(project_root: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dt%H%M%Sz").lower()
    return project_root.resolve() / "eval" / "result" / f"parity-{stamp}"


async def run_live_parity(
    project_root: Path,
    evidence_root: Path,
    *,
    suite: ParitySuite,
    claude_settings_path: Path,
    task_ids: tuple[str, ...] = (),
    repetitions: int | None = None,
    random_seed: int = 20260805,
    maximum_attempts: int = 2,
    cooldown_seconds: float = 30.0,
    expected_claude_code_version: str = DEFAULT_CLAUDE_CODE_VERSION,
    observe_unbounded: bool = False,
    emergency_watchdog_seconds: int = 3600,
    progress=None,
) -> LiveParityResult:
    """Run built-in live paired suites and materialize a self-contained evidence bundle."""

    if suite in {ParitySuite.HOLDOUT, ParitySuite.RELEASE}:
        raise ValueError(
            f"{suite.value} requires frozen imported evidence; built-in canaries are not holdout data"
        )
    if emergency_watchdog_seconds <= 60:
        raise ValueError("emergency watchdog must exceed 60 seconds")
    project_root = project_root.resolve()
    evidence_root = evidence_root.resolve()
    env_file = project_root / ".env"
    if not env_file.is_file():
        raise ValueError(f"project .env is missing: {env_file}")
    definition = suite_definition(suite)
    repetitions = repetitions or definition.minimum_repetitions
    if repetitions < definition.minimum_repetitions:
        raise ValueError(
            f"{suite.value} requires at least {definition.minimum_repetitions} repetitions"
        )
    profiles = standard_profiles(claude_settings_path=claude_settings_path)
    store = EvalStore(evidence_root)
    _report(progress, f"opening evidence store: {evidence_root}")
    await store.open()
    try:
        contracts = await _install_contracts(store, suite, task_ids)
        if observe_unbounded:
            contracts = _observational_contracts(contracts, emergency_watchdog_seconds)
        source_ids = _source_ids(contracts)
        coverage = assess_coverage(
            suite,
            source_ids,
            observed_domains={domain for contract in contracts for domain in contract.domains},
        )
        if not coverage.complete:
            raise ValueError("suite coverage is incomplete: " + "; ".join(coverage.errors))
        schedule = build_balanced_schedule(
            tuple(contract.task_id for contract in contracts),
            repetitions=repetitions,
            random_seed=random_seed,
        )
        _write_json(evidence_root / "schedule.json", schedule.model_dump(mode="json"))
        preflights = await _preflight_profiles(store, profiles, env_file, progress=progress)
        comparison_id = f"parity-{datetime.now(UTC).strftime('%Y%m%dt%H%M%Sz').lower()}"
        environment = _environment_spec(project_root)
        source_snapshot_ref = await store.put_artifact(
            _source_snapshot(project_root),
            "application/vnd.cc-harness.source-snapshot+zip",
        )
        manifests = {
            item.profile.harness: _manifest(
                project_root,
                evidence_root,
                contracts,
                environment,
                comparison_id,
                item,
                source_snapshot_ref,
                tier=definition.tier,
                split=definition.split,
                repetitions=repetitions,
                random_seed=random_seed,
            )
            for item in preflights
        }
        validation = validate_execution_contract(
            manifests,
            contracts,
            schedule,
            expected_claude_code_version=expected_claude_code_version,
        )
        if not validation.valid:
            raise ValueError("invalid parity execution contract: " + "; ".join(validation.errors))
        manifest_projection = {
            "schema_version": "eval.parity-manifest.v1",
            "suite": suite.value,
            "execution_profile": {
                "mode": "observational_unbounded" if observe_unbounded else "bounded",
                "emergency_watchdog_seconds": (
                    emergency_watchdog_seconds if observe_unbounded else None
                ),
            },
            "pricing_contract": {
                **PARITY_PRICING.projection(),
                "digest": PARITY_PRICING.digest,
                "cost_semantics": "normalized_comparable_cost",
            },
            "source_ids": list(source_ids),
            "coverage": coverage.model_dump(mode="json"),
            "execution_validation": validation.model_dump(mode="json"),
            "profiles": [item.model_dump(mode="json") for item in profiles],
            "runs": {
                harness.value: manifests[harness].model_dump(mode="json")
                for harness in _HARNESSES
            },
            "preflight_digests": {
                item.profile.harness.value: item.evidence_ref_digest for item in preflights
            },
        }
        _write_json(evidence_root / "manifest.json", manifest_projection)
        for harness in _HARNESSES:
            await store.create_run(manifests[harness])
        adapters = {
            profile.harness: HarnessCanaryAdapter(profile, environment_files=(env_file,))
            for profile in profiles
        }
        local_runner = LocalEvalRunner(
            store,
            tuple(adapters[harness] for harness in _HARNESSES),
            worker_id=f"worker-{comparison_id}",
            heartbeat_interval_seconds=5.0,
            stale_after_seconds=30.0,
        )
        runner = ScheduledPairedRunner(
            store,
            local_runner,
            manifests,
            {harness: adapters[harness].identity for harness in _HARNESSES},
            contracts,
            schedule,
            expected_claude_code_version=expected_claude_code_version,
            retry_policy=PairedRetryPolicy(
                maximum_attempts=maximum_attempts,
                cooldown_seconds=cooldown_seconds,
            ),
            transient_classifier=is_transient_provider_result,
            progress=progress,
        )
        selections = await runner.run()
        decision, pairs = await _evaluate(store, comparison_id, contracts, selections, suite)
        summary = {
            "schema_version": "eval.claude-parity-summary.v1",
            "comparison_id": comparison_id,
            "suite": suite.value,
            "candidate": "cc-harness",
            "baseline": "claude-code",
            "model": "deepseek-v4-flash",
            "execution_profile": manifest_projection["execution_profile"],
            "decision": decision.model_dump(mode="json"),
            "coverage": coverage.model_dump(mode="json"),
            "domains": _domain_summaries(contracts, pairs),
            "pairs": pairs,
        }
        _write_json(evidence_root / "summary.json", summary)
        await _materialize_trial_evidence(store, evidence_root, selections)
        report = _render_report(summary)
        report_path = evidence_root / "parity-report.md"
        _atomic_write(report_path, report.encode("utf-8"))
        integrity_path = evidence_root / "integrity.json"
        _write_json(integrity_path, _integrity_projection(evidence_root))
        summary_path = evidence_root / "summary.json"
        return LiveParityResult(
            evidence_root=evidence_root,
            summary_path=summary_path,
            report_path=report_path,
            integrity_path=integrity_path,
            conclusion=decision.conclusion.value,
            summary_digest=_digest(summary_path.read_bytes()),
            report_digest=_digest(report_path.read_bytes()),
        )
    finally:
        await store.close()


async def _install_contracts(
    store: EvalStore,
    suite: ParitySuite,
    task_ids: tuple[str, ...],
) -> tuple[TaskContract, ...]:
    standard = await install_canary_contracts(store)
    advanced = await install_advanced_canary_contracts(store)
    available = standard + advanced
    if task_ids:
        selected = tuple(item for item in available if item.task_id in set(task_ids))
        missing = set(task_ids) - {item.task_id for item in selected}
        if missing:
            raise ValueError(f"unknown parity task ids: {sorted(missing)}")
        return selected
    if suite is ParitySuite.SMOKE:
        return (advanced[0],)
    return available


def _observational_contracts(
    contracts: tuple[TaskContract, ...],
    emergency_watchdog_seconds: int,
) -> tuple[TaskContract, ...]:
    return tuple(
        contract.model_copy(
            update={
                "task_version": _OBSERVATIONAL_VERSION,
                "suite_version": _OBSERVATIONAL_VERSION,
                "budget": contract.budget.model_copy(
                    update={
                        "enforcement": BudgetEnforcement.OBSERVE,
                        "emergency_watchdog_seconds": emergency_watchdog_seconds,
                    }
                ),
                "tags": tuple(dict.fromkeys((*contract.tags, "observational-unbounded"))),
            }
        )
        for contract in contracts
    )


def _source_ids(contracts: tuple[TaskContract, ...]) -> tuple[str, ...]:
    sources: list[str] = []
    if any(item.suite_id == "harness-canary" for item in contracts):
        sources.append("canary.standard")
    if any(item.suite_id == "harness-canary-advanced" for item in contracts):
        sources.append("canary.advanced")
    return tuple(sources)


async def _evaluate(
    store: EvalStore,
    comparison_id: str,
    contracts: tuple[TaskContract, ...],
    selections: tuple[ScheduledPairSelection, ...],
    suite: ParitySuite,
):
    contracts_by_id = {item.task_id: item for item in contracts}
    observations: list[ParityTrialObservation] = []
    pairs: list[dict] = []
    for selection in selections:
        attempt = selection.attempts[-1]
        candidate_id = selection.selected_candidate_trial_id or attempt.candidate_trial_id
        baseline_id = selection.selected_baseline_trial_id or attempt.baseline_trial_id
        candidate = await store.get_result(candidate_id)
        baseline = await store.get_result(baseline_id)
        if candidate is None or baseline is None:
            raise RuntimeError("selected parity evidence is missing")
        contract = contracts_by_id[selection.pair.task_id]
        veto = _is_veto_regression(contract, candidate, baseline)
        observation = ParityTrialObservation(
            pair_id=f"{selection.pair.task_id}.r{selection.pair.repetition}",
            task_id=selection.pair.task_id,
            repetition=selection.pair.repetition,
            candidate_status=candidate.status,
            baseline_status=baseline.status,
            candidate_usage=candidate.usage,
            baseline_usage=baseline.usage,
            veto_regression=veto,
        )
        observations.append(observation)
        pairs.append(
            {
                "pair_id": observation.pair_id,
                "task_id": contract.task_id,
                "title": contract.title,
                "domains": [item.value for item in contract.domains],
                "risk": contract.risk.value,
                "repetition": selection.pair.repetition,
                "order": [item.value for item in selection.pair.order],
                "candidate": _trial_projection(candidate),
                "baseline": _trial_projection(baseline),
                "veto_regression": veto,
                "attempts": [item.model_dump(mode="json") for item in selection.attempts],
            }
        )
    definition = suite_definition(suite)
    minimum_tasks = 1 if suite is ParitySuite.SMOKE else 10
    decision = evaluate_parity_decision(
        comparison_id,
        tuple(observations),
        policy=ParityDecisionPolicy(
            minimum_task_clusters=minimum_tasks,
            minimum_repetitions=definition.minimum_repetitions,
            random_seed=20260805,
        ),
    )
    decision = apply_suite_claim_gate(suite, decision)
    return decision, pairs


def _is_veto_regression(
    contract: TaskContract,
    candidate: TrialResult,
    baseline: TrialResult,
) -> bool:
    return (
        candidate.status is ResultStatus.FAIL
        and baseline.status is ResultStatus.PASS
        and contract.risk is RiskLevel.CRITICAL
        and any(grader.veto for grader in contract.graders)
    )


def _trial_projection(result: TrialResult) -> dict:
    return {
        "trial_id": result.trial_id,
        "status": result.status.value,
        "invalid_reason": result.invalid_reason,
        "failure": None if result.failure is None else result.failure.model_dump(mode="json"),
        "usage": result.usage.model_dump(mode="json"),
        "outcome_digest": None if result.outcome_ref is None else result.outcome_ref.digest,
        "trajectory_digest": None
        if result.trajectory_ref is None
        else result.trajectory_ref.digest,
    }


def _domain_summaries(contracts: tuple[TaskContract, ...], pairs: list[dict]) -> dict:
    declared = {domain for contract in contracts for domain in contract.domains}
    summaries: dict[str, dict] = {}
    for domain in CapabilityDomain:
        matching = [pair for pair in pairs if domain.value in pair["domains"]]
        valid = [
            pair
            for pair in matching
            if "invalid" not in {pair["candidate"]["status"], pair["baseline"]["status"]}
        ]
        summaries[domain.value] = {
            "declared": domain in declared,
            "pair_count": len(matching),
            "valid_pair_count": len(valid),
            "candidate_passes": sum(pair["candidate"]["status"] == "pass" for pair in valid),
            "baseline_passes": sum(pair["baseline"]["status"] == "pass" for pair in valid),
            "veto_regressions": sum(pair["veto_regression"] for pair in matching),
        }
    return summaries


async def _materialize_trial_evidence(
    store: EvalStore,
    root: Path,
    selections: tuple[ScheduledPairSelection, ...],
) -> None:
    for directory in ("trials", "trajectories", "patches", "scoring"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    for selection in selections:
        scoring_path = root / "scoring" / f"{_safe(selection.pair.task_id)}-r{selection.pair.repetition}.json"
        _write_json(scoring_path, selection.model_dump(mode="json"))
        for attempt in selection.attempts:
            for trial_id in (attempt.candidate_trial_id, attempt.baseline_trial_id):
                if trial_id in seen:
                    continue
                seen.add(trial_id)
                result = await store.get_result(trial_id)
                if result is None:
                    continue
                _write_json(root / "trials" / f"{_safe(trial_id)}.json", result.model_dump(mode="json"))
                if result.trajectory_ref is not None:
                    content = await store.read_artifact(result.trajectory_ref)
                    _atomic_write(root / "trajectories" / f"{_safe(trial_id)}.jsonl", content)
                if result.outcome_ref is not None:
                    content = await store.read_artifact(result.outcome_ref)
                    _atomic_write(root / "patches" / f"{_safe(trial_id)}.json", content)


def _render_report(summary: dict) -> str:
    decision = summary["decision"]
    interval = decision["success_difference"]
    execution_profile = summary.get("execution_profile", {"mode": "legacy"})
    lines = [
        "# cc-harness vs Claude Code Parity Report",
        "",
        f"Suite: `{summary['suite']}`  ",
        f"Model: `{summary['model']}`  ",
        f"Execution profile: `{execution_profile['mode']}`  ",
        f"Decision: `{decision['conclusion']}`",
        "",
        "## Paired Outcome",
        "",
        (
            f"Valid pairs: {decision['valid_pair_count']}; invalid pairs: "
            f"{decision['invalid_pair_count']}; task clusters: "
            f"{decision['task_cluster_count']}."
        ),
    ]
    if interval is not None:
        lines.append(
            "Success-rate difference (cc-harness - Claude Code): "
            f"{interval['estimate']:.3f}, 95% CI "
            f"[{interval['confidence_low']:.3f}, {interval['confidence_high']:.3f}]."
        )
    lines.extend(
        [
            "",
            "## Domains",
            "",
            "| Domain | Valid pairs | cc-harness passes | Claude Code passes | Veto regressions |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for domain, item in summary["domains"].items():
        lines.append(
            f"| {domain} | {item['valid_pair_count']} | {item['candidate_passes']} | "
            f"{item['baseline_passes']} | {item['veto_regressions']} |"
        )
    lines.extend(
        [
            "",
            "## Tasks",
            "",
            "| Task | Rep | Order | cc-harness | Claude Code | Veto |",
            "|---|---:|---|---:|---:|---:|",
        ]
    )
    for pair in summary["pairs"]:
        lines.append(
            f"| `{pair['task_id']}` | {pair['repetition']} | {pair['order'][0]} first | "
            f"{pair['candidate']['status']} | {pair['baseline']['status']} | "
            f"{str(pair['veto_regression']).lower()} |"
        )
    if decision["errors"]:
        lines.extend(["", "## Limitations", ""])
        lines.extend(f"- {item}" for item in decision["errors"])
    return "\n".join(lines) + "\n"


def _integrity_projection(root: Path) -> dict:
    excluded = {"integrity.json", "eval.sqlite3", "eval.sqlite3-shm", "eval.sqlite3-wal"}
    files = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in excluded or "objects" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith("workspaces/"):
            continue
        files[relative] = _digest(path.read_bytes())
    return {
        "schema_version": "eval.parity-integrity.v1",
        "algorithm": "sha256",
        "files": files,
    }


def _write_json(path: Path, value) -> None:
    _atomic_write(path, canonical_json_bytes(value))


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _safe(value: str) -> str:
    return "".join(character if character.isalnum() or character in "._-" else "_" for character in value)


def _report(progress, message: str) -> None:
    if progress is not None:
        progress(message)
