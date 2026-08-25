"""SQLite/WAL persistence for the existing context projection manager.

This module is deliberately a persistence adapter, not another context
manager.  ``ContextProjection`` remains the only component that decides what
the model sees; this store only publishes immutable compaction versions and
the single current pointer transactionally.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ContextLeaseConflict(RuntimeError):
    """Another live writer owns this Run's context projection."""


class ContextCommitConflict(RuntimeError):
    """The current parent or lease changed before publication."""


@dataclass(frozen=True)
class StoredCompaction:
    context_id: str
    version: int
    compaction_key: str
    tier: str
    parent_version: int | None
    source_digest: str
    source_count: int
    summary_version: int | None
    payload: dict[str, Any]


class SqliteContextState:
    """Small synchronous repository sharing the runtime's SQLite/WAL file."""

    def __init__(self, db_path: Path, context_id: str) -> None:
        self.db_path = Path(db_path)
        self.context_id = str(context_id)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS context_compaction_version (
                    context_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    compaction_key TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    parent_version INTEGER,
                    source_digest TEXT NOT NULL,
                    source_count INTEGER NOT NULL,
                    summary_version INTEGER,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (context_id, version),
                    UNIQUE (context_id, compaction_key)
                );
                CREATE TABLE IF NOT EXISTS context_current_compaction (
                    context_id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    compaction_key TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (context_id, version)
                        REFERENCES context_compaction_version(context_id, version)
                );
                CREATE TABLE IF NOT EXISTS context_writer_lease (
                    context_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    acquired_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_call_manifest (
                    context_id TEXT NOT NULL,
                    call_sequence INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (context_id, call_sequence)
                );
                CREATE TRIGGER IF NOT EXISTS context_compaction_no_update
                BEFORE UPDATE ON context_compaction_version BEGIN
                    SELECT RAISE(ABORT, 'context compaction versions are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS context_compaction_no_delete
                BEFORE DELETE ON context_compaction_version BEGIN
                    SELECT RAISE(ABORT, 'context compaction versions are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS context_call_manifest_no_update
                BEFORE UPDATE ON context_call_manifest BEGIN
                    SELECT RAISE(ABORT, 'context call manifests are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS context_call_manifest_no_delete
                BEFORE DELETE ON context_call_manifest BEGIN
                    SELECT RAISE(ABORT, 'context call manifests are immutable');
                END;
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> StoredCompaction | None:
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        return StoredCompaction(
            context_id=str(row["context_id"]),
            version=int(row["version"]),
            compaction_key=str(row["compaction_key"]),
            tier=str(row["tier"]),
            parent_version=(
                int(row["parent_version"]) if row["parent_version"] is not None else None
            ),
            source_digest=str(row["source_digest"]),
            source_count=int(row["source_count"]),
            summary_version=(
                int(row["summary_version"]) if row["summary_version"] is not None else None
            ),
            payload=payload,
        )

    def current(self) -> StoredCompaction | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT version.* FROM context_current_compaction AS current "
                "JOIN context_compaction_version AS version "
                "ON version.context_id=current.context_id AND version.version=current.version "
                "WHERE current.context_id=?",
                (self.context_id,),
            ).fetchone()
        return self._row(row)

    def candidates(self) -> tuple[StoredCompaction, ...]:
        """Return current first, then older versions for rewind recovery."""
        current = self.current()
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM context_compaction_version WHERE context_id=? "
                "ORDER BY version DESC",
                (self.context_id,),
            ).fetchall()
        versions = [item for row in rows if (item := self._row(row)) is not None]
        if current is None:
            return tuple(versions)
        return (current, *tuple(item for item in versions if item.version != current.version))

    def by_key(self, compaction_key: str) -> StoredCompaction | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM context_compaction_version "
                "WHERE context_id=? AND compaction_key=?",
                (self.context_id, compaction_key),
            ).fetchone()
        return self._row(row)

    def acquire(self, owner_id: str, *, ttl_seconds: float) -> tuple[int, int | None]:
        now = time.time()
        expires_at = now + max(1.0, float(ttl_seconds))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            lease = db.execute(
                "SELECT owner_id, epoch, expires_at FROM context_writer_lease "
                "WHERE context_id=?",
                (self.context_id,),
            ).fetchone()
            if lease is not None and float(lease["expires_at"]) > now:
                if str(lease["owner_id"]) != owner_id:
                    db.rollback()
                    raise ContextLeaseConflict(
                        f"context {self.context_id!r} is owned by another live writer"
                    )
                epoch = int(lease["epoch"])
            else:
                epoch = (int(lease["epoch"]) if lease is not None else 0) + 1
            db.execute(
                "INSERT INTO context_writer_lease"
                "(context_id, owner_id, epoch, acquired_at, expires_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(context_id) DO UPDATE SET owner_id=excluded.owner_id, "
                "epoch=excluded.epoch, acquired_at=excluded.acquired_at, "
                "expires_at=excluded.expires_at",
                (self.context_id, owner_id, epoch, now, expires_at),
            )
            current = db.execute(
                "SELECT version FROM context_current_compaction WHERE context_id=?",
                (self.context_id,),
            ).fetchone()
            db.commit()
        return epoch, (int(current["version"]) if current is not None else None)

    def release(self, owner_id: str, epoch: int) -> None:
        with self._connect() as db:
            db.execute(
                "DELETE FROM context_writer_lease "
                "WHERE context_id=? AND owner_id=? AND epoch=?",
                (self.context_id, owner_id, int(epoch)),
            )

    def commit(
        self,
        *,
        owner_id: str,
        epoch: int,
        expected_parent: int | None,
        compaction_key: str,
        tier: str,
        source_digest: str,
        source_count: int,
        summary_version: int | None,
        payload: dict[str, Any],
    ) -> StoredCompaction:
        """Publish the immutable version and current pointer in one transaction."""

        now = time.time()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM context_compaction_version "
                "WHERE context_id=? AND compaction_key=?",
                (self.context_id, compaction_key),
            ).fetchone()
            if existing is not None:
                db.execute(
                    "DELETE FROM context_writer_lease "
                    "WHERE context_id=? AND owner_id=? AND epoch=?",
                    (self.context_id, owner_id, int(epoch)),
                )
                db.commit()
                return self._row(existing)  # type: ignore[return-value]

            lease = db.execute(
                "SELECT owner_id, epoch, expires_at FROM context_writer_lease "
                "WHERE context_id=?",
                (self.context_id,),
            ).fetchone()
            if (
                lease is None
                or str(lease["owner_id"]) != owner_id
                or int(lease["epoch"]) != int(epoch)
                or float(lease["expires_at"]) <= now
            ):
                db.rollback()
                raise ContextCommitConflict("context writer lease is stale")
            current = db.execute(
                "SELECT version FROM context_current_compaction WHERE context_id=?",
                (self.context_id,),
            ).fetchone()
            current_version = int(current["version"]) if current is not None else None
            if current_version != expected_parent:
                db.rollback()
                raise ContextCommitConflict(
                    f"expected parent {expected_parent}, current parent is {current_version}"
                )
            version = (current_version or 0) + 1
            stored_payload = dict(payload)
            stored_payload["version"] = version
            stored_payload["parent_version"] = expected_parent
            encoded = json.dumps(
                stored_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            db.execute(
                "INSERT INTO context_compaction_version"
                "(context_id, version, compaction_key, tier, parent_version, source_digest, "
                "source_count, summary_version, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.context_id,
                    version,
                    compaction_key,
                    tier,
                    expected_parent,
                    source_digest,
                    int(source_count),
                    summary_version,
                    encoded,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO context_current_compaction"
                "(context_id, version, compaction_key, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(context_id) DO UPDATE SET version=excluded.version, "
                "compaction_key=excluded.compaction_key, updated_at=excluded.updated_at",
                (self.context_id, version, compaction_key, now),
            )
            db.execute(
                "DELETE FROM context_writer_lease "
                "WHERE context_id=? AND owner_id=? AND epoch=?",
                (self.context_id, owner_id, int(epoch)),
            )
            db.commit()
            row = db.execute(
                "SELECT * FROM context_compaction_version WHERE context_id=? AND version=?",
                (self.context_id, version),
            ).fetchone()
        return self._row(row)  # type: ignore[return-value]

    def manifest_uri(self, version: int) -> str:
        return f"sqlite:///{self.db_path.resolve().as_posix()}#context/{self.context_id}/{version}"

    def record_call(self, payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT COALESCE(MAX(call_sequence), 0) + 1 FROM context_call_manifest "
                "WHERE context_id=?",
                (self.context_id,),
            ).fetchone()
            sequence = int(row[0])
            db.execute(
                "INSERT INTO context_call_manifest(context_id, call_sequence, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (self.context_id, sequence, encoded, time.time()),
            )
            db.commit()
        return (
            f"sqlite:///{self.db_path.resolve().as_posix()}"
            f"#context-call/{self.context_id}/{sequence}"
        )

    def authorizes_ref(self, source_ref: str) -> bool:
        """Authorize only refs reachable from the one current manifest."""
        current = self.current()
        if current is None:
            return False
        payload = current.payload
        if str(payload.get("source_scope") or "") == str(source_ref):
            return True
        if any(
            str(item.get("source_ref") or "") == str(source_ref)
            for item in (payload.get("source_refs") or ())
            if isinstance(item, dict)
        ):
            return True
        return any(
            str(item.get("source_ref") or "") == str(source_ref)
            for item in (payload.get("cumulative_entries") or ())
            if isinstance(item, dict)
        )


__all__ = [
    "ContextCommitConflict",
    "ContextLeaseConflict",
    "SqliteContextState",
    "StoredCompaction",
]
