from __future__ import annotations

import os
import time

import pytest

from cc_harness.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFound,
    ArtifactStore,
    digest_bytes,
)


def test_artifact_store_deduplicates_and_round_trips(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "objects")
    first = store.put(b"durable result", media_type="text/plain")
    second = store.put(b"durable result", media_type="application/octet-stream")

    assert first.digest == second.digest == digest_bytes(b"durable result")
    assert first.size_bytes == second.size_bytes == len(b"durable result")
    assert store.read(first.digest) == b"durable result"
    assert store.verify(first.digest).digest == first.digest
    assert len(tuple(path for path in (tmp_path / "objects").rglob("*") if path.is_file())) == 2


def test_digest_mismatch_and_missing_object_are_rejected(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "objects")
    with pytest.raises(ArtifactIntegrityError):
        store.put(b"content", expected_digest="sha256:" + "0" * 64)

    missing = "sha256:" + "1" * 64
    with pytest.raises(ArtifactNotFound):
        store.read(missing)


def test_corrupted_object_is_detected(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "objects")
    ref = store.put(b"original")
    store.object_path(ref.digest).write_bytes(b"tampered")
    assert not store.exists(ref.digest)
    with pytest.raises(ArtifactIntegrityError):
        store.read(ref.digest)


def test_gc_respects_references_and_grace_period(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "objects", grace_period_seconds=60)
    protected = store.put(b"referenced")
    old_orphan = store.put(b"orphan")
    young_orphan = store.put(b"young")
    old_time = time.time() - 3600
    os.utime(store.object_path(old_orphan.digest), (old_time, old_time))

    report = store.collect_garbage(
        {protected.digest},
        now=time.time(),
        grace_period_seconds=60,
    )
    assert old_orphan.digest in report.deleted
    assert protected.digest in report.protected
    assert young_orphan.digest in report.young
    assert store.exists(protected.digest)
    assert not store.object_path(old_orphan.digest).exists()
    assert store.object_path(young_orphan.digest).exists()


def test_abandoned_temporary_files_can_be_cleaned(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "objects")
    temp = store.root / "aa" / ".tmp-crashed-write"
    temp.parent.mkdir(parents=True)
    temp.write_bytes(b"partial")
    removed = store.cleanup_temporary_files()
    assert temp in removed
    assert not temp.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path contract")
def test_atomic_publish_supports_windows_paths_beyond_legacy_max_path(tmp_path) -> None:
    deep = tmp_path
    while len(str(deep / "objects" / ("f" * 64))) <= 280:
        deep /= "isolated-durable-home"
    store = ArtifactStore(deep / "objects")

    ref = store.put(b"long-path durable evidence", media_type="text/plain")

    assert store.read(ref.digest) == b"long-path durable evidence"
    assert store.verify(ref.digest).media_type == "text/plain"
