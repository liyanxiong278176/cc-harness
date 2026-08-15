"""Regrade archived LoCoMo predictions without creating a formal score."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from eval.cc_only.adapters.memory import _grade_locomo_answer
from eval.cc_only.storage import atomic_json, atomic_text, utc_now


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive_root", type=Path)
    args = parser.parse_args()
    root = args.archive_root.resolve()
    if not (root / "manifest.json").is_file():
        raise SystemExit(f"not a LoCoMo result archive: {root}")

    records: list[dict[str, Any]] = []
    for path in sorted((root / "raw").glob("**/qa/[0-9][0-9][0-9][0-9].json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        grading_input = {**item, "answer": item.get("gold")}
        grading = _grade_locomo_answer(grading_input, str(item.get("prediction") or ""))
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sample_id": item.get("sample_id"),
                "question_index": item.get("question_index"),
                "category": str(item.get("category")),
                "old_score": item.get("f1"),
                "regraded_score": grading["score"],
                "grading": grading,
            }
        )

    by_category: dict[str, list[float]] = defaultdict(list)
    for item in records:
        by_category[item["category"]].append(float(item["regraded_score"]))
    report = {
        "schema_version": "eval.locomo-regraded-diagnostic.v1",
        "classification": "diagnostic-only-not-a-formal-benchmark-score",
        "source_archive": str(root),
        "generated_at": utc_now(),
        "question_count": len(records),
        "diagnostic_mean": (
            sum(float(item["regraded_score"]) for item in records) / len(records)
            if records
            else None
        ),
        "category_scores": {
            category: {"count": len(scores), "mean": sum(scores) / len(scores)}
            for category, scores in sorted(by_category.items())
        },
        "records": records,
    }
    json_path = root / "regraded-diagnostic.json"
    markdown_path = root / "regraded-diagnostic.md"
    atomic_json(json_path, report)
    lines = [
        "# LoCoMo Regraded Diagnostic Report",
        "",
        "> Diagnostic only. This is not a new formal benchmark score and does not alter the archived evidence.",
        "",
        f"- Questions regraded: {len(records)}",
        f"- Diagnostic mean: {report['diagnostic_mean']}",
        "",
        "## Category scores",
        "",
        "```json",
        json.dumps(report["category_scores"], ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    atomic_text(markdown_path, "\n".join(lines))
    print(f"json={json_path}")
    print(f"report={markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
