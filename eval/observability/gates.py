"""Fixed Agent regression and evidence-bound release wrapper."""
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
    parser.add_argument("action", choices=("full", "release", "normalize", "check", "approve-baseline", "publish"))
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
    if args.action in {"full", "release"}:
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
