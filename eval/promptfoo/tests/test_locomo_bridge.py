"""Tests for locomo 桥接段 (Task 13 of eval-v2)。

`render_locomo_section(metrics, html_link)` 出 5-key 摘要 + HTML 链接。
uncomputed key → '-'。float 字段以 .3f 格式化。

Loaded via importlib + sys.modules 注册,让 brief 的
`from report_to_md import ...` 形式工作(tools dir 不在 sys.path)。
"""
import importlib.util
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "report_to_md.py"
_SPEC = importlib.util.spec_from_file_location("report_to_md", TOOL)
rtm = importlib.util.module_from_spec(_SPEC)
sys.modules["report_to_md"] = rtm  # 让 from report_to_md import ... 工作
_SPEC.loader.exec_module(rtm)

from report_to_md import render_locomo_section  # noqa: E402


def test_render_locomo_section_shows_5keys():
    """5-key 段(recall/timeliness/utilization/compaction/consistency)+ HTML 链接。
    float 字段以 .3f 格式(recall=0.7 → '0.700',drift_rate=0.25 → '0.250')。
    """
    metrics = {
        "1_recall": {"n_eligible": 40, "precision": 0.8, "recall": 0.7},
        "2_timeliness": {"n": 10, "pass_rate": 0.6},
        "3_utilization": {"avg": 0.3, "p50": 0.25, "p90": 0.6},
        "4_compaction": {"total_compressed_n": 5, "overall_avg_retain": 0.4},
        "5_consistency": {"n_groups": 8, "drift_rate": 0.25},
    }
    out = render_locomo_section(metrics, html_link="locomo-report.html")
    # 段头
    assert "记忆能力" in out
    # 5 个 key 段(每个 key 名都在输出中)
    assert "recall" in out.lower()
    assert "时效性" in out
    assert "利用率" in out
    assert "压缩" in out
    assert "一致性" in out
    # float 字段格式化到 3 位小数(brief 用 .3f)
    assert "0.700" in out  # recall
    assert "0.250" in out  # drift_rate
    # HTML 链接
    assert "locomo-report.html" in out
    assert "drift_rate" in out
