"""Tests for 双 judge 一致率 + 分歧(Δ>0.3)helpers(Task 8)。

extract_judge_scores 从 gradingResult.componentResults 抽所有 llm-rubric
的 score(primary + 第二源 MiMo,顺序按 componentResults 出现序)。
judge_agreement 收 paired (score_a, score_b) 列,算一致率 + 分歧数;
Δ>threshold → 分歧。空 list → n=0,默认 0% agreement(避免除零)。

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

from report_to_md import extract_judge_scores, judge_agreement  # noqa: E402


def test_extract_two_scores():
    gr = {"componentResults": [
        {"score": 1.0, "assertion": {"type": "llm-rubric"}},
        {"score": 0.0, "assertion": {"type": "llm-rubric"}},
    ]}
    scores = extract_judge_scores({"gradingResult": gr})
    assert scores == [1.0, 0.0]


def test_judge_agreement_stats():
    rows = [(1.0, 1.0), (0.0, 0.0), (1.0, 0.0)]  # 2 一致 1 分歧
    stats = judge_agreement(rows)
    assert stats["agree_pct"] > 0.6
    assert stats["disagreements"] == 1