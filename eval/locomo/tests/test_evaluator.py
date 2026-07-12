"""Tests for eval.locomo.evaluator: token F1 + deepeval GEval wrapper."""
from eval.locomo.evaluator import token_f1, evaluate_qa


def test_token_f1_exact_match():
    assert token_f1("hello world", "hello world") == 1.0


def test_token_f1_partial():
    score = token_f1("the cat sat", "the cat sat on a mat")
    assert 0.4 < score < 0.8


def test_token_f1_no_overlap():
    assert token_f1("apple banana", "cherry date") == 0.0


def test_token_f1_empty_predicted():
    assert token_f1("", "anything") == 0.0


def test_token_f1_empty_gold():
    assert token_f1("anything", "") == 0.0


def test_token_f1_handles_cjk():
    score = token_f1("我喜欢苹果", "我喜欢苹果和香蕉")
    assert score > 0.5


async def test_evaluate_qa_returns_dict_with_expected_keys():
    result = await evaluate_qa("What color?", "blue", "blue")  # judge_llm=None 默认
    assert set(result.keys()) >= {"f1", "semantic_f1", "quality", "pass", "trace_payload"}
    assert result["f1"] == 1.0
    assert result["pass"] is True              # f1>0.5 fallback(judge_llm=None → semantic None)
    assert result["semantic_f1"] is None        # 无 judge
    assert result["quality"] is None or 0.0 <= result["quality"] <= 1.0
    assert result["trace_payload"]["f1"] == result["f1"]
    assert result["trace_payload"]["pass"] == result["pass"]


async def test_evaluate_qa_fail_when_low_f1_and_no_quality():
    result = await evaluate_qa("q", "completely wrong answer xyzzy", "the cat sat on the mat")
    assert result["f1"] < 0.3
    assert result["semantic_f1"] is None        # judge_llm=None → 无 semantic
    assert result["pass"] is False              # f1<0.5 fallback;quality 已不参与 pass(decision #1)


async def test_semantic_f1_equivalent():
    """judge 返 score=1.0 → semantic_f1 == 1.0。"""
    from eval.locomo.evaluator import semantic_f1
    async def fake_judge(s):  # _judge async-fn 形态:单 str 参数
        return '{"score": 1.0}'
    assert await semantic_f1("q", "blue", "blue", fake_judge) == 1.0


async def test_semantic_f1_partial():
    from eval.locomo.evaluator import semantic_f1
    async def fake_judge(s):
        return '{"score": 0.5}'
    assert await semantic_f1("q", "two cats", "two dogs", fake_judge) == 0.5


async def test_semantic_f1_judge_fail_soft():
    """judge 返非 JSON / raise → None。"""
    from eval.locomo.evaluator import semantic_f1
    async def bad_json(s):
        return "not json"
    async def raising(s):
        raise RuntimeError("judge down")
    assert await semantic_f1("q", "a", "b", bad_json) is None
    assert await semantic_f1("q", "a", "b", raising) is None


async def test_semantic_f1_no_llm():
    """judge_llm=None → None(退化 token_f1)。"""
    from eval.locomo.evaluator import semantic_f1
    assert await semantic_f1("q", "a", "b", None) is None


async def test_evaluate_qa_pass_semantic_main():
    """semantic>0.7 主(即使 f1 低)→ pass=True。"""
    async def fake_judge(s):
        return '{"score": 0.8}'
    # predicted 与 gold token 不重合(f1 低),但 semantic 高
    result = await evaluate_qa("q", "she visited france", "she traveled to paris",
                               judge_llm=fake_judge)
    assert result["f1"] < 0.5                    # token 不重合
    assert result["semantic_f1"] == 0.8
    assert result["pass"] is True                # semantic>0.7 主


async def test_evaluate_qa_fail_soft_token_fallback():
    """semantic=None(judge_llm=None)+ f1>0.5 → pass=True(token 兜底)。"""
    result = await evaluate_qa("q", "the cat sat", "the cat sat on a mat")  # f1 偏高
    assert result["semantic_f1"] is None
    assert result["f1"] > 0.5
    assert result["pass"] is True
