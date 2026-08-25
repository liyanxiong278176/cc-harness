"""Frozen Linux bootstrap artifacts for Terminal-Bench agent containers."""

from __future__ import annotations

import hashlib
import os
import tarfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

UV_BOOTSTRAP_VERSION = "0.7.13"
UV_BOOTSTRAP_FILENAME = f"uv-{UV_BOOTSTRAP_VERSION}-x86_64-unknown-linux-gnu.tar.gz"
UV_BOOTSTRAP_URL = (
    "https://github.com/astral-sh/uv/releases/download/"
    f"{UV_BOOTSTRAP_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz"
)
UV_BOOTSTRAP_SHA256 = "909278eb197c5ed0e9b5f16317d1255270d1f9ea4196e7179ce934d48c4c2545"
UV_BOOTSTRAP_MAX_BYTES = 64 * 1024 * 1024
UV_BOOTSTRAP_ATTEMPTS = 3

Progress = Callable[[str], None]


def uv_bootstrap_cache_path(project_root: Path) -> Path:
    return (
        project_root
        / "eval"
        / "cache"
        / "terminal-bench"
        / UV_BOOTSTRAP_FILENAME
    )


def uv_bootstrap_identity(path: Path) -> dict[str, int | str]:
    return {
        "version": UV_BOOTSTRAP_VERSION,
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def ensure_uv_bootstrap(
    project_root: Path,
    *,
    progress: Progress | None = None,
) -> Path:
    """Return a verified Linux uv archive, downloading it once on the host.

    Task containers must not depend on GitHub/Astral being reachable during
    every agent setup.  The host-side artifact is content-addressed and then
    frozen into the formal run so all 89 tasks use the same bootstrap binary.
    """

    target = uv_bootstrap_cache_path(project_root.resolve())
    if _valid_archive(target):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    last_error: Exception | None = None
    for attempt in range(1, UV_BOOTSTRAP_ATTEMPTS + 1):
        try:
            if temporary.exists():
                temporary.unlink()
            request = urllib.request.Request(
                UV_BOOTSTRAP_URL,
                headers={"User-Agent": "cc-harness-terminal-bench-bootstrap/1"},
            )
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
                with temporary.open("wb") as stream:
                    total = 0
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > UV_BOOTSTRAP_MAX_BYTES:
                            raise ValueError("uv bootstrap archive exceeds the size limit")
                        stream.write(chunk)
            if not _valid_archive(temporary):
                raise ValueError("downloaded uv bootstrap archive failed integrity validation")
            os.replace(temporary, target)
            if progress is not None:
                progress(f"prepared verified Linux uv bootstrap {UV_BOOTSTRAP_VERSION}")
            return target
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = exc
            if progress is not None:
                progress(f"uv bootstrap attempt {attempt}/{UV_BOOTSTRAP_ATTEMPTS} failed: {exc}")
    raise RuntimeError(
        "unable to prepare the verified Linux uv bootstrap; "
        "fix host network access before starting Terminal-Bench"
    ) from last_error


def _valid_archive(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size > UV_BOOTSTRAP_MAX_BYTES:
        return False
    try:
        if _sha256(path) != UV_BOOTSTRAP_SHA256:
            return False
        with tarfile.open(path, mode="r:gz") as archive:
            names = set(archive.getnames())
        return any(name.endswith("/uv") for name in names) and any(
            name.endswith("/uvx") for name in names
        )
    except (OSError, tarfile.TarError):
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "UV_BOOTSTRAP_FILENAME",
    "UV_BOOTSTRAP_SHA256",
    "UV_BOOTSTRAP_URL",
    "UV_BOOTSTRAP_VERSION",
    "ensure_uv_bootstrap",
    "uv_bootstrap_cache_path",
    "uv_bootstrap_identity",
]
