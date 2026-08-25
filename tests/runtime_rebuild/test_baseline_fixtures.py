from __future__ import annotations

import json
from pathlib import Path


def test_legacy_fixture_manifest_matches_files(legacy_fixture_root: Path, fixture_digest) -> None:
    manifest = json.loads((legacy_fixture_root / "manifest.json").read_text(encoding="utf-8"))
    for name, entry in manifest["fixtures"].items():
        assert fixture_digest(legacy_fixture_root / name) == entry["sha256"]


def test_corrupt_fixture_is_intentionally_unparseable(legacy_fixture_root: Path) -> None:
    import pytest

    with pytest.raises(json.JSONDecodeError):
        json.loads((legacy_fixture_root / "corrupt.json").read_text(encoding="utf-8"))
