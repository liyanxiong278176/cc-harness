"""Crash-safe local file publication helpers."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


def _extended_windows_path(path: Path) -> str | Path:
    if os.name != "nt":
        return path
    value = str(path.resolve())
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def atomic_write_text(path: Path, value: str, *, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, value.encode(encoding))


def atomic_write_bytes(path: Path, value: bytes) -> None:
    """Publish bytes atomically despite short-lived Windows sharing violations."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".tmp-",
        suffix=".tmp",
        dir=_extended_windows_path(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temporary, path)
    finally:
        _unlink_with_retry(temporary)


def _replace_with_retry(source: Path, target: Path) -> None:
    for attempt in range(20):
        try:
            os.replace(_extended_windows_path(source), _extended_windows_path(target))
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(min(0.01 * (attempt + 1), 0.1))


def _unlink_with_retry(path: Path) -> None:
    for attempt in range(5):
        try:
            try:
                os.unlink(_extended_windows_path(path))
            except FileNotFoundError:
                pass
            return
        except PermissionError:
            if attempt == 4:
                return
            time.sleep(0.01 * (attempt + 1))
