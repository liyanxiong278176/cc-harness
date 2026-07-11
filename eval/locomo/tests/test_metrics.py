"""metrics.py 纯聚合单测(无 LLM)。用 fixture results。"""
import pytest

FIXTURE = [  # 3 条 result,2 类 q_type
    {"q_type": "single-hop", "f1": 0.8, "quality": 0.9, "pass": True,
     "prompt_tokens": 50000, "completion_tokens": 100, "cost_usd": 0.01,
     "tool_calls": [{"name": "memory_recall", "args": {"query": "q"}, "ok": True, "result": "找到 1 条"}],
     "compaction": None, "turn_idx": -1, "sample_id": "conv-1"},
    {"q_type": "multi-hop", "f1": 0.2, "quality": 0.3, "pass": False,
     "prompt_tokens": 60000, "completion_tokens": 200, "cost_usd": 0.02,
     "tool_calls": [], "compaction": None, "turn_idx": -1, "sample_id": "conv-1"},
    {"q_type": "single-hop", "f1": 0.6, "quality": None, "pass": False,
     "prompt_tokens": 70000, "completion_tokens": 150, "cost_usd": 0.01,
     "tool_calls": [], "compaction": {"tier": 2, "before_tokens": 180000, "after_tokens": 150000,
                                      "ratio_before": 0.18, "ratio_after": 0.15},
     "turn_idx": -1, "sample_id": "conv-1"},
]


def test_compute_by_q_type():
    from eval.locomo.metrics import compute_by_q_type
    out = compute_by_q_type(FIXTURE)
    assert "single-hop" in out and "multi-hop" in out
    sh = out["single-hop"]
    assert sh["n"] == 2
    assert sh["pass"] == 1  # 1/2 pass


def test_compute_compaction():
    from eval.locomo.metrics import compute_compaction
    out = compute_compaction(FIXTURE)
    assert out["triggered"] == 1  # 1 条有 compaction tier>0
    assert out["by_tier"][2] == 1  # tier2 一次


def test_compute_context_utilization():
    """利用率 = prompt_tokens / 1M。"""
    from eval.locomo.metrics import compute_context_utilization
    out = compute_context_utilization(FIXTURE, context_window=1_000_000)
    assert out["peak"] == pytest.approx(70000 / 1_000_000)
    assert out["avg"] > 0


def test_compute_token_series():
    from eval.locomo.metrics import compute_token_series
    out = compute_token_series(FIXTURE)
    assert out["prompt"] == [50000, 60000, 70000]
    assert out["cumulative_cost"] == pytest.approx(0.04)
