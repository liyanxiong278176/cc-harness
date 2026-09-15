from __future__ import annotations

import json
import hashlib
import sqlite3

import pytest

from cc_harness.coordinator import RunCoordinator, RunRequest
from cc_harness.run_store import RunStore, RunStoreError


def _restore_snapshot_update_trigger(database: sqlite3.Connection) -> None:
    database.execute(
        """CREATE TRIGGER run_snapshot_no_update
           BEFORE UPDATE ON run_snapshot BEGIN
               SELECT RAISE(ABORT, 'run snapshots are immutable');
           END"""
    )


@pytest.mark.asyncio
async def test_schema_evolved_snapshot_replays_from_authoritative_events(tmp_path) -> None:
    """A legacy canonical snapshot must not strand a queued/follow-up Run."""

    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        handle = await RunCoordinator(store).submit(RunRequest("legacy snapshot", ("recorded",)))
        snapshot = await store.checkpoint(handle.run_id)
        payload = snapshot.to_dict()
        # ``outcome`` was added after the snapshot was written.  The raw
        # canonical digest remains valid, but the current dataclass digest is
        # intentionally different and must be rebuilt from events.
        payload.pop("outcome")
        legacy_digest = "sha256:" + hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with sqlite3.connect(store.db_path) as database:
            database.execute("DROP TRIGGER run_snapshot_no_update")
            database.execute(
                "UPDATE run_snapshot SET projection_json = ?, projection_digest = ? "
                "WHERE run_id = ? AND sequence = ?",
                (
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    legacy_digest,
                    handle.run_id,
                    snapshot.sequence,
                ),
            )
            database.execute(
                "UPDATE run_record SET projection_digest = ? WHERE run_id = ?",
                (legacy_digest, handle.run_id),
            )
            _restore_snapshot_update_trigger(database)
            database.commit()

        rebuilt = await store.load_projection(handle.run_id)
        assert rebuilt.sequence == snapshot.sequence
        assert rebuilt.digest != legacy_digest
        # The replay is not just an in-memory compatibility shim.  The
        # denormalized cursor is repaired atomically so the supervisor and the
        # next append do not rediscover the same legacy mismatch forever.
        with sqlite3.connect(store.db_path) as database:
            row = database.execute(
                "SELECT last_sequence, projection_digest FROM run_record WHERE run_id = ?",
                (handle.run_id,),
            ).fetchone()
        assert row == (snapshot.sequence, rebuilt.digest)
        second = await store.load_projection(handle.run_id)
        assert second.digest == rebuilt.digest
        # Re-checkpointing an upgraded run must remain idempotent even though
        # the immutable legacy snapshot at this sequence cannot be replaced.
        assert (await store.checkpoint(handle.run_id)).digest == rebuilt.digest
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_snapshot_bytes_tampering_still_fails_closed(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        handle = await RunCoordinator(store).submit(RunRequest("tamper snapshot", ("recorded",)))
        snapshot = await store.checkpoint(handle.run_id)
        payload = snapshot.to_dict()
        payload["status"] = "completed"
        with sqlite3.connect(store.db_path) as database:
            database.execute("DROP TRIGGER run_snapshot_no_update")
            database.execute(
                "UPDATE run_snapshot SET projection_json = ? WHERE run_id = ? AND sequence = ?",
                (
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    handle.run_id,
                    snapshot.sequence,
                ),
            )
            _restore_snapshot_update_trigger(database)
            database.commit()

        with pytest.raises(RunStoreError, match="snapshot digest mismatch"):
            await store.load_projection(handle.run_id)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_stale_derived_projection_digest_is_repaired_from_events(tmp_path) -> None:
    """A crash-stale run cursor must not strand the supervisor forever."""

    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        handle = await RunCoordinator(store).submit(RunRequest("stale cursor", ("recorded",)))
        projection = await store.load_projection(handle.run_id)
        with sqlite3.connect(store.db_path) as database:
            database.execute(
                "UPDATE run_record SET projection_digest = ? WHERE run_id = ?",
                ("sha256:stale-derived-digest", handle.run_id),
            )
            database.commit()

        rebuilt = await store.load_projection(handle.run_id)
        assert rebuilt.sequence == projection.sequence
        assert rebuilt.digest == projection.digest
        with sqlite3.connect(store.db_path) as database:
            row = database.execute(
                "SELECT last_sequence, projection_digest FROM run_record WHERE run_id = ?",
                (handle.run_id,),
            ).fetchone()
        assert row == (projection.sequence, projection.digest)
    finally:
        await store.close()
