"""真 deepeval + deepseek judge 集成测试。手动跑:
   .venv/Scripts/python.exe -m pytest tests/_test_evaluator_integration.py -v
   需 .env 的 OPENAI_API_KEY/BASE_URL/MODEL。"""
import os
from dotenv import dotenv_values


def test_quality_score_real_judge():
    e = {k: v for k, v in dotenv_values(".env").items() if v}
    for k, v in e.items():
        os.environ.setdefault(k, v)
    from eval.locomo.evaluator import quality_score
    score = quality_score("Alice 的Favorite color?", "Blue", "Green")
    assert score is not None
    assert 0.0 <= score <= 1.0
    # Blue vs Green 矛盾 → 低分
    assert score < 0.4
