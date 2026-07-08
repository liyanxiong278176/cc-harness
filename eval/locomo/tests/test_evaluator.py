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


def test_evaluate_qa_returns_dict_with_expected_keys():
    result = evaluate_qa("What color?", "blue", "blue")
    assert set(result.keys()) >= {"f1", "quality", "pass", "trace_payload"}
    assert result["f1"] == 1.0
    assert result["pass"] is True
    # quality may be None if deepeval judge LLM not available — that's fail-soft
    assert result["quality"] is None or 0.0 <= result["quality"] <= 1.0
    assert result["trace_payload"]["f1"] == result["f1"]
    assert result["trace_payload"]["pass"] == result["pass"]


def test_evaluate_qa_fail_when_low_f1_and_no_quality():
    result = evaluate_qa("q", "completely wrong answer xyzzy", "the cat sat on the mat")
    assert result["f1"] < 0.3
    if result["quality"] is None:
        assert result["pass"] is False
