"""Frozen tiktoken encoding data for offline Terminal-Bench agents.

``tiktoken`` lazily downloads its merge table the first time a token counter is
constructed.  That network request is unrelated to the benchmark task and can
turn an otherwise healthy trial into an invalid Harbor job.  Download the
content-addressed table once on the host and mount it into every task image.
"""

from __future__ import annotations

import hashlib
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

TIKTOKEN_ENCODING = "cl100k_base"
TIKTOKEN_URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
TIKTOKEN_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
TIKTOKEN_CACHE_KEY = hashlib.sha1(TIKTOKEN_URL.encode("utf-8")).hexdigest()
TIKTOKEN_BOOTSTRAP_FILENAME = "tiktoken-cl100k-base-v1.tiktoken"
TIKTOKEN_MAX_BYTES = 8 * 1024 * 1024
TIKTOKEN_ATTEMPTS = 3

Progress = Callable[[str], None]


def tiktoken_bootstrap_cache_path(project_root: Path) -> Path:
    return (
        project_root
        / "eval"
        / "cache"
        / "terminal-bench"
        / TIKTOKEN_BOOTSTRAP_FILENAME
    )


def tiktoken_bootstrap_identity(path: Path) -> dict[str, int | str]:
    if not _valid_file(path):
        raise ValueError("tiktoken bootstrap failed integrity validation")
    return {
        "encoding": TIKTOKEN_ENCODING,
        "url": TIKTOKEN_URL,
        "cache_key": TIKTOKEN_CACHE_KEY,
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def ensure_tiktoken_bootstrap(
    project_root: Path,
    *,
    progress: Progress | None = None,
) -> Path:
    target = tiktoken_bootstrap_cache_path(project_root.resolve())
    if _valid_file(target):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    last_error: Exception | None = None
    for attempt in range(1, TIKTOKEN_ATTEMPTS + 1):
        try:
            temporary.unlink(missing_ok=True)
            request = urllib.request.Request(
                TIKTOKEN_URL,
                headers={"User-Agent": "cc-harness-terminal-bench-bootstrap/1"},
            )
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
                total = 0
                with temporary.open("wb") as stream:
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > TIKTOKEN_MAX_BYTES:
                            raise ValueError("tiktoken bootstrap exceeds the size limit")
                        stream.write(chunk)
            if not _valid_file(temporary):
                raise ValueError("downloaded tiktoken bootstrap failed integrity validation")
            os.replace(temporary, target)
            if progress is not None:
                progress(f"prepared offline {TIKTOKEN_ENCODING} tokenizer data")
            return target
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = exc
            if progress is not None:
                progress(f"tiktoken bootstrap attempt {attempt}/{TIKTOKEN_ATTEMPTS} failed: {exc}")
    raise RuntimeError(
        "unable to prepare the offline tiktoken encoding; "
        "fix host network access before starting Terminal-Bench"
    ) from last_error


def _valid_file(path: Path) -> bool:
    try:
        return (
            path.is_file()
            and 0 < path.stat().st_size <= TIKTOKEN_MAX_BYTES
            and _sha256(path) == TIKTOKEN_SHA256
        )
    except OSError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "TIKTOKEN_BOOTSTRAP_FILENAME",
    "TIKTOKEN_CACHE_KEY",
    "TIKTOKEN_ENCODING",
    "TIKTOKEN_SHA256",
    "TIKTOKEN_URL",
    "ensure_tiktoken_bootstrap",
    "tiktoken_bootstrap_cache_path",
    "tiktoken_bootstrap_identity",
]
