"""Tests for Pass^k 聚合 + Wilson 95% CI (Task 6).

critical-severity attack 跑 5 次,report 用 Wilson score CI 给区间
(比 normal approx 在小样本更稳)。`aggregate_repeats` 按 testCase.id
聚合同 id 的 N 个 result 为 {hold, n},供 report 顶层加"critical 采样 ×5"段。

Loaded via importlib(tools dir is not on sys.path in the test runner)。
"""
import importlib.util
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "report_to_md.py"
_SPEC = importlib.util.spec_from_file_location("report_to_md", TOOL)
rtm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rtm)


def test_wilson_ci_small_sample():
    # 5 次 3 hold(0.6)——Wilson 比 normal approx 更稳
    lo, hi = rtm.wilson_ci(3, 5)
    assert 0.0 < lo < 0.6 < hi < 1.0
    assert hi - lo > 0.3   # n=5 区间宽,诚实呈现


def test_wilson_ci_extremes():
    assert rtm.wilson_ci(0, 5)[0] == 0.0      # 全 broke
    assert rtm.wilson_ci(5, 5)[1] == 1.0      # 全 hold


def test_aggregate_repeats_groups_by_testid():
    """同 testId 的 N 个 result → 聚合成 {testId: {hold, n}}。"""
    results = [
        {"testCase": {"id": "crit-1"}, "success": True},
        {"testCase": {"id": "crit-1"}, "success": False},
        {"testCase": {"id": "crit-1"}, "success": True},
        {"testCase": {"id": "crit-1"}, "success": True},
        {"testCase": {"id": "crit-1"}, "success": True},   # 4/5 hold
    ]
    agg = rtm.aggregate_repeats(results)
    assert agg["crit-1"] == {"hold": 4, "n": 5}