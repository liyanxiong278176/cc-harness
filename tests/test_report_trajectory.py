"""Tests for trajectory 指标抽取 (Task 4) — extract_trajectory_from_output
从 wrapper 塞的 '--- trajectory ---' 段解析步数/工具错误/borderline。
Loaded via importlib(tools dir is not on sys.path in the test runner)。"""
import importlib.util
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "eval" / "promptfoo" / "tools" / "report_to_md.py"
_spec = importlib.util.spec_from_file_location("report_to_md", TOOL)
rtm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rtm)


def test_extract_trajectory_from_output_present():
    out = ("我无法执行\n--- trajectory ---\n"
           "步数=1 工具错误=1 borderline=True\n思考: 我来执行 rm -rf")
    t = rtm.extract_trajectory_from_output(out)
    assert t == {"steps": 1, "tool_errors": 1, "borderline": True}


def test_extract_trajectory_from_output_absent():
    assert rtm.extract_trajectory_from_output("普通响应,无 trajectory") == {
        "steps": 0, "tool_errors": 0, "borderline": False}
