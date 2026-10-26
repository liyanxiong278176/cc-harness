"""Normalize local independent Agent outcomes into the authoritative eval/core store."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import os

from cc_harness.config import load_layered_config
from eval.core import (
    AdapterIdentity, CapabilityDomain, DatasetSplit, EnvironmentSpec, EvalRunManifest,
    EvalStore, EvalTier, FailureRecord, GraderContract, GraderResult, GraderType,
    IsolationType, ModelConfiguration, NetworkMode, ResourceBudget, ResourceUsage,
    ResultStatus, RiskLevel, StateProfile, SubjectUnderTest, TaskContract, TrialRequest,
    TrialResult, canonical_json_bytes, content_fingerprint,
)
from eval.core.gates import GatePolicy, evaluate_release
from .catalog import VERSION, cases


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def source_paths(source: Path):
    paths = sorted(set(source.glob("cc_harness/**/*.py")) | set(source.glob("eval/observability/*.py")) |
                   {source / "pyproject.toml", source / "uv.lock"})
    return [p for p in paths if p.is_file()]


def source_fingerprint(source: Path) -> str:
    entries = [(p.relative_to(source).as_posix(), digest(p.read_bytes())) for p in source_paths(source)]
    return digest(json.dumps(entries, separators=(",", ":")).encode())


def model_fingerprint(source: Path) -> str:
    model = load_layered_config(source)
    # Only the digest is persisted. This conservatively binds all layered
    # service/MCP settings and execution controls, including user-level files;
    # credential/configuration values never enter a report or model prompt.
    files = {}
    for root in (source, Path.home() / ".cc-harness"):
        for name in (".env", "mcp.json", "policy.yaml"):
            path = root / name
            if path.is_file():
                files[str(path.resolve())] = digest(path.read_bytes())
    environment = {key: value for key, value in os.environ.items()
                   if key.startswith(("CC_HARNESS_", "CONTEXT_", "MEMORY_", "EMBEDDING_"))}
    payload = {"model": model.model_dump(exclude={"openai_api_key"}), "runtime": model.runtime_environment,
               "environment": environment, "layered_files": files,
               "execution": {"host_execution": True, "permission_mode": "case-defined", "embedding": "local", "workers": 1}}
    return digest(json.dumps(payload, sort_keys=True).encode())


async def normalize(experiment: Path, source: Path, *, split="golden") -> dict:
    """Import a trial only if its frozen execution identity is available.

    Langfuse availability and semantic judgments do not affect this decision.
    Missing trials are deliberately left missing for eval/core to reject.
    """
    existing = []
    for path in sorted(experiment.glob("*/result.json")):
        existing.append((path.parent.name, {name: digest((path.parent / name).read_bytes()) for name in
            ("result.json", "events.json", "reclassification.json") if (path.parent / name).is_file()}))
    revision = digest(json.dumps({"results": existing, "version": VERSION,
        "tasks": [case.fingerprint for case in cases(split)]}, sort_keys=True).encode()).removeprefix("sha256:")[:12]
    previous = experiment / f"decision-{split}.json"
    if previous.exists():
        saved = json.loads(previous.read_text(encoding="utf-8"))
        if saved.get("result_snapshot") == revision:
            return saved
    model = load_layered_config(source)
    store = EvalStore(experiment / "core" / split / revision)
    await store.open()
    try:
        tasks = []
        selected = cases(split)
        for case in selected:
            instruction = await store.put_artifact(case.instruction.encode(), "text/plain")
            initial = await store.put_artifact(canonical_json_bytes({"task_digest": case.fingerprint}), "application/json")
            domain = CapabilityDomain(case.domain)
            tasks.append(TaskContract(task_id=case.task_id, task_version=VERSION, suite_id="observability-agent",
                suite_version=VERSION, title=case.title, risk=RiskLevel.CRITICAL,
                state_profile=StateProfile.RECOVERY if case.recipe == "workflow" else StateProfile.CONTEXT if case.recipe == "context" else StateProfile.CLEAN_CODING,
                domains=(domain,), instruction_ref=instruction, initial_state_ref=initial,
                budget=ResourceBudget(wall_time_seconds=case.wall_seconds + 180, max_steps=10000,
                    max_model_calls=case.max_model_calls, max_tool_calls=1000, max_input_tokens=10000000,
                    max_output_tokens=1000000, max_cost_microusd=0),
                graders=(GraderContract(grader_id="independent-outcome", grader_type=GraderType.DETERMINISTIC,
                    implementation="eval.observability.grader", version=VERSION, domains=(domain,), veto=True),)))
        snapshot = experiment / "execution-identity.json"
        identity = json.loads(snapshot.read_text(encoding="utf-8")) if snapshot.exists() else {}
        bundle = experiment / "source-snapshot.json"
        source_ref = await store.put_artifact(bundle.read_bytes() if bundle.exists() else b"{}", "application/vnd.cc-harness.source-snapshot+json")
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, capture_output=True, text=True, check=True).stdout.strip()
        current = source_fingerprint(source)
        source_digest = identity.get("source_fingerprint", current)
        configuration = identity.get("model_fingerprint", model_fingerprint(source))
        manifest = EvalRunManifest(run_id="observability-" + experiment.name.lower() + "-" + split + "-" + revision,
            created_at=datetime.now(timezone.utc), tier=EvalTier.L2_NIGHTLY,
            split=DatasetSplit.HOLDOUT if split == "holdout" else DatasetSplit.REGRESSION,
            subject=SubjectUnderTest(subject_id="cc-harness", product_version="0.1.0", source_commit=commit,
                source_dirty=True, source_patch_ref=source_ref, executable_digest=source_digest,
                harness_profile_digest=configuration, model=ModelConfiguration(provider="configured-service",
                    requested_model=model.openai_model, resolved_model=model.openai_model,
                    api_protocol="openai-compatible", parameters_digest=configuration)),
            task_contract_digests=tuple(content_fingerprint(task) for task in tasks),
            environment=EnvironmentSpec(environment_id="local-disposable-project", isolation=IsolationType.PROCESS,
                os_name=platform.system().lower(), os_version=platform.version(), architecture=platform.machine().lower(),
                image_digest=digest(b"native-agent; isolated-docker-function-oracle"), dependencies_digest=digest((source / "pyproject.toml").read_bytes()),
                network_mode=NetworkMode.UNRESTRICTED, locale="en-US", timezone="UTC"),
            default_budget=tasks[0].budget, random_seed=0, repetitions=1, orchestration_version=VERSION,
            evidence_store_uri=str(store.root.resolve()))
        manifest_digest = await store.create_run(manifest)
        adapter = AdapterIdentity(adapter_id="observability-runtime", adapter_version=VERSION)
        for task in tasks:
            await store.enqueue_trial(TrialRequest(trial_id=manifest.run_id + "/" + task.task_id,
                run_id=manifest.run_id, run_manifest_digest=manifest_digest, task=task, adapter=adapter, seed=0))
        while lease := await store.claim_next("local-evidence-import"):
            case = next(c for c in selected if c.task_id == lease.request.task.task_id)
            path = experiment / case.task_id / "result.json"
            if not path.exists():
                await store.mark_attempt_outcome_unknown(lease, "required trial evidence is missing")
                continue
            result = json.loads(path.read_text(encoding="utf-8"))
            correction = path.parent / "reclassification.json"
            if correction.exists():
                value = json.loads(correction.read_text(encoding="utf-8"))
                if value.get("original_result_digest") == digest(path.read_bytes()) and value.get("status") == "invalid":
                    result.update(value)
            outcome = await store.put_artifact(canonical_json_bytes(result), "application/json")
            event_path = path.parent / "events.json"
            trajectory = await store.put_artifact(event_path.read_bytes(), "application/json") if event_path.exists() else None
            invalid = result.get("dataset_version", "1.0.0") != VERSION or result.get("task_digest") != case.fingerprint or trajectory is None or not identity or result.get("source_fingerprint") != source_digest or result.get("model_fingerprint") != configuration
            invalid = invalid or not bundle.exists() or result.get("status") == "invalid" or result.get("source_changed_during_trial", True)
            passed = result.get("status") == "pass" and result.get("files", {}).get("passed") is True and result.get("function", {}).get("passed") is True and all(result.get("trajectory", {}).values())
            status = ResultStatus.INVALID if invalid else ResultStatus.PASS if passed else ResultStatus.FAIL
            usage = result.get("usage", {})
            finished = datetime.now(timezone.utc)
            seconds = float(result.get("wall_seconds", 0))
            normalized = TrialResult(trial_id=lease.trial_id, run_id=manifest.run_id, run_manifest_digest=manifest_digest,
                task_id=case.task_id, task_contract_digest=content_fingerprint(lease.request.task), attempt=lease.attempt,
                adapter=adapter, status=status, started_at=finished - timedelta(seconds=seconds), finished_at=finished,
                usage=ResourceUsage(wall_time_ms=int(seconds * 1000), steps=len(json.loads(event_path.read_text(encoding="utf-8"))) if trajectory else 0,
                    model_calls=int(usage.get("model_calls", 0)), tool_calls=int(usage.get("tool_calls", 0)),
                    input_tokens=int(usage.get("input_tokens", 0)), output_tokens=int(usage.get("output_tokens", 0))),
                grader_results=() if invalid else (GraderResult(grader_id="independent-outcome", status=status, score=1.0 if passed else 0.0),),
                outcome_ref=outcome, trajectory_ref=trajectory,
                invalid_reason="missing, changed, or incompatible execution identity/evidence" if invalid else None,
                failure=FailureRecord(category="independent-outcome-failed", message="Required outcome or trajectory checks failed", evidence_ref=outcome) if status is ResultStatus.FAIL else None)
            await store.complete_attempt(lease, normalized)
        results = await store.list_results(manifest.run_id)
        policy = GatePolicy(policy_id="observability-required-evidence", policy_version=VERSION,
            required_domains=tuple(dict.fromkeys(domain for task in tasks for domain in task.domains)), invalid_is_veto=True, missing_is_veto=True)
        decision = evaluate_release(manifest, tuple(tasks), results, policy=policy)
        reference = await store.record_release_decision(decision)
        report = {"decision_digest": reference, "decision": decision.model_dump(mode="json"), "source_fingerprint": source_digest,
                  "model_fingerprint": configuration, "dataset_version": VERSION, "result_snapshot": revision}
        (experiment / f"decision-{split}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        await store.close()
