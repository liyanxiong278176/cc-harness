"""Tests for eval.reports.csv_report (Task 5.2)."""
import csv
from eval.metrics.schema import ComparisonReport
from eval.reports.csv_report import write_csv_report
from tests.eval.test_markdown_report import _sm


def test_csv_has_expected_columns(tmp_path):
    cmp = ComparisonReport(
        master=_sm(branch="master"),
        cc=_sm(branch="context-compaction"),
        accuracy_delta=0.0, peak_ratio_delta=0.0,
        api_tokens_delta=0, api_tokens_delta_pct=0.0,
        overflow_delta=0,
        per_task_diffs=[
            {"task_id": "t1", "level": 1, "master_correct": True,
             "cc_correct": False, "master_peak": 100, "cc_peak": 200,
             "master_failed": False, "cc_failed": False,
             "master_api_tokens": 300, "cc_api_tokens": 250},
        ],
    )
    p = tmp_path / "report.csv"
    write_csv_report(cmp, p)
    rows = list(csv.DictReader(p.open()))
    assert len(rows) == 1
    assert rows[0]["task_id"] == "t1"
    assert rows[0]["master_correct"] == "True"
    assert rows[0]["cc_correct"] == "False"
