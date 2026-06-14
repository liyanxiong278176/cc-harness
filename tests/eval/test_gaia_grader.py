"""Tests for eval.grading.gaia_grader (port of GAIA's official scoring)."""
from eval.grading.gaia_grader import _normalize_str


def test_normalize_strips_articles_punctuation_lower():
    assert _normalize_str("The Eiffel Tower.") == "eiffel tower"
    assert _normalize_str("A cat") == "cat"
    assert _normalize_str("An apple") == "apple"


def test_normalize_collapses_whitespace():
    assert _normalize_str("hello   world\n") == "hello world"


def test_normalize_handles_empty():
    assert _normalize_str("") == ""
    assert _normalize_str("   ") == ""


def test_scorer_exact_string():
    from eval.grading.gaia_grader import question_scorer
    assert question_scorer("Paris", "Paris") is True
    assert question_scorer("the Eiffel Tower", "Eiffel Tower") is True
    assert question_scorer("London", "Paris") is False


def test_scorer_number_with_tolerance():
    from eval.grading.gaia_grader import question_scorer
    assert question_scorer("42", "42") is True
    assert question_scorer("42.0", "42") is True
    assert question_scorer("41.999", "42") is True   # within 0.01 rel tol
    assert question_scorer("$1,234.56", "1234.56") is True  # strip currency/commas
    assert question_scorer("100", "200") is False


def test_scorer_robust_to_whitespace():
    from eval.grading.gaia_grader import question_scorer
    assert question_scorer("  Paris  \n", "Paris") is True


def test_scorer_list_order_insensitive():
    from eval.grading.gaia_grader import question_scorer
    assert question_scorer("Paris, London", "London, Paris") is True
    assert question_scorer("Paris", "London, Paris") is False  # missing
    assert question_scorer("Paris, London, Berlin", "London, Paris") is False  # extra


def test_scorer_list_with_normalization():
    from eval.grading.gaia_grader import question_scorer
    assert question_scorer("the Apple, an orange", "apple, orange") is True


def test_extract_explicit_final_answer_marker():
    from eval.grading.gaia_grader import extract_final_answer
    text = "Let me think.\n\nFINAL ANSWER: 42"
    assert extract_final_answer(text) == "42"


def test_extract_case_insensitive_marker():
    from eval.grading.gaia_grader import extract_final_answer
    assert extract_final_answer("final answer: paris") == "paris"
    assert extract_final_answer("Final Answer:  Paris  \n") == "Paris"


def test_extract_fallback_to_last_paragraph():
    from eval.grading.gaia_grader import extract_final_answer
    text = "Step 1: ...\n\nStep 2: ...\n\nThe answer is 42."
    assert extract_final_answer(text) == "The answer is 42."


def test_extract_empty_or_whitespace():
    from eval.grading.gaia_grader import extract_final_answer
    assert extract_final_answer("") == ""
    assert extract_final_answer("   ") == ""


