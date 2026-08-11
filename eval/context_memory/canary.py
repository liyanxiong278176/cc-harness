"""Deterministic recovery and tamper canaries, excluded from benchmark scoring."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from eval.cc_only.storage import atomic_json, digest_file, digest_json

from .contracts import Arm, ArmOutcome, BenchmarkTask, EvalProfile, ExecutionStatus, TrialContext
from .execution import (
    restore_runtime,
    restored_runtime_matches,
    snapshot_runtime,
    write_source_manifest,
)
from .gates import evaluate_trial_gates
from .isolation import _short_runtime_root, open_runtime, seal_runtime
from .storage import verify_attempt_integrity, write_attempt_integrity

RECOVERY_STAGES = (
    "after_event",
    "after_offload_ref",
    "after_summary_commit",
    "after_checkpoint_commit",
)
TAMPER_TARGETS = {
    "source_event": "source-events.jsonl",
    "summary": "summary-v0001.json",
    "node": "nodes.jsonl",
    "ref": "refs/object.md",
    "checkpoint": "checkpoint.json",
}


class InjectedCrash(RuntimeError):
    pass


def run_recovery_tamper_canaries(root: Path) -> dict[str, Any]:
    """Run fixed crash points and corruption probes without model calls."""

    root = root.resolve()
    report_path = root / "report.json"
    if report_path.is_file():
        value = json.loads(report_path.read_text(encoding="utf-8"))
        if value.get("production_path", {}).get("passed") and value.get(
            "evidence_digest"
        ) == _report_digest(value):
            return value
    root.mkdir(parents=True, exist_ok=True)
    recovery = {}
    for stage in RECOVERY_STAGES:
        case_root = root / "recovery" / stage
        if case_root.exists():
            shutil.rmtree(case_root)
        case_root.mkdir(parents=True)
        try:
            _resume_fixture(case_root, crash_after=stage)
        except InjectedCrash:
            pass
        _resume_fixture(case_root)
        first_digest = _tree_digest(case_root)
        _resume_fixture(case_root)
        second_digest = _tree_digest(case_root)
        recovery[stage] = {
            "resumed": _verify_fixture(case_root)["passed"],
            "idempotent": first_digest == second_digest,
            "state_digest": second_digest,
        }

    tamper = {}
    for name, relative in TAMPER_TARGETS.items():
        case_root = root / "tamper" / name
        if case_root.exists():
            shutil.rmtree(case_root)
        case_root.mkdir(parents=True)
        _resume_fixture(case_root)
        target = case_root / relative
        with target.open("ab") as handle:
            handle.write(b"\nTAMPERED\n")
            handle.flush()
            os.fsync(handle.fileno())
        verification = _verify_fixture(case_root)
        tamper[name] = {
            "detected": not verification["passed"],
            "verdict": "invalid" if not verification["passed"] else "incorrectly-valid",
            "errors": verification["errors"],
        }

    production_path = _run_production_path_probe(root / "production-path")

    payload = {
        "schema_version": "eval.context-memory-canary.v1",
        "model_calls": 0,
        "excluded_from_benchmark_score": True,
        "recovery": recovery,
        "tamper": tamper,
        "passed": all(item["resumed"] and item["idempotent"] for item in recovery.values())
        and all(item["detected"] for item in tamper.values())
        and production_path["passed"],
        "production_path": production_path,
    }
    payload["evidence_digest"] = _report_digest(payload)
    atomic_json(report_path, payload)
    return payload


def _resume_fixture(root: Path, crash_after: str | None = None) -> None:
    source = root / "source-events.jsonl"
    if not source.is_file():
        source.write_text(
            json.dumps(
                {"event_id": "canary-event", "kind": "tool_result", "content": "alpha beta"},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        _crash(crash_after, "after_event")
    reference = root / "refs" / "object.md"
    if not reference.is_file():
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_text("alpha beta", encoding="utf-8", newline="\n")
        _crash(crash_after, "after_offload_ref")
    nodes = root / "nodes.jsonl"
    if not nodes.is_file():
        nodes.write_text(
            json.dumps(
                {
                    "node_id": "canary-node",
                    "result_ref": "refs/object.md",
                    "content_digest": digest_file(reference),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    summary = root / "summary-v0001.json"
    if not summary.is_file():
        atomic_json(
            summary,
            {
                "version": 1,
                "source_digest": digest_file(source),
                "before_tokens": 10,
                "after_tokens": 4,
                "summary": "alpha beta",
            },
        )
        _crash(crash_after, "after_summary_commit")
    checkpoint = root / "checkpoint.json"
    if not checkpoint.is_file():
        atomic_json(
            checkpoint,
            {
                "phase": "complete",
                "source_digest": digest_file(source),
                "summary_digest": digest_file(summary),
                "nodes_digest": digest_file(nodes),
                "ref_digest": digest_file(reference),
            },
        )
        _crash(crash_after, "after_checkpoint_commit")
    manifest = root / "integrity.json"
    if not manifest.is_file():
        atomic_json(
            manifest,
            {
                "files": {
                    path.relative_to(root).as_posix(): digest_file(path)
                    for path in (source, reference, nodes, summary, checkpoint)
                }
            },
        )


def _verify_fixture(root: Path) -> dict[str, Any]:
    manifest = root / "integrity.json"
    if not manifest.is_file():
        return {"passed": False, "errors": ["missing-integrity-manifest"]}
    value = json.loads(manifest.read_text(encoding="utf-8"))
    errors = []
    for relative, expected in value.get("files", {}).items():
        path = root / relative
        if not path.is_file():
            errors.append(f"missing:{relative}")
        elif digest_file(path) != expected:
            errors.append(f"digest-mismatch:{relative}")
    return {"passed": not errors, "errors": errors}


def _run_production_path_probe(root: Path) -> dict[str, Any]:
    baseline = _build_production_probe(root / "baseline")
    tamper = {}
    for name in TAMPER_TARGETS:
        probe = _build_production_probe(root / "tamper" / name)
        target = probe["targets"][name]
        with target.open("ab") as handle:
            handle.write(b"\nTAMPERED\n")
            handle.flush()
            os.fsync(handle.fileno())
        verified, errors = verify_attempt_integrity(probe["integrity"])
        tamper[name] = {"detected": not verified, "errors": errors}
    return {
        "recovery_uses_snapshot_restore": baseline["restored"],
        "mechanism_gates_passed": baseline["gates_passed"],
        "sealed_attempt_integrity_passed": baseline["integrity_passed"],
        "tamper": tamper,
        "passed": baseline["restored"]
        and baseline["gates_passed"]
        and baseline["integrity_passed"]
        and all(item["detected"] for item in tamper.values()),
    }


def _build_production_probe(root: Path) -> dict[str, Any]:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    task = BenchmarkTask("canary/production", "canary")
    namespace = "context-memory/canary/production/treatment"
    # The production-path probe intentionally reuses a fixed deep result
    # path.  If a prior process died after opening the external short root,
    # remove that exact generated canary runtime before rebuilding the probe.
    short_active = _short_runtime_root(root, namespace)
    if short_active.exists():
        shutil.rmtree(short_active)
    runtime = open_runtime(root, namespace, resumed=False)
    context = TrialContext(
        project_root=root,
        output_root=root,
        attempt_root=root,
        active_root=runtime.active_root,
        workspace=runtime.workspace,
        home=runtime.home,
        task=task,
        profile=EvalProfile.PORTFOLIO,
        arm=Arm.TREATMENT,
        namespace=namespace,
        watchdog_seconds=1,
    )
    source_digest = write_source_manifest(
        context,
        [{"event_id": "canary-event", "kind": "tool_result", "content": "alpha beta"}],
    )
    mechanism = runtime.workspace / ".cc-harness" / "context" / "canary" / "offload"
    reference = mechanism / "refs" / "canary-node.md"
    reference.parent.mkdir(parents=True)
    reference.write_text("alpha beta", encoding="utf-8", newline="\n")
    event_label = hashlib.sha256(b"canary-event").hexdigest()[:16]
    read_args = {"path": f"benchmark-input/{event_label}/event.txt"}
    nodes = mechanism / "nodes.jsonl"
    nodes.write_text(
        json.dumps(
            {
                "node_id": "canary-node",
                "result_ref": str(reference),
                "content_digest": digest_file(reference),
                "tool_name": "Read",
                "args_digest": "sha256:"
                + hashlib.sha256(
                    json.dumps(read_args, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    summary = runtime.workspace / ".cc-harness" / "context" / "canary" / "summary-v0001.json"
    atomic_json(
        summary,
        {
            "summary_version": 1,
            "source_digest": source_digest,
            "before_tokens": 10,
            "after_tokens": 4,
        },
    )
    journal = runtime.workspace / ".cc-harness" / "action-journal" / "canary.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        json.dumps({"tool": "Read", "args": read_args})
        + "\n"
        + json.dumps({"tool": "search_ref", "args": {"node_id": "canary-node"}})
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    memory = runtime.workspace / ".cc-harness" / "memory.db"
    memory.write_bytes(b"sqlite-canary")
    snapshot = root / "query-snapshot"
    snapshot_runtime(runtime.workspace, runtime.home, snapshot)
    restored_workspace = root / "query-probe" / "workspace"
    restored_home = root / "query-probe" / "home"
    restore_runtime(snapshot, restored_workspace, restored_home)
    restored = restored_runtime_matches(snapshot, restored_workspace, restored_home)
    sealed = seal_runtime(runtime, root)
    outcome = ArmOutcome(
        ExecutionStatus.COMPLETE,
        protocol={
            "source_digest": source_digest,
            "gold_visible_to_sue": False,
            "expect_compaction": True,
            "expect_memory": True,
            "expect_offload": True,
            "expect_ref_retrieval": True,
            "checkpoint_restore_verified": restored,
            "checkpoint_manifest_digest": digest_file(snapshot / "snapshot.json"),
        },
    )
    gates = evaluate_trial_gates(context, sealed, outcome)
    integrity = write_attempt_integrity(root)
    integrity_passed, _errors = verify_attempt_integrity(integrity)
    return {
        "restored": restored,
        "gates_passed": gates["passed"],
        "integrity_passed": integrity_passed,
        "integrity": integrity,
        "targets": {
            "source_event": root / "source-events.jsonl",
            "summary": sealed / summary.relative_to(runtime.active_root),
            "node": sealed / nodes.relative_to(runtime.active_root),
            "ref": sealed / reference.relative_to(runtime.active_root),
            "checkpoint": snapshot / "snapshot.json",
        },
    }


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()


def _report_digest(value: dict[str, Any]) -> str:
    return digest_json({key: item for key, item in value.items() if key != "evidence_digest"})


def _crash(selected: str | None, stage: str) -> None:
    if selected == stage:
        raise InjectedCrash(stage)
