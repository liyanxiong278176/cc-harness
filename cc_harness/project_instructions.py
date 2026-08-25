"""Project-instruction initialization for the wired ``/init`` command."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

INSTRUCTIONS_FILENAME = "CC-HARNESS.md"
_CANDIDATE_FILENAMES = ("AGENTS.md", "CC-HARNESS.md", ".cc-harness/instructions.md")
_MAX_INSTRUCTION_CHARS = 16_000

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


@dataclass(frozen=True)
class ProjectInstructionLayer:
    """Bounded project-scoped dynamic instructions.

    ``text`` is used only to construct the model request.  Telemetry should
    retain the digest and source count, never the content itself.
    """

    text: str
    source_files: tuple[str, ...]
    digest: str

    def public_metadata(self) -> dict[str, object]:
        return {
            "source_count": len(self.source_files),
            "digest": self.digest,
            "chars": len(self.text),
        }


def initialize_project_instructions(cwd: Path) -> tuple[Path, bool]:
    path = Path(cwd) / INSTRUCTIONS_FILENAME
    if path.exists():
        return path, False
    path.write_text(_TEMPLATE, encoding="utf-8")
    return path, True


def load_project_instructions(cwd: Path) -> ProjectInstructionLayer | None:
    """Load project instruction files in stable priority order.

    Files are deliberately kept separate from the shared prompt core.  The
    loader is local-only, bounded, and fail-soft; a malformed/unreadable file
    is skipped instead of changing the core safety rules.
    """
    root = Path(cwd).resolve()
    chunks: list[str] = []
    sources: list[str] = []
    remaining = _MAX_INSTRUCTION_CHARS
    for relative in _CANDIDATE_FILENAMES:
        path = root / relative
        if not path.is_file() or remaining <= 0:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        # The generated template is a placeholder, not useful project policy.
        if path.name == INSTRUCTIONS_FILENAME and text.strip() == _TEMPLATE.strip():
            continue
        text = text.strip()
        if not text:
            continue
        bounded = text[:remaining]
        chunks.append(bounded)
        sources.append(relative.replace("\\", "/"))
        remaining -= len(bounded)
    if not chunks:
        return None
    joined = "\n\n".join(chunks)
    return ProjectInstructionLayer(
        text=joined,
        source_files=tuple(sources),
        digest=hashlib.sha256(joined.encode("utf-8")).hexdigest(),
    )


__all__ = [
    "INSTRUCTIONS_FILENAME",
    "ProjectInstructionLayer",
    "initialize_project_instructions",
    "load_project_instructions",
]
