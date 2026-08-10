"""Aggregate four independent context-memory reports without a cross-benchmark score."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.cc_only.storage import atomic_json, atomic_text, digest_file

from .contracts import MODEL, EvalProfile

BENCHMARKS = (
    "longmemeval-s-cleaned",
    "longmemeval-v2-small",
    "locomo",
    "memoryagentbench",
)


def aggregate_reports(
    project_root: Path, *, profile: EvalProfile, check_only: bool = False
) -> dict[str, Path]:
    base = project_root.resolve() / "eval" / "result" / "cc-only" / "context-memory" / MODEL
    source_base = base / "check" / profile.value if check_only else base / profile.value
    records = []
    for benchmark in BENCHMARKS:
        path = source_base / benchmark / "summary.json"
        summary = _read(path)
        records.append(
            {
                "benchmark": benchmark,
                "summary_path": str(path),
                "available": summary is not None,
                "summary_digest": digest_file(path) if summary is not None else None,
                "status": summary.get("status") if summary else "missing",
                "benchmark_metrics": summary.get("benchmark_metrics") if summary else None,
                "mechanism_verdict": summary.get("mechanism_verdict") if summary else None,
                "adaptations": summary.get("adaptations") if summary else [],
            }
        )
    acceptable = {"ready"} if check_only else {"complete"}
    status = (
        "complete" if all(record["status"] in acceptable for record in records) else "incomplete"
    )
    summary = {
        "schema_version": "eval.context-memory-aggregate.v1",
        "model": MODEL,
        "profile": profile.value,
        "check_only": check_only,
        "status": status,
        "benchmarks": records,
        "overall_score": None,
        "cross_benchmark_weighted_score": None,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    output = source_base / "aggregate"
    output.mkdir(parents=True, exist_ok=True)
    summary_path = output / "summary.json"
    report_path = output / "report.md"
    atomic_json(summary_path, summary)
    atomic_text(report_path, _report(summary))
    integrity_path = output / "integrity.json"
    atomic_json(
        integrity_path,
        {
            "schema_version": "eval.context-memory-aggregate-integrity.v1",
            "files": [
                {
                    "path": path.name,
                    "sha256": digest_file(path),
                    "size_bytes": path.stat().st_size,
                }
                for path in (summary_path, report_path)
            ],
        },
    )
    return {"summary": summary_path, "report": report_path, "integrity": integrity_path}


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _report(summary: dict[str, Any]) -> str:
    lines = [
        "# Context-Memory Engineering Evaluation",
        "",
        f"- Status: `{summary['status']}`",
        f"- Model: `{summary['model']}`",
        f"- Profile: `{summary['profile']}`",
        "- Cross-benchmark weighted score: not calculated",
        "",
        (
            "Only an untrimmed `full` profile may be presented as an external benchmark result. "
            "DeepSeek reader/judge substitutions remain explicitly adapted results."
        ),
        "",
        "## Benchmarks",
        "",
    ]
    for record in summary["benchmarks"]:
        lines.append(
            f"- `{record['benchmark']}`: {record['status']}; mechanism="
            f"{record['mechanism_verdict'] or 'unavailable'}"
        )
    lines.append("")
    return "\n".join(lines)
