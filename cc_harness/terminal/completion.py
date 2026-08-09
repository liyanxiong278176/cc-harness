"""prompt_toolkit completion for slash commands and project paths."""
from __future__ import annotations

import os
from pathlib import Path

from prompt_toolkit.completion import Completer, Completion

from cc_harness.terminal.commands import COMMANDS

_IGNORED_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".idea"}


class TerminalCompleter(Completer):
    def __init__(self, cwd: Path) -> None:
        self.cwd = Path(cwd)

    def get_completions(self, document, complete_event):
        del complete_event
        before = document.text_before_cursor
        word = document.get_word_before_cursor(WORD=True)
        if before.lstrip().startswith("/") and " " not in before.lstrip():
            prefix = before.lstrip().lower()
            for command in COMMANDS:
                if command.name.startswith(prefix):
                    yield Completion(command.name, start_position=-len(prefix))
            return
        at = word.rfind("@")
        if at < 0:
            return
        prefix = word[at + 1:].strip('"\'')
        target = self.cwd / prefix
        parent = target if target.is_dir() and prefix.endswith(("/", "\\")) else target.parent
        partial = "" if parent == target else target.name
        try:
            children = sorted(parent.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for child in children:
            if child.name in _IGNORED_DIRS or not child.name.lower().startswith(partial.lower()):
                continue
            relative_parent = parent.relative_to(self.cwd)
            rel = (relative_parent / child.name) if str(relative_parent) != "." else Path(child.name)
            value = "@" + str(rel)
            if child.is_dir():
                value += os.sep
            if " " in value:
                value = '@"' + value[1:] + ('"' if not child.is_dir() else '')
            yield Completion(value, start_position=-(len(word) - at), display=value)
