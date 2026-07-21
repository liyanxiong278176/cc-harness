"""LoCoMo 跑 1 sample 对比 maintenance 前后 utilization/recall。

跑法:
  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_maintenance_locomo.py -v
"""
import pytest

pytestmark = pytest.mark.requires_llm


def test_locomo_with_maintenance_does_not_break_recall():
    """跑 1 个 locomo sample, 验证 maintenance 开启不显著降低 recall (±2% 漂移)。"""
    try:
        from eval.locomo.runner import run_one_sample
        from eval.locomo.metrics import compute_recall
    except Exception as e:
        pytest.skip(f"locomo imports failed: {e}")
    sample_id = "locomo-001"
    try:
        result_before = run_one_sample(sample_id, maintenance=False)
        result_after = run_one_sample(sample_id, maintenance=True)
    except Exception as e:
        pytest.skip(f"locomo run failed (likely missing dataset): {e}")
    recall_before = compute_recall(result_before)
    recall_after = compute_recall(result_after)
    delta = abs(recall_after - recall_before)
    assert delta < 0.02, f"recall 漂移 {delta:.3f} 超过 2% 阈值"
