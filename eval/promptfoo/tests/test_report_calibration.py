"""Tests for report 校准段 + 回归段 (Task 11 of eval-v2)。

`render_calibration_section(kappas)` 出每维 κ 值,κ<0.7 标「⚠ judge 需校准」。
`render_regression_section(reg)` 出 new_breaks / fixed 计数 + ids。
两者皆为纯函数,test 直接验输出 markdown 文本。

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

from report_to_md import render_calibration_section, render_regression_section  # noqa: E402


def test_render_calibration_section():
    """3 维 κ,κ<0.7 标警告。"""
    kappas = {"hold_broke": 0.85, "borderline": 0.6, "leak_type": 0.4}
    out = render_calibration_section(kappas)
    assert "校准(Cohen's κ)" in out
    # 3 个维度名 + κ 值
    assert "hold_broke" in out and "0.85" in out
    assert "borderline" in out and "0.60" in out
    assert "leak_type" in out and "0.40" in out
    # borderline + leak_type 应有警告
    borderline_line = [line for line in out.splitlines() if "borderline" in line][0]
    leak_type_line = [line for line in out.splitlines() if "leak_type" in line][0]
    assert "⚠ judge 需校准(κ<0.7)" in borderline_line
    assert "⚠ judge 需校准(κ<0.7)" in leak_type_line
    # hold_broke (0.85) 不应有警告
    hold_line = [line for line in out.splitlines() if "hold_broke" in line][0]
    assert "⚠" not in hold_line


def test_render_regression_section():
    """new_breaks / fixed 计数 + ids。"""
    reg = {"new_breaks": ["crit-1", "crit-2"], "fixed": ["crit-3"]}
    out = render_regression_section(reg)
    assert "回归" in out
    assert "new_breaks" in out and "2 条" in out
    assert "fixed" in out and "1 条" in out
    assert "crit-1" in out and "crit-2" in out and "crit-3" in out
