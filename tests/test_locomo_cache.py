from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from eval.cc_only.locomo_cache import (
    CacheBusyError,
    CacheIdentity,
    LoCoMoSnapshotStore,
)


def _identity() -> CacheIdentity:
    return CacheIdentity(
        sample_id="conv-26",
        sample_digest="sha256:sample",
        model="deepseek-v4-flash",
        protocol_version="locomo-context-memory-adaptation.v3",
        capability_profile="memory-eval",
        ingestion_contract="locomo-ingestion-contract.v1",
        implementation_digest="sha256:implementation",
        memory_scope="locomo:conv-26:sample",
    )


def _snapshot(root: Path, scope: str) -> Path:
    snapshot = root / "snapshot"
    workspace = snapshot / "workspace-state"
    (snapshot / "home-state").mkdir(parents=True)
    workspace.mkdir(parents=True)
    connection = sqlite3.connect(workspace / "memory.db")
    connection.executescript(
        """
        create table memories (validity text, project_scope text);
        create table conversation (id integer);
        insert into memories values ('active', 'locomo:conv-26:sample');
        insert into conversation values (1);
        """
    )
    connection.commit()
    connection.close()
    (snapshot / "checkpoint.json").write_text(
        json.dumps({"complete": True}), encoding="utf-8"
    )
    (snapshot / "empty-evidence.txt").write_bytes(b"")
    return snapshot


def test_snapshot_publish_admit_and_tamper_detection(tmp_path: Path) -> None:
    store = LoCoMoSnapshotStore(tmp_path)
    identity = _identity()
    attempt = tmp_path / "attempt"
    snapshot = _snapshot(attempt, identity.memory_scope)

    hit = store.publish(
        identity,
        attempt_root=attempt,
        snapshot=snapshot,
        session_names=["session_1"],
        preparation_usage={"model_calls": 3, "cost_microusd": 10},
        source="test",
    )
    assert hit.preparation_usage["model_calls"] == 3
    assert store.admit(
        identity,
        session_names=["session_1"],
        expected_atom_scope=identity.memory_scope,
    ) is not None

    database = hit.snapshot / "workspace-state" / "memory.db"
    database.write_bytes(database.read_bytes() + b"tamper")
    assert (
        store.admit(
            identity,
            session_names=["session_1"],
            expected_atom_scope=identity.memory_scope,
        )
        is None
    )


def test_duplicate_live_snapshot_build_is_rejected(tmp_path: Path) -> None:
    store = LoCoMoSnapshotStore(tmp_path)
    identity = _identity()
    with store.lock(identity), pytest.raises(CacheBusyError), store.lock(identity):
        pass


def test_stale_snapshot_lock_can_be_reclaimed(tmp_path: Path) -> None:
    store = LoCoMoSnapshotStore(tmp_path)
    identity = _identity()
    lock = store.root / "locks" / f"{identity.sample_id}-{identity.key[:16]}.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"pid": 999_999_999}), encoding="utf-8")
    with store.lock(identity):
        assert lock.is_file()
    assert not lock.exists()


def test_build_restore_stops_at_first_checkpoint_gap(tmp_path: Path) -> None:
    store = LoCoMoSnapshotStore(tmp_path)
    identity = _identity()
    build = store.build_root(identity)
    for index in (1, 3):
        checkpoint = build / "ingestion-checkpoints" / f"{index:04d}"
        checkpoint.mkdir(parents=True)
        (checkpoint / "checkpoint.json").write_text("{}", encoding="utf-8")
    restored = tmp_path / "attempt"

    assert store.restore_build(identity, restored) == 1
    assert (restored / "ingestion-checkpoints" / "0001").is_dir()
    assert not (restored / "ingestion-checkpoints" / "0003").exists()
