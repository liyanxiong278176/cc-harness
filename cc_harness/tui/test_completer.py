"""Tests for cc_harness.tui.completer — Tab completion for /commands and @paths."""
from pathlib import Path

from cc_harness.tui.completer import Completer


def test_completer_slash_commands():
    c = Completer(cwd=str(Path.cwd()))
    matches = c.complete("/the")
    # 应包含 /theme
    assert any(m.startswith("/theme") for m in matches)


def test_completer_at_path():
    c = Completer(cwd=str(Path.cwd()))
    matches = c.complete("@CLAUDE")
    # 仓库根有 CLAUDE.md,应匹配
    assert any(m.endswith("CLAUDE.md") for m in matches)


def test_completer_no_match_returns_empty():
    c = Completer(cwd=str(Path.cwd()))
    assert c.complete("/xyz_no_match") == []
    assert c.complete("@xyz_no_match") == []