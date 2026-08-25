from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

from eval.cc_only import terminal_bootstrap


def test_uv_bootstrap_identity_is_content_addressed(tmp_path: Path) -> None:
    archive = tmp_path / "uv.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for name in ("uv-x86_64-unknown-linux-gnu/uv", "uv-x86_64-unknown-linux-gnu/uvx"):
            info = tarfile.TarInfo(name)
            info.size = 3
            output.addfile(info, io.BytesIO(b"bin"))
    expected = hashlib.sha256(archive.read_bytes()).hexdigest()
    identity = terminal_bootstrap.uv_bootstrap_identity(archive)
    assert identity["sha256"] == expected
    assert identity["size_bytes"] == archive.stat().st_size


def test_uv_bootstrap_cache_path_is_project_scoped(tmp_path: Path) -> None:
    path = terminal_bootstrap.uv_bootstrap_cache_path(tmp_path)
    assert path.parent == tmp_path / "eval" / "cache" / "terminal-bench"
    assert path.name == terminal_bootstrap.UV_BOOTSTRAP_FILENAME
