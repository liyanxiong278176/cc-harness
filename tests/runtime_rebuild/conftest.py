"""Fixtures shared by the durable-runtime rebuild contract tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "legacy"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


@pytest.fixture
def legacy_fixture_root() -> Path:
    return FIXTURE_ROOT


@pytest.fixture
def fixture_digest():
    return sha256_file
