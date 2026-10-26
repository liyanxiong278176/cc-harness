"""Compose local regression hooks with existing hooks without replacing config."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys


def install(source: Path) -> list[str]:
    resolved = subprocess.run(["git", "rev-parse", "--git-path", "hooks"], cwd=source,
                              capture_output=True, text=True, check=True).stdout.strip()
    folder = Path(resolved)
    if not folder.is_absolute():
        folder = source / folder
    folder.mkdir(parents=True, exist_ok=True)
    installed = []
    for name, action in (("pre-commit", "quick"), ("pre-push", "full")):
        path = folder / name
        previous = folder / (name + ".cc-harness-previous")
        marker = "# cc-harness-observability hook v1"
        if path.exists() and marker not in path.read_text(encoding="utf-8", errors="replace"):
            if previous.exists():
                raise ValueError(f"existing backup prevents replacing {path}")
            path.rename(previous)
        text = "\n".join(("#!/bin/sh", marker,
            'if [ -f "$0.cc-harness-previous" ]; then',
            '  "$0.cc-harness-previous" "$@" || exit $?', "fi",
            "cd " + shlex.quote(source.as_posix()) + " || exit 1",
            "exec " + shlex.quote(Path(sys.executable).as_posix()) + " -m eval.observability.gates " + action, ""))
        path.write_text(text, encoding="utf-8", newline="\n")
        if os.name != "nt":
            path.chmod(path.stat().st_mode | 0o111)
        installed.append(str(path))
    return installed


if __name__ == "__main__":
    for hook in install(Path(__file__).resolve().parents[1]):
        print(hook)
