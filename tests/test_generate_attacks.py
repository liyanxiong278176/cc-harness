"""Tests for tools/generate_attacks.py"""
from eval.promptfoo.tools import generate_attacks


def test_categories_has_all_five_keys():
    assert set(generate_attacks.CATEGORIES.keys()) == {
        "shell-injection",
        "prompt-extraction",
        "excessive-agency",
        "hijacking",
        "sql-injection",
    }


def test_categories_values_are_nonempty_strings():
    for cat, desc in generate_attacks.CATEGORIES.items():
        assert isinstance(desc, str)
        assert len(desc) > 20, f"{cat} description too short"