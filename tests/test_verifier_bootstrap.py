from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

from eval.cc_only import verifier_bootstrap


def _archive(path: Path) -> None:
    wheel_content = b"wheel"
    wheels = list(verifier_bootstrap.VERIFIER_BOOTSTRAP_WHEELS)
    manifest = {
        "schema_version": "terminal-bench.verifier-bootstrap.v1",
        "version": verifier_bootstrap.VERIFIER_BOOTSTRAP_VERSION,
        "packages": list(verifier_bootstrap.VERIFIER_BOOTSTRAP_PACKAGES),
        "required_imports": list(verifier_bootstrap.VERIFIER_REQUIRED_IMPORTS),
        "wheels": [
            {
                "name": name,
                "sha256": hashlib.sha256(wheel_content).hexdigest(),
                "size_bytes": len(wheel_content),
            }
            for name in wheels
        ],
    }
    entries = {
        "manifest.json": json.dumps(manifest).encode(),
        "bin/curl": b"#!/bin/sh\n",
        "bin/uvx": b"#!/usr/local/bin/python3\n",
        "bin/install-wheelhouse.py": b"#!/usr/bin/env python3\n",
        "env": b"export PATH=...\n",
    }
    entries.update({f"wheelhouse/{name}": wheel_content for name in wheels})
    with tarfile.open(path, "w:gz") as archive:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


def test_verifier_bootstrap_identity_is_content_addressed(tmp_path: Path) -> None:
    archive = tmp_path / "verifier.tar.gz"
    _archive(archive)

    identity = verifier_bootstrap.verifier_bootstrap_identity(archive)

    assert identity["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert identity["size_bytes"] == archive.stat().st_size
    assert identity["version"] == verifier_bootstrap.VERIFIER_BOOTSTRAP_VERSION
    assert identity["required_imports"] == list(verifier_bootstrap.VERIFIER_REQUIRED_IMPORTS)


def test_verifier_bootstrap_cache_path_is_project_scoped(tmp_path: Path) -> None:
    path = verifier_bootstrap.verifier_bootstrap_cache_path(tmp_path)

    assert path.parent == tmp_path / "eval" / "cache" / "terminal-bench"
    assert path.name == verifier_bootstrap.VERIFIER_BOOTSTRAP_FILENAME
