"""Build the no-model-call cc-harness domain evaluation report."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.cc_only.storage import atomic_json, atomic_text, digest_file

MODEL = "deepseek-v4-flash"
DOMAINS = {
    "context-memory": ("context-memory",),
    "safety": ("safety8", "agentdojo-v1.2.2", "agentharm-public-test"),
    "overall-agent-ability": ("terminal-bench-2.1", "swe-bench-verified"),
}


def main() -> int:
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--project-root", type=Path, default=Path.cwd())
    args.add_argument("--profile", choices=("portfolio", "full"), default="portfolio")
    args.add_argument("--check", action="store_true")
    parsed = args.parse_args()
    root = parsed.project_root.resolve()
    result_root = root / "eval" / "result" / "cc-only"
    output = result_root / "final-report" / MODEL / ("check" if parsed.check else parsed.profile)
    records = []
    for domain, benchmarks in DOMAINS.items():
        for benchmark in benchmarks:
            summary_path = (
                result_root
                / "context-memory"
                / MODEL
                / parsed.profile
                / "aggregate"
                / "summary.json"
                if benchmark == "context-memory"
                else result_root / benchmark / MODEL / parsed.profile / "summary.json"
            )
            summary = _read(summary_path)
            records.append(
                {
                    "domain": domain,
                    "benchmark": benchmark,
                    "summary_path": str(summary_path),
                    "available": summary is not None,
                    "summary_digest": digest_file(summary_path) if summary is not None else None,
                    "status": summary.get("status") if summary else "missing",
                    "metrics": summary.get("benchmark_metrics") if summary else None,
                    "adaptations": summary.get("adaptations") if summary else [],
                }
            )
    domain_status = {}
    for domain in DOMAINS:
        selected = [record for record in records if record["domain"] == domain]
        if any(record["status"] == "critical-regression" for record in selected):
            domain_status[domain] = "critical-regression"
        elif all(record["status"] == "complete" for record in selected):
            domain_status[domain] = "complete"
        else:
            domain_status[domain] = "incomplete"
    summary = {
        "schema_version": "eval.cc-only-domain-summary.v2",
        "model": MODEL,
        "profile": parsed.profile,
        "generated_at": datetime.now(UTC).isoformat(),
        "domains": domain_status,
        "overall_score": None,
        "percentage_of_claude_code": None,
        "status": (
            "critical-regression"
            if "critical-regression" in domain_status.values()
            else "complete"
            if all(value == "complete" for value in domain_status.values())
            else "incomplete"
        ),
    }
    output.mkdir(parents=True, exist_ok=True)
    index_path = output / "benchmark-index.json"
    summary_path = output / "summary.json"
    report_path = output / "report.md"
    references_path = output / "external-references.md"
    atomic_json(
        index_path, {"schema_version": "eval.cc-only-benchmark-index.v1", "benchmarks": records}
    )
    atomic_json(summary_path, summary)
    atomic_text(report_path, _report(summary, records))
    atomic_text(
        references_path,
        "# External Claude Code References\n\n"
        "No external reference is treated as locally executed evidence. Add only source URL, "
        "retrieval date, model, benchmark version, protocol and sample-count qualified results.\n",
    )
    integrity_path = output / "integrity.json"
    atomic_json(
        integrity_path,
        {
            "schema_version": "eval.cc-only-final-integrity.v1",
            "files": [
                {"path": path.name, "sha256": digest_file(path), "size_bytes": path.stat().st_size}
                for path in (index_path, summary_path, report_path, references_path)
            ],
        },
    )
    print("model_calls=0")
    for name, path in (
        ("summary", summary_path),
        ("report", report_path),
        ("integrity", integrity_path),
        ("benchmark_index", index_path),
        ("external_references", references_path),
    ):
        print(f"{name}={path}")
    return 0 if parsed.check or summary["status"] == "complete" else 2


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _report(summary: dict[str, Any], records: list[dict[str, Any]]) -> str:
    lines = [
        "# cc-harness Domain Evaluation",
        "",
        f"Status: `{summary['status']}`",
        "",
        "No overall weighted score or Claude Code capability percentage is calculated.",
        "",
    ]
    for domain, status in summary["domains"].items():
        lines.extend((f"## {domain.replace('-', ' ').title()}", "", f"Status: `{status}`", ""))
        for record in records:
            if record["domain"] == domain:
                lines.append(f"- `{record['benchmark']}`: {record['status']}")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
