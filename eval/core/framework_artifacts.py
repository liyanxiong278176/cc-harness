"""Safe helpers for importing third-party evaluator artifacts."""

from __future__ import annotations

from pathlib import Path

from .adapters import ArtifactRepository
from .models import ArtifactRef


class FrameworkArtifactError(ValueError):
    """Raised when an imported framework artifact is unsafe or incomplete."""


def resolve_workspace_file(workspace: Path, relative_path: str) -> Path:
    """Resolve a regular file while keeping imports inside the disposable workspace."""
    normalized = relative_path.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith(("/", "../"))
        or "/../" in f"/{normalized}/"
        or ":" in normalized
    ):
        raise FrameworkArtifactError(f"unsafe framework artifact path: {relative_path}")

    root = workspace.resolve()
    unresolved = root / normalized
    if unresolved.is_symlink():
        raise FrameworkArtifactError(f"framework artifact cannot be a symlink: {relative_path}")
    candidate = unresolved.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise FrameworkArtifactError(
            f"framework artifact escaped the workspace: {relative_path}"
        ) from exc
    if not candidate.is_file():
        raise FrameworkArtifactError(f"framework artifact is not a regular file: {relative_path}")
    return candidate


async def import_workspace_file(
    repository: ArtifactRepository,
    workspace: Path,
    relative_path: str,
    media_type: str,
    *,
    max_bytes: int,
) -> tuple[bytes, ArtifactRef]:
    path = resolve_workspace_file(workspace, relative_path)
    size = path.stat().st_size
    if size > max_bytes:
        raise FrameworkArtifactError(
            f"framework artifact exceeds capture limit: {relative_path} ({size}>{max_bytes})"
        )
    content = path.read_bytes()
    return content, await repository.put_artifact(content, media_type)


def unique_artifacts(*references: ArtifactRef) -> tuple[ArtifactRef, ...]:
    return tuple({reference.digest: reference for reference in references}.values())
