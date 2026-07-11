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
    expected = {SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT, SingleTurnParams.EXPECTED_OUTPUT}
    assert set(kwargs["evaluation_params"]) == expected
    assert kwargs["model"] == "deepseek-v4-flash"


def test_quality_score_fail_soft_returns_none(monkeypatch):
    """judge 抛异常 → 返回 None(fail-soft)。"""
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-v4-flash")
    from eval.locomo.evaluator import quality_score
    with patch("eval.locomo.evaluator.GEval", side_effect=RuntimeError("boom")):
        result = quality_score("q?", "ans", "gold")
    assert result is None


def test_quality_score_fail_soft_no_deepeval(monkeypatch):
    """deepeval 未装(GEval=None)→ return None。"""
    import eval.locomo.evaluator as mod
    monkeypatch.setattr(mod, "GEval", None)
    assert mod.quality_score("q", "a", "g") is None


def test_tokenize_handles_int_answer():
    """locomo answer 可能是 int(年份 2022/次数 2);_tokenize/token_f1 不崩。"""
    from eval.locomo.evaluator import _tokenize, token_f1
    assert _tokenize(2022) == ["2022"]
    assert token_f1("2022", 2022) == 1.0   # int gold 不崩
    assert token_f1(2022, "2022") == 1.0   # int predicted 不崩
