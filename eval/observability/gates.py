"""Local quick checks, fixed Agent regression and evidence-bound release wrapper."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from .catalog import VERSION
from .evidence import model_fingerprint, normalize, source_fingerprint
from eval.core.gates import ReleaseDecision
from eval.core.models import EvalRunManifest
from eval.core.serialization import canonical_json_bytes, content_fingerprint

# Current production Runtime contracts replace retired Agent test paths. Each
# group has an explicit capability identity, even when run in one pytest process.
QUICK_GROUPS = {
    "coding_outcome": ("tests/test_native_tools.py", "tests/test_completion_gate.py"),
    "agent_loop": ("tests/runtime_rebuild/test_durable_execution.py", "tests/runtime_rebuild/test_run_state_machine.py"),
    "context_management": ("tests/runtime_rebuild/test_context_projection_contract.py", "tests/test_tokens.py"),
    "memory": ("tests/test_memory_service.py",),
    "tools_and_protocols": ("tests/runtime_rebuild/test_tool_contracts.py", "tests/runtime_rebuild/test_tool_observation.py"),
    "safety_and_privacy": ("tests/runtime_rebuild/test_security_gate.py", "tests/runtime_rebuild/test_permissions.py"),
    "reliability_and_recovery": ("tests/runtime_rebuild/test_action_recovery.py", "tests/runtime_rebuild/test_worker_recovery.py"),
    "human_interaction": ("tests/runtime_rebuild/test_approvals.py", "tests/runtime_rebuild/test_task_execution_control.py"),
    "operational_fitness": ("tests/test_config.py", "tests/runtime_rebuild/test_new_entrypoint.py", "tests/test_observability.py", "tests/test_observability_dataset.py", "tests/test_observability_gates.py", "tests/test_observability_boundaries.py"),
}


def quick(source: Path, output: Path) -> bool:
    missing = [name for names in QUICK_GROUPS.values() for name in names if not (source / name).is_file()]
    if missing:
        raise ValueError("quick suite contains missing targets: " + ", ".join(missing))
    identity = source_fingerprint(source)
    targets = list(dict.fromkeys(name for names in QUICK_GROUPS.values() for name in names))
    env = {**os.environ, "CC_HARNESS_OBSERVABILITY_DISABLED": "1", "CC_HARNESS_RUN_REAL_LLM": "0"}
    completed = subprocess.run([sys.executable, "-m", "pytest", *targets, "-q"], cwd=source, env=env)
    report = {"passed": completed.returncode == 0 and source_fingerprint(source) == identity,
              "groups": QUICK_GROUPS, "source_fingerprint": identity, "at": datetime.now(timezone.utc).isoformat()}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report["passed"]


def check(experiment: Path, source: Path, baseline: Path, *, require_holdout=True) -> dict:
    reasons = []
    expected = source_fingerprint(source)
    configuration = model_fingerprint(source)
    reports = {}
    for split in (("golden", "holdout") if require_holdout else ("golden",)):
        path = experiment / f"decision-{split}.json"
        if not path.is_file():
            reasons.append(f"missing_{split}_decision")
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        reports[split] = report
        decision = report["decision"]
        try:
            parsed = ReleaseDecision.model_validate_json(json.dumps(decision))
            reference = content_fingerprint(parsed)
            hexadecimal = reference.removeprefix("sha256:")
            artifact = experiment / "core" / split / report["result_snapshot"] / "objects" / hexadecimal[:2] / hexadecimal[2:]
            if reference != report.get("decision_digest") or artifact.read_bytes() != canonical_json_bytes(parsed):
                reasons.append(f"{split}_decision_integrity_error")
            manifest_hex = parsed.run_manifest_digest.removeprefix("sha256:")
            manifest_path = artifact.parents[1] / manifest_hex[:2] / manifest_hex[2:]
            manifest = EvalRunManifest.model_validate_json(manifest_path.read_bytes())
            if content_fingerprint(manifest) != parsed.run_manifest_digest:
                reasons.append(f"{split}_manifest_integrity_error")
            if manifest.subject.executable_digest != expected or manifest.subject.harness_profile_digest != configuration:
                reasons.append(f"{split}_manifest_identity_mismatch")
        except (ValueError, OSError, KeyError):
            reasons.append(f"{split}_decision_integrity_error")
        if decision["status"] != "pass" or not decision["valid"] or not decision["complete"]:
            reasons.append(f"{split}_required_checks_not_passed")
        if report["source_fingerprint"] != expected or report["model_fingerprint"] != configuration or report["dataset_version"] != VERSION:
            reasons.append(f"{split}_evidence_identity_mismatch")
    quick_path = experiment / "quick.json"
    if not quick_path.exists():
        reasons.append("missing_quick_checks")
    else:
        q = json.loads(quick_path.read_text(encoding="utf-8"))
        if not q.get("passed") or q.get("source_fingerprint") != expected:
            reasons.append("quick_checks_failed_or_stale")
    if not baseline.is_file():
        reasons.append("no_approved_baseline")
    else:
        approved = json.loads(baseline.read_text(encoding="utf-8"))
        if not approved.get("approved_by") or approved.get("dataset_version") != VERSION or approved.get("model_fingerprint") != configuration:
            reasons.append("baseline_incompatible")
        # Every deterministic task must pass: a good aggregate cannot offset a
        # critical failure, so no iterative last-run baseline can mask decay.
        if set(approved.get("decisions", {})) != {"golden", "holdout"}:
            reasons.append("baseline_incomplete")
    return {"passed": not reasons, "reasons": reasons, "source_fingerprint": expected,
            "baseline": str(baseline), "experiment": str(experiment)}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("quick", "full", "release", "normalize", "check", "approve-baseline", "publish"))
    parser.add_argument("--include-holdout", action="store_true", help="Explicit phase/release validation; never enabled by the pre-push hook")
    parser.add_argument("--root", type=Path, default=Path("D:/agent_data/cc-harness-observability/evaluation"))
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--experiment", default="release-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--reviewer")
    parser.add_argument("--command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.experiment.replace("-", "").replace("_", "").isalnum() or len(args.experiment) > 49:
        raise ValueError("invalid experiment name")
    args.root = args.root.resolve()
    if os.name == "nt" and args.root.drive.lower() != "d:":
        raise ValueError("evaluation evidence must be on D:")
    experiment = args.root / "runs" / args.experiment
    baseline = args.root / "approved-baseline.json"
    release_scope = args.action in {"release", "publish", "approve-baseline", "check"} or args.include_holdout
    if args.action in {"quick", "full", "release"}:
        if not quick(args.source, experiment / "quick.json"):
            return 1
        if args.action == "quick":
            return 0
        result = await asyncio.create_subprocess_exec(sys.executable, "-m", "eval.observability.runner", "run",
            "--root", str(args.root), "--source", str(args.source), "--experiment", args.experiment,
            "--split", "all" if release_scope else "golden")
        await result.wait()  # Failed trials are still normalized and reported.
    if args.action in {"full", "release", "normalize"}:
        for split in (("golden", "holdout") if release_scope else ("golden",)):
            await normalize(experiment, args.source, split=split)
        if args.action == "normalize":
            return 0
    if args.action == "approve-baseline":
        if not args.reviewer or not args.reviewer.strip():
            raise ValueError("baseline approval requires an explicitly named reviewer")
        if baseline.exists():
            raise ValueError("an approved baseline already exists; migrate it explicitly, never overwrite implicitly")
        candidate = check(experiment, args.source, baseline)
        if candidate["reasons"] != ["no_approved_baseline"]:
            raise ValueError("only a complete passing candidate can become a baseline: " + str(candidate["reasons"]))
        reports = {split: json.loads((experiment / f"decision-{split}.json").read_text(encoding="utf-8"))["decision_digest"] for split in ("golden", "holdout")}
        baseline.write_text(json.dumps({"approved_by": args.reviewer, "at": datetime.now(timezone.utc).isoformat(),
            "dataset_version": VERSION, "model_fingerprint": model_fingerprint(args.source),
            "source_fingerprint": source_fingerprint(args.source), "decisions": reports, "experiment": str(experiment)}, indent=2), encoding="utf-8")
        return 0
    decision = check(experiment, args.source, baseline, require_holdout=release_scope)
    experiment.mkdir(parents=True, exist_ok=True)
    (experiment / "release-gate.json").write_text(json.dumps(decision, indent=2), encoding="utf-8")
    print(json.dumps(decision))
    if args.action == "publish" and decision["passed"]:
        if not args.command:
            raise ValueError("publish requires --command followed by an explicit release command")
        return subprocess.call(args.command, cwd=args.source)
    return 0 if decision["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
