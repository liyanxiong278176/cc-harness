"""Emit per-task comparison rows as CSV for spreadsheet pivot work."""
from __future__ import annotations
import csv
from pathlib import Path
from eval.metrics.schema import ComparisonReport


def write_csv_report(cmp: ComparisonReport, path: Path) -> None:
    if not cmp.per_task_diffs:
        path.write_text("", encoding="utf-8")
        return
    keys = list(cmp.per_task_diffs[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for row in cmp.per_task_diffs:
            w.writerow(row)
