"""evaluator quality_score 单测。mock deepeval GEval,验证构造参数 + fail-soft。"""
from unittest.mock import patch, MagicMock


def test_quality_score_returns_float(monkeypatch):
    """quality_score 成功时返回 float(0-1)。"""
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-v4-flash")
    from eval.locomo.evaluator import quality_score
    with patch("eval.locomo.evaluator.GEval") as MockGEval:
        mock_metric = MagicMock()
        mock_metric.score = 0.75
        MockGEval.return_value = mock_metric
        result = quality_score("q?", "ans", "gold")
    assert result == 0.75
    assert isinstance(result, float)


def test_quality_score_passes_required_params(monkeypatch):
    """GEval 必须收到 evaluation_params(枚举列表)+ model。"""
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-v4-flash")
    from eval.locomo.evaluator import quality_score
    from deepeval.test_case.llm_test_case import SingleTurnParams
    with patch("eval.locomo.evaluator.GEval") as MockGEval:
        MockGEval.return_value = MagicMock(score=0.5)
        quality_score("q?", "ans", "gold")
    kwargs = MockGEval.call_args.kwargs
    assert "evaluation_params" in kwargs
    assert all(isinstance(p, SingleTurnParams) for p in kwargs["evaluation_params"])
    assert kwargs["model"] == "deepseek-v4-flash"


def test_quality_score_fail_soft_returns_none(monkeypatch):
    """judge 抛异常 → 返回 None(fail-soft)。"""
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-v4-flash")
    from eval.locomo.evaluator import quality_score
    with patch("eval.locomo.evaluator.GEval", side_effect=RuntimeError("boom")):
        result = quality_score("q?", "ans", "gold")
    assert result is None
