"""End-to-end live runner for same-model cc-harness and Claude Code canaries."""

from __future__ import annotations

import asyncio
import hashlib
import locale
import platform
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from eval.core import (
    DatasetSplit,
    EnvironmentSpec,
    EvalRunManifest,
    EvalStore,
    EvalTier,
    IsolationType,
    LocalEvalRunner,
    ModelConfiguration,
    NetworkMode,
    PairedObservation,
    ResourceBudget,
    SubjectUnderTest,
    TaskContract,
    TrialResult,
    canonical_json_bytes,
    content_fingerprint,
    evaluate_paired_comparison,
    summarize_usage,
)
from eval.launch import (
    PARITY_MODEL,
    CompletedLaunch,
    HarnessKind,
    LaunchProfile,
    LaunchRequest,
    build_invocation,
    run_invocation,
    standard_profiles,
)

from .adapter import HarnessCanaryAdapter
from .advanced_catalog import install_advanced_canary_contracts
from .retry import PairedCanaryRunner, PairedRetryPolicy, PairedTaskSelection

_HARNESSES = (HarnessKind.CC_HARNESS, HarnessKind.CLAUDE_CODE)


@dataclass(frozen=True)
class PreflightRecord:
    profile: LaunchProfile
    completed: CompletedLaunch
    evidence_ref_digest: str


@dataclass(frozen=True)
class LiveCanaryResult:
    evidence_root: Path
    summary_path: Path
    report_path: Path
    summary_digest: str
    report_digest: str


async def run_live_advanced_canary(
    project_root: Path,
    evidence_root: Path,
    *,
    claude_settings_path: Path,
    task_ids: tuple[str, ...] = (),
    maximum_attempts: int = 2,
    cooldown_seconds: float = 30.0,
    progress: Callable[[str], None] | None = None,
) -> LiveCanaryResult:
    project_root = project_root.resolve()
    evidence_root = evidence_root.resolve()
    env_file = project_root / ".env"
    if not env_file.is_file():
        raise ValueError(f"project .env is missing: {env_file}")
    profiles = standard_profiles(claude_settings_path=claude_settings_path)
    if {profile.harness for profile in profiles} != set(_HARNESSES):
        raise ValueError("live canary requires exactly cc-harness and Claude Code profiles")

    store = EvalStore(evidence_root)
    _report(progress, f"opening evidence store: {evidence_root}")
    await store.open()
    try:
        _report(progress, "installing and selecting advanced canary contracts")
        contracts = await install_advanced_canary_contracts(store)
        contracts = _select_contracts(contracts, task_ids)
        _report(progress, f"selected {len(contracts)} task(s)")
        preflights = await _preflight_profiles(store, profiles, env_file, progress=progress)
        comparison_id = f"advanced-{datetime.now(UTC).strftime('%Y%m%dt%H%M%Sz').lower()}"
        environment = _environment_spec(project_root)
        source_snapshot_ref = await store.put_artifact(
            _source_snapshot(project_root),
            "application/vnd.cc-harness.source-snapshot+zip",
        )
        manifests = {
            record.profile.harness: _manifest(
                project_root,
                evidence_root,
                contracts,
                environment,
                comparison_id,
                record,
                source_snapshot_ref,
            )
            for record in preflights
        }
        for harness in _HARNESSES:
            await store.create_run(manifests[harness])

        adapters = {
            profile.harness: HarnessCanaryAdapter(
                profile,
                environment_files=(env_file,),
            )
            for profile in profiles
        }
        local_runner = LocalEvalRunner(
            store,
            tuple(adapters[harness] for harness in _HARNESSES),
            worker_id=f"worker-{comparison_id}",
            heartbeat_interval_seconds=5.0,
            stale_after_seconds=30.0,
        )
        paired_runner = PairedCanaryRunner(
            store,
            local_runner,
            manifests,
            {harness: adapters[harness].identity for harness in _HARNESSES},
            policy=PairedRetryPolicy(
                maximum_attempts=maximum_attempts,
                cooldown_seconds=cooldown_seconds,
            ),
            progress=progress,
        )
        selections = await paired_runner.run(contracts)
        _report(progress, "building signed summary and Markdown report")
        summary, markdown = await _build_report(
            store,
            comparison_id,
            contracts,
            manifests,
            preflights,
            selections,
        )
        summary_bytes = canonical_json_bytes(summary)
        report_bytes = markdown.encode("utf-8")
        summary_ref, report_ref = await asyncio.gather(
            store.put_artifact(summary_bytes, "application/vnd.cc-harness.canary-summary+json"),
            store.put_artifact(report_bytes, "text/markdown"),
        )
        evidence_root.mkdir(parents=True, exist_ok=True)
        summary_path = evidence_root / "summary.json"
        report_path = evidence_root / "report.md"
        summary_path.write_bytes(summary_bytes)
        report_path.write_bytes(report_bytes)
        return LiveCanaryResult(
            evidence_root=evidence_root,
            summary_path=summary_path,
            report_path=report_path,
            summary_digest=summary_ref.digest,
            report_digest=report_ref.digest,
        )
    finally:
        await store.close()


