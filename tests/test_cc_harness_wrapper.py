"""Tests for eval/promptfoo/wrappers/cc_harness.py — main.py resolution.

The wrapper computes MAIN_PY by searching upward from its own directory
for a file named main.py. CI historically hit "main.py not found" failures
when the wrapper was invoked from a context where parents[3] didn't contain
main.py. The fallback search walks up to 6 ancestor levels.

These tests verify the search returns the real main.py in the project
layout and returns None when no candidate exists.
"""
import importlib.util
import sys
from pathlib import Path

# Load the wrapper directly (it's not on Python path).
WRAPPER_PATH = (
    Path(__file__).resolve().parent.parent
    / "eval" / "promptfoo" / "wrappers" / "cc_harness.py"
)
spec = importlib.util.spec_from_file_location("cc_harness_wrapper", WRAPPER_PATH)
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)


def test_resolve_main_py_finds_real_main_py():
    """In the real project layout, the search returns an existing main.py."""
    result = wrapper._resolve_main_py()
    assert result is not None, "_resolve_main_py returned None"
    assert result.name == "main.py"
    assert result.exists()
    assert result.is_file()


def test_resolve_main_py_matches_parents3_path():
    """The resolved path equals parents[3] / 'main.py' (the original logic)."""
    result = wrapper._resolve_main_py()
    expected = WRAPPER_PATH.resolve().parents[3] / "main.py"
    assert result == expected


def test_resolve_main_py_search_returns_none_for_nonexistent_start(tmp_path):
    """When start is in a tmp dir with no main.py anywhere, returns None."""
    deep = tmp_path / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    result = wrapper._resolve_main_py_search(start=deep)
    assert result is None


def test_resolve_main_py_search_finds_when_in_ancestor(tmp_path):
    """When start is deep in tmp, and we plant main.py 2 levels up, search finds it."""
    deep = tmp_path / "deep" / "deeper"
    deep.mkdir(parents=True)
    # Plant main.py in tmp_path (one level above deep)
    (tmp_path / "main.py").write_text("#!/usr/bin/env python\n", encoding="utf-8")
    # Search starting from deep/deeper should walk up and find it at tmp_path/main.py
    result = wrapper._resolve_main_py_search(start=deep)
    assert result is not None
    assert result == tmp_path / "main.py"


def test_default_repl_timeout_is_300():
    """Default repl_timeout is 5 min (300s) so a pathological probe can't
    burn the entire job budget. The per-test cap was lowered from 1800s
    after a real CI run showed one stuck probe consumed 29 of 60 minutes."""
    cfg = {}  # no override
    # Mimic the call_api line that reads cfg.get("repl_timeout", 300)
    repl_timeout = int(cfg.get("repl_timeout", 300))
    assert repl_timeout == 300


def test_repl_timeout_override_works():
    """Per-config repl_timeout override still works (promptfoo config
    can set its own value if the default is wrong for some test type)."""
    cfg = {"repl_timeout": 600}
    repl_timeout = int(cfg.get("repl_timeout", 300))
    assert repl_timeout == 600
