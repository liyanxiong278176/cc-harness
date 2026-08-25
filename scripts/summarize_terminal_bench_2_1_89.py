"""Combine the frozen 59-task development run and 30-task Hard holdout."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DATASET = (
    "terminal-bench/terminal-bench-2-1@"
    "sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a"
)


def _read(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _results(summary: dict[str, Any]) -> list[dict[str, Any]]:
    values = summary.get("task_results") or []
    if not isinstance(values, list):
        raise ValueError("summary.task_results must be a list")
    return [dict(value) for value in values]


def _source_dataset(summary: dict[str, Any]) -> str | None:
    """Read the immutable dataset marker across supported summary schemas."""
    dataset = summary.get("dataset")
    if isinstance(dataset, str) and dataset:
        return dataset
    check = summary.get("check")
    if isinstance(check, dict):
        details = check.get("details")
        if isinstance(details, dict):
            official_dataset = details.get("official_dataset")
            if isinstance(official_dataset, str) and official_dataset:
                return official_dataset
    return None


def build_summary(project_root: Path, development_root: Path, hard_root: Path) -> Path:
    catalog = _read(project_root / "eval/harbor/catalogs/terminal_bench_2_1.json")
    difficulty = {
        f"terminal-bench/{item['name']}": str(item["difficulty"]).title()
        for item in catalog["tasks"]
    }
    development_summary_path = development_root / "summary.json"
    hard_summary_path = hard_root / "summary.json"
    development = _read(development_summary_path)
    hard = _read(hard_summary_path)
    if _source_dataset(development) != DATASET or _source_dataset(hard) != DATASET:
        raise ValueError("source runs do not use the frozen Terminal-Bench 2.1 dataset")

    dev_results = _results(development)
    hard_results = _results(hard)
    dev_ids = {str(item["task_id"]) for item in dev_results}
    hard_ids = {str(item["task_id"]) for item in hard_results}
    expected_ids = set(difficulty)
    if len(dev_results) != 59 or len(hard_results) != 30:
        raise ValueError("expected exactly 59 development and 30 holdout results")
    if dev_ids & hard_ids or dev_ids | hard_ids != expected_ids:
        raise ValueError("development and holdout task partitions do not form the official 89")
    if any(difficulty[task_id] == "Hard" for task_id in dev_ids):
        raise ValueError("development result contains a Hard holdout task")
    if any(difficulty[task_id] != "Hard" for task_id in hard_ids):
        raise ValueError("holdout result contains a non-Hard task")
    all_results = dev_results + hard_results
    if any(str(item.get("status")) not in {"pass", "fail"} for item in all_results):
        raise ValueError("all 89 results must be terminal pass/fail outcomes")

    buckets: dict[str, dict[str, int | float]] = defaultdict(
        lambda: {"tasks": 0, "pass": 0, "fail": 0, "accuracy": 0.0}
    )
    for result in all_results:
        level = difficulty[str(result["task_id"])]
        buckets[level]["tasks"] = int(buckets[level]["tasks"]) + 1
        key = "pass" if float(result.get("reward") or 0) > 0 else "fail"
        buckets[level][key] = int(buckets[level][key]) + 1
    for bucket in buckets.values():
        bucket["accuracy"] = int(bucket["pass"]) / int(bucket["tasks"])

    passes = sum(float(result.get("reward") or 0) > 0 for result in all_results)
    output = hard_root.parent / "combined-89-development-plus-holdout"
    summary = {
        "schema_version": "terminal-bench-2.1-mixed-evidence-summary.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": DATASET,
        "official_success_rule": "reward > 0",
        "trials_per_task": 1,
        "leaderboard_compatible": False,
        "evidence_boundary": {
            "easy_medium": "development",
            "hard": "one-shot holdout",
            "overall": "mixed development + holdout; not an independent test score",
        },
        "sources": {
            "development_root": str(development_root.resolve()),
            "development_summary_sha256": _sha256(development_summary_path),
            "hard_holdout_root": str(hard_root.resolve()),
            "hard_summary_sha256": _sha256(hard_summary_path),
        },
        "counts": {"tasks": 89, "pass": passes, "fail": 89 - passes, "invalid": 0},
        "accuracy": passes / 89,
        "by_difficulty": {key: buckets[key] for key in ("Easy", "Medium", "Hard")},
    }
    _write(output / "summary.json", summary)
    lines = [
        "# Terminal-Bench 2.1: 89-task evidence summary",
        "",
        "This report combines a tuned 59-task Easy+Medium development set with a",
        "one-shot 30-task Hard holdout. The combined number is not an untouched",
        "leaderboard score and each task was run once.",
        "",
        "| Split | Role | Pass | Total | Accuracy |",
        "|---|---|---:|---:|---:|",
    ]
    for level in ("Easy", "Medium", "Hard"):
        bucket = buckets[level]
        role = "holdout" if level == "Hard" else "development"
        lines.append(
            f"| {level} | {role} | {bucket['pass']} | {bucket['tasks']} | "
            f"{float(bucket['accuracy']):.2%} |"
        )
    lines.extend(
        [
            f"| Overall | mixed evidence | {passes} | 89 | {passes / 89:.2%} |",
            "",
            f"- Development source: `{development_root.resolve()}`",
            f"- Hard holdout source: `{hard_root.resolve()}`",
            "- Official success rule: `reward > 0`",
            "- Invalid/pending: `0`",
        ]
    )
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    integrity = {
        "schema_version": "terminal-bench-2.1-combined-integrity.v1",
        "files": {
            "summary.json": _sha256(output / "summary.json"),
            "report.md": _sha256(output / "report.md"),
            "development_summary": _sha256(development_summary_path),
            "hard_summary": _sha256(hard_summary_path),
        },
    }
    _write(output / "integrity.json", integrity)
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--development-root", type=Path, required=True)
    parser.add_argument("--hard-root", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    output = build_summary(
        args.project_root.resolve(), args.development_root.resolve(), args.hard_root.resolve()
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