def default_evidence_root(project_root: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dt%H%M%Sz").lower()
    return project_root / "eval" / "result" / f"advanced-{stamp}"


async def _preflight_profiles(
    store: EvalStore,
    profiles: tuple[LaunchProfile, ...],
    env_file: Path,
    *,
    progress: Callable[[str], None] | None = None,
) -> tuple[PreflightRecord, ...]:
    budget = ResourceBudget(
        wall_time_seconds=90,
        max_steps=10,
        max_model_calls=5,
        max_tool_calls=10,
        max_input_tokens=50_000,
        max_output_tokens=2_000,
        max_cost_microusd=250_000,
    )
    records: list[PreflightRecord] = []
    for profile in profiles:
        _report(progress, f"preflight starting: {profile.harness.value}")
        with tempfile.TemporaryDirectory(
            prefix=f"canary-preflight-{profile.harness.value}-"
        ) as raw:
            workspace = Path(raw)
            (workspace / "README.txt").write_text(
                "Harness model identity preflight.\n", encoding="utf-8"
            )
            request = LaunchRequest(
                prompt="Reply with READY only. Do not modify files and do not call tools.",
                budget=budget,
            )
            invocation = build_invocation(
                profile,
                request,
                workspace,
                environment_files=(env_file,),
            )
            completed = await run_invocation(profile, invocation, timeout_seconds=90)
        evidence_ref, stdout_ref, stderr_ref = await asyncio.gather(
            store.put_artifact(
                canonical_json_bytes(completed.evidence),
                "application/vnd.cc-harness.launch-evidence+json",
            ),
            store.put_artifact(completed.stdout, "application/x-ndjson"),
            store.put_artifact(completed.stderr, "text/plain"),
        )
        bundle_ref = await store.put_artifact(
            canonical_json_bytes(
                {
                    "schema_version": "eval.canary-preflight.v1",
                    "profile_digest": content_fingerprint(profile),
                    "launch_evidence_digest": evidence_ref.digest,
                    "stdout_digest": stdout_ref.digest,
                    "stderr_digest": stderr_ref.digest,
                }
            ),
            "application/vnd.cc-harness.canary-preflight+json",
        )
        if not completed.evidence.valid_for_parity:
            raise RuntimeError(
                f"{profile.harness.value} model preflight failed: "
                f"{completed.evidence.model_dump(mode='json')}"
            )
        records.append(
            PreflightRecord(
                profile=profile,
                completed=completed,
                evidence_ref_digest=bundle_ref.digest,
            )
        )
        _report(progress, f"preflight passed: {profile.harness.value}")
    return tuple(records)


def _report(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _manifest(
    project_root: Path,
    evidence_root: Path,
    contracts: tuple[TaskContract, ...],
    environment: EnvironmentSpec,
    comparison_id: str,
    preflight: PreflightRecord,
    source_snapshot_ref,
    *,
    tier: EvalTier = EvalTier.L2_NIGHTLY,
    split: DatasetSplit = DatasetSplit.REGRESSION,
    repetitions: int = 1,
    random_seed: int = 20260804,
) -> EvalRunManifest:
    profile = preflight.profile
    executable = Path(profile.executable).resolve()
    executable_bytes = executable.read_bytes()
    executable_digest = _digest(executable_bytes)
    if profile.harness is HarnessKind.CC_HARNESS:
        source_commit = _git(project_root, "rev-parse", "HEAD").strip()
        dirty = bool(_git(project_root, "status", "--porcelain", "--", "cc_harness", "eval"))
        patch_ref = source_snapshot_ref if dirty else None
        product_version = _package_version()
        subject_id = "cc-harness"
    else:
        source_commit = hashlib.sha1(executable_bytes).hexdigest()
        dirty = False
        patch_ref = None
        product_version = _executable_version(executable)
        subject_id = "claude-code"
    model = ModelConfiguration(
        provider=profile.provider_route_id,
        requested_model=PARITY_MODEL,
        resolved_model=preflight.completed.evidence.resolved_model or "",
        api_protocol=(
            "openai-compatible"
            if profile.harness is HarnessKind.CC_HARNESS
            else "anthropic-compatible"
        ),
        parameters_digest=content_fingerprint(
            {
                "profile": profile.model_dump(mode="json"),
                "preflight_digest": preflight.evidence_ref_digest,
            }
        ),
    )
    run_id = f"{comparison_id}-{'cc' if profile.harness is HarnessKind.CC_HARNESS else 'claude'}"
    return EvalRunManifest(
        run_id=run_id,
        created_at=datetime.now(UTC),
        tier=tier,
        split=split,
        comparison_group_id=comparison_id,
        subject=SubjectUnderTest(
            subject_id=subject_id,
            product_version=product_version,
            source_commit=source_commit,
            source_dirty=dirty,
            source_patch_ref=patch_ref,
            executable_digest=executable_digest,
            harness_profile_digest=content_fingerprint(profile),
            model=model,
        ),
        task_contract_digests=tuple(content_fingerprint(contract) for contract in contracts),
        environment=environment,
        default_budget=contracts[0].budget,
        random_seed=random_seed,
        repetitions=repetitions,
        orchestration_version="1.0.0",
        evidence_store_uri=evidence_root.as_uri(),
    )


def _environment_spec(project_root: Path) -> EnvironmentSpec:
    dependency_file = project_root / "uv.lock"
    dependency_bytes = dependency_file.read_bytes() if dependency_file.is_file() else b""
    system = platform.system().lower() or "unknown"
    architecture = (platform.machine().lower() or "unknown").replace("-", "_")
    locale_name = locale.getlocale()[0] or "unknown"
    os_version = platform.version() or platform.release() or "unknown"
    environment_identity = canonical_json_bytes(
        {
            "system": system,
            "version": os_version,
            "architecture": architecture,
            "python": platform.python_version(),
        }
    )
    return EnvironmentSpec(
        environment_id="local-windows-process-v1",
        isolation=IsolationType.PROCESS,
        os_name=system,
        os_version=os_version,
        architecture=architecture,
        image_digest=_digest(environment_identity),
        dependencies_digest=_digest(dependency_bytes),
        network_mode=NetworkMode.UNRESTRICTED,
        locale=locale_name,
        timezone="Asia/Shanghai",
    )


async def _build_report(
    store: EvalStore,
    comparison_id: str,
    contracts: tuple[TaskContract, ...],
    manifests: dict[HarnessKind, EvalRunManifest],
    preflights: tuple[PreflightRecord, ...],
    selections: tuple[PairedTaskSelection, ...],
) -> tuple[dict, str]:
    contracts_by_id = {contract.task_id: contract for contract in contracts}
    observations: list[PairedObservation] = []
    cc_selected: list[TrialResult] = []
    claude_selected: list[TrialResult] = []
    pairs: list[dict] = []
    for selection in selections:
        contract = contracts_by_id[selection.task_id]
        if selection.valid_pair_selected:
            cc_result = await store.get_result(selection.selected_cc_harness_trial_id or "")
            claude_result = await store.get_result(selection.selected_claude_code_trial_id or "")
            assert cc_result is not None and claude_result is not None
            cc_selected.append(cc_result)
            claude_selected.append(claude_result)
        else:
            cc_result = await store.get_result(selection.attempts[-1].cc_harness_trial_id)
            claude_result = await store.get_result(selection.attempts[-1].claude_code_trial_id)
            assert cc_result is not None and claude_result is not None
        observation = PairedObservation(
            pair_id=selection.task_id,
            task_contract_digest=content_fingerprint(contract),
            candidate_status=cc_result.status,
            baseline_status=claude_result.status,
        )
        observations.append(observation)
        pairs.append(
            {
                "task_id": selection.task_id,
                "title": contract.title,
                "risk": contract.risk.value,
                "outcome": observation.outcome.value,
                "selected": selection.valid_pair_selected,
                "cc_harness": _result_summary(cc_result),
                "claude_code": _result_summary(claude_result),
                "attempts": [item.model_dump(mode="json") for item in selection.attempts],
            }
        )
    comparison = evaluate_paired_comparison(
        comparison_id,
        tuple(observations),
        minimum_discordant=10,
    )
    summary = {
        "schema_version": "eval.live-canary-summary.v1",
        "comparison_id": comparison_id,
        "model": PARITY_MODEL,
        "candidate": "cc-harness",
        "baseline": "claude-code",
        "comparison": comparison.model_dump(mode="json"),
        "usage": {
            "cc_harness": summarize_usage(tuple(cc_selected)).model_dump(mode="json"),
            "claude_code": summarize_usage(tuple(claude_selected)).model_dump(mode="json"),
        },
        "run_manifest_digests": {
            harness.value: content_fingerprint(manifests[harness]) for harness in _HARNESSES
        },
        "preflight_digests": {
            item.profile.harness.value: item.evidence_ref_digest for item in preflights
        },
        "pairs": pairs,
    }
    lines = [
        "# Advanced cc-harness vs Claude Code Canary",
        "",
        f"Model: `{PARITY_MODEL}` on both harnesses  ",
        f"Comparison: `{comparison_id}`  ",
        f"Decision: `{comparison.status.value}`",
        "",
        "| Task | Risk | cc-harness | Claude Code | Pair outcome | Attempts |",
        "|---|---|---:|---:|---|---:|",
    ]
    for pair in pairs:
        lines.append(
            f"| `{pair['task_id']}` | {pair['risk']} | {pair['cc_harness']['status']} | "
            f"{pair['claude_code']['status']} | {pair['outcome']} | {len(pair['attempts'])} |"
        )
    lines.extend(
        [
            "",
            (
                f"Candidate wins: {comparison.candidate_wins}; baseline wins: "
                f"{comparison.baseline_wins}; ties: {comparison.ties}; "
                f"invalid: {comparison.invalid}."
            ),
            "",
            "The result remains inconclusive until at least 10 discordant valid pairs exist.",
        ]
    )
    return summary, "\n".join(lines) + "\n"


def _result_summary(result: TrialResult) -> dict:
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


def _select_contracts(
    contracts: tuple[TaskContract, ...],
    task_ids: tuple[str, ...],
) -> tuple[TaskContract, ...]:
    if not task_ids:
        return contracts
    selected = tuple(contract for contract in contracts if contract.task_id in set(task_ids))
    missing = set(task_ids) - {contract.task_id for contract in selected}
    if missing:
        raise ValueError(f"unknown advanced canary task ids: {sorted(missing)}")
    return selected


def _source_snapshot(root: Path) -> bytes:
    paths = [
        *root.glob("cc_harness/**/*.py"),
        *root.glob("eval/**/*.py"),
        *(path for path in (root / "pyproject.toml", root / "uv.lock") if path.is_file()),
    ]
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_STORED) as archive:
        for path in sorted(set(paths)):
            relative = path.relative_to(root).as_posix()
            info = ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_STORED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", *args),
        cwd=root,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout


def _package_version() -> str:
    try:
        return version("cc-harness")
    except PackageNotFoundError:
        return "0.1.0"


def _executable_version(executable: Path) -> str:
    completed = subprocess.run(
        (str(executable), "--version"),
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=15,
    )
    return completed.stdout.decode("utf-8", errors="replace").strip()[:128]


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"
