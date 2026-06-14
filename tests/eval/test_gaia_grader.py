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
