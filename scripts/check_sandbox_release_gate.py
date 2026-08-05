"""Evaluate collected sandbox conformance reports for release eligibility."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from cc_harness.sandbox_evidence import control_bundle_digest
from cc_harness.sandbox_release_gate import evaluate_release_gate

ROOT = Path(__file__).resolve().parents[1]


def _reports(path: Path) -> list[dict]:
    reports = []
    for report_path in path.rglob("report.json"):
        try:
            reports.append(json.loads(report_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return reports


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=ROOT / "eval" / "result" / "sandbox-conformance",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--minimum-consecutive-runs", type=int, default=2)
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    result = evaluate_release_gate(
        _reports(args.evidence_dir),
        target_commit=commit,
        target_control_digest=control_bundle_digest(ROOT),
        minimum_consecutive_runs=args.minimum_consecutive_runs,
        max_age_days=args.max_age_days,
    )
    output = args.output or (args.evidence_dir / "release-gate.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(output)
    return 0 if result["eligible"] or args.report_only else 1


if __name__ == "__main__":
    raise SystemExit(main())
