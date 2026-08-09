"""User-visible cc-harness release notes used by the terminal shell."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Release:
    version: str
    items: tuple[str, ...]


RELEASES = (
    Release(
        "0.1.0",
        (
            "Added Claude Code-style classic inline terminal shell",
            "Added project-scoped resumable sessions and bounded attachments",
            "Added layered configuration, permission modes, and MCP status",
        ),
    ),
)


def recent_items(limit: int = 3) -> list[str]:
    return [item for release in RELEASES for item in release.items][:limit]
