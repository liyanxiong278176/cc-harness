"""Tests for cc_harness.tui.history — input history + Ctrl+R reverse search."""
from cc_harness.tui.history import History


def test_history_starts_empty():
    h = History()
    assert h.entries == []


def test_history_append():
    h = History()
    h.append("hello")
    h.append("world")
    assert h.entries == ["hello", "world"]


def test_history_search_substring():
    h = History()
    h.append("git status")
    h.append("git log")
    h.append("pytest")
    matches = h.search("git")
    assert matches == ["git log", "git status"]  # latest first


def test_history_no_match_returns_empty():
    h = History()
    h.append("hello")
    assert h.search("xyz") == []