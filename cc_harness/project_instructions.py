"""Project-instruction initialization for the wired ``/init`` command."""
from __future__ import annotations

from pathlib import Path

INSTRUCTIONS_FILENAME = "CC-HARNESS.md"

_TEMPLATE = """# CC-HARNESS.md

Project instructions for cc-harness.

## Commands

- Build: <!-- command -->
- Test: <!-- command -->
- Lint: <!-- command -->

## Conventions

- <!-- Add rules that cannot be inferred from the codebase. -->

## Boundaries

- <!-- Add paths, services, or operations that require extra care. -->
"""


def initialize_project_instructions(cwd: Path) -> tuple[Path, bool]:
    path = Path(cwd) / INSTRUCTIONS_FILENAME
    if path.exists():
        return path, False
    path.write_text(_TEMPLATE, encoding="utf-8")
    return path, True
