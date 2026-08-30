"""Append-only project facts and rebuildable session projections."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from .sqlite_utils import begin_immediate

EVENT_SCHEMA_VERSION = 1
SUMMARY_SCHEMA_VERSION = 1
MAX_EVENT_PAGE = 1_000


class FactStoreError(RuntimeError):
    """The durable fact store rejected an invalid or conflicting operation."""


class ArtifactIntegrityError(FactStoreError):
    """A content-addressed object does not match its recorded digest."""


@dataclass(frozen=True)
class ArtifactRef:
    digest: str
    size_bytes: int
    media_type: str


@dataclass(frozen=True)
class FactEvent:
    session_id: str
    seq: int
    event_id: str
    event_type: str
    payload: dict[str, Any]
    parent_seq: int | None
    artifact_digest: str | None
    causal_event_id: str | None
    attempt_id: str | None
    schema_version: int
    created_at_ns: int


@dataclass(frozen=True)
class EventPage:
    events: tuple[FactEvent, ...]
    next_cursor: int | None


@dataclass(frozen=True)
class CompactionSummary:
    session_id: str
    version: int
    summary_id: str
    parent_summary_id: str | None
    covers_from_seq: int
    covers_through_seq: int
    source_hash: str
    schema_version: int
    prompt_version: str
    model: str
    artifact_digest: str
    created_at_ns: int


@dataclass(frozen=True)
class ContextProjection:
    session_id: str
    head_seq: int | None
    event_ids: tuple[str, ...]
    summary: CompactionSummary | None
    summary_text: str | None
    messages: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class LegacyImportReport:
    imported_sessions: int
    imported_events: int
    skipped_sessions: int


def default_user_data_dir() -> Path:
    """Return the platform user-data directory without touching the filesystem."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "cc-harness"


def project_identity(project_root: Path) -> tuple[str, str]:
    canonical = os.path.normcase(str(Path(project_root).resolve(strict=True)))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"project_{digest[:32]}", canonical


class ProjectFactStore:
    """Project-isolated append-only facts with content-addressed artifacts."""

    def __init__(self, project_root: Path, *, data_root: Path | None = None) -> None:
        self.project_root = Path(project_root).resolve(strict=True)
        self.project_id, self.canonical_root = project_identity(self.project_root)
        self.data_root = Path(data_root) if data_root is not None else default_user_data_dir()
        self.state_dir = self.data_root / "projects" / self.project_id
        self.db_path = self.state_dir / "facts.db"
        self.objects_root = self.state_dir / "objects" / "sha256"
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def open(self) -> ProjectFactStore:
        self.objects_root.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path, timeout=30)
        await self._db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=30000;

            CREATE TABLE IF NOT EXISTS project (
                project_id TEXT PRIMARY KEY,
                canonical_root TEXT NOT NULL UNIQUE,
                created_at_ns INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session (
                session_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                parent_session_id TEXT,
                parent_seq INTEGER,
                active_head_seq INTEGER,
                created_at_ns INTEGER NOT NULL,
                updated_at_ns INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifact (
                digest TEXT PRIMARY KEY,
                size_bytes INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                created_at_ns INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS event (
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                parent_seq INTEGER,
                artifact_digest TEXT,
                causal_event_id TEXT,
                attempt_id TEXT,
                schema_version INTEGER NOT NULL,
                created_at_ns INTEGER NOT NULL,
                PRIMARY KEY (session_id, seq),
                FOREIGN KEY (session_id) REFERENCES session(session_id),
                FOREIGN KEY (artifact_digest) REFERENCES artifact(digest)
            );
            CREATE TABLE IF NOT EXISTS compaction_summary (
                session_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                summary_id TEXT NOT NULL UNIQUE,
                parent_summary_id TEXT,
                covers_from_seq INTEGER NOT NULL,
                covers_through_seq INTEGER NOT NULL,
                source_hash TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                prompt_version TEXT NOT NULL,
                model TEXT NOT NULL,
                artifact_digest TEXT NOT NULL,
                created_at_ns INTEGER NOT NULL,
                PRIMARY KEY (session_id, version),
                FOREIGN KEY (session_id) REFERENCES session(session_id),
                FOREIGN KEY (artifact_digest) REFERENCES artifact(digest)
            );
            CREATE TABLE IF NOT EXISTS legacy_import (
                source_key TEXT NOT NULL,
                source_session_id TEXT NOT NULL,
                imported_event_count INTEGER NOT NULL,
                imported_at_ns INTEGER NOT NULL,
                PRIMARY KEY (source_key, source_session_id)
            );

            CREATE TRIGGER IF NOT EXISTS event_no_update
            BEFORE UPDATE ON event BEGIN
                SELECT RAISE(ABORT, 'events are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS event_no_delete
            BEFORE DELETE ON event BEGIN
                SELECT RAISE(ABORT, 'events are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS artifact_no_update
            BEFORE UPDATE ON artifact BEGIN
                SELECT RAISE(ABORT, 'artifacts are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS artifact_no_delete
            BEFORE DELETE ON artifact BEGIN
                SELECT RAISE(ABORT, 'artifacts are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS summary_no_update
            BEFORE UPDATE ON compaction_summary BEGIN
                SELECT RAISE(ABORT, 'summaries are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS summary_no_delete
            BEFORE DELETE ON compaction_summary BEGIN
                SELECT RAISE(ABORT, 'summaries are immutable');
            END;
            """
        )
        now = time.time_ns()
        await self._db.execute(
            "INSERT OR IGNORE INTO project (project_id, canonical_root, created_at_ns) "
            "VALUES (?, ?, ?)",
            (self.project_id, self.canonical_root, now),
        )
        cursor = await self._db.execute(
            "SELECT canonical_root FROM project WHERE project_id = ?", (self.project_id,)
        )
        row = await cursor.fetchone()
        if row is None or row[0] != self.canonical_root:
            raise FactStoreError("project identity collision or mismatched project root")
        await self._db.commit()
        self._restrict_permissions(self.db_path)
        return self

    async def create_session(
        self,
        session_id: str,
        *,
        mode: str,
        title: str = "Untitled session",
        status: str = "active",
        parent_session_id: str | None = None,
        parent_seq: int | None = None,
    ) -> bool:
        if not session_id:
            raise FactStoreError("session_id cannot be empty")
        db = self._require_db()
        async with self._write_lock:
            await begin_immediate(db)
            try:
                cursor = await db.execute(
                    "INSERT OR IGNORE INTO session "
                    "(session_id, mode, title, status, parent_session_id, parent_seq, "
                    "active_head_seq, created_at_ns, updated_at_ns) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                    (
                        session_id,
                        mode,
                        title,
                        status,
                        parent_session_id,
                        parent_seq,
                        time.time_ns(),
                        time.time_ns(),
                    ),
                )
                created = cursor.rowcount == 1
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return created

    async def append_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        artifact_digest: str | None = None,
        causal_event_id: str | None = None,
        attempt_id: str | None = None,
        event_id: str | None = None,
        schema_version: int = EVENT_SCHEMA_VERSION,
    ) -> FactEvent:
        if not event_type or not isinstance(payload, dict):
            raise FactStoreError("event_type and object payload are required")
        db = self._require_db()
        async with self._write_lock:
            await begin_immediate(db)
            try:
                event = await self._append_event_tx(
                    session_id,
                    event_type,
                    payload,
                    artifact_digest=artifact_digest,
                    causal_event_id=causal_event_id,
                    attempt_id=attempt_id,
                    event_id=event_id,
                    schema_version=schema_version,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return event

    async def rewind(self, session_id: str, target_seq: int) -> FactEvent:
        db = self._require_db()
        async with self._write_lock:
            await begin_immediate(db)
            try:
                cursor = await db.execute(
                    "SELECT 1 FROM event WHERE session_id = ? AND seq = ?",
                    (session_id, target_seq),
                )
                if await cursor.fetchone() is None:
                    raise FactStoreError(f"rewind target does not exist: {target_seq}")
                event = await self._append_event_tx(
                    session_id,
                    "projection_head_moved",
                    {"target_seq": target_seq},
                    parent_seq=target_seq,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return event

    async def read_events(
        self, session_id: str, *, after_seq: int = 0, limit: int = 200
    ) -> EventPage:
        if after_seq < 0 or limit < 1:
            raise FactStoreError("after_seq must be non-negative and limit must be positive")
        page_size = min(limit, MAX_EVENT_PAGE)
        cursor = await self._require_db().execute(
            "SELECT session_id, seq, event_id, event_type, payload_json, parent_seq, "
            "artifact_digest, causal_event_id, attempt_id, schema_version, created_at_ns "
            "FROM event WHERE session_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (session_id, after_seq, page_size + 1),
        )
        rows = await cursor.fetchall()
        has_more = len(rows) > page_size
        events = tuple(self._event_from_row(row) for row in rows[:page_size])
        return EventPage(events, events[-1].seq if has_more and events else None)

    async def put_object(
        self, content: bytes, *, media_type: str = "application/octet-stream"
    ) -> ArtifactRef:
        if not isinstance(content, bytes):
            raise FactStoreError("object content must be bytes")
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        target = self._object_path(digest)
        await asyncio.to_thread(self._write_object, target, content)
        db = self._require_db()
        async with self._write_lock:
            await begin_immediate(db)
            try:
                await db.execute(
                    "INSERT OR IGNORE INTO artifact "
                    "(digest, size_bytes, media_type, created_at_ns) VALUES (?, ?, ?, ?)",
                    (digest, len(content), media_type, time.time_ns()),
                )
                cursor = await db.execute(
                    "SELECT size_bytes, media_type FROM artifact WHERE digest = ?", (digest,)
                )
                row = await cursor.fetchone()
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        if row is None or int(row[0]) != len(content):
            raise ArtifactIntegrityError(f"artifact metadata mismatch: {digest}")
        return ArtifactRef(digest, len(content), str(row[1]))

    async def read_object(self, digest: str) -> bytes:
        cursor = await self._require_db().execute(
            "SELECT size_bytes FROM artifact WHERE digest = ?", (digest,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise KeyError(digest)
        content = await asyncio.to_thread(self._object_path(digest).read_bytes)
        actual = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if actual != digest or len(content) != int(row[0]):
            raise ArtifactIntegrityError(f"artifact integrity check failed: {digest}")
        return content

    async def append_summary(
        self,
        session_id: str,
        text: str,
        *,
        covers_from_seq: int,
        covers_through_seq: int,
        prompt_version: str,
        model: str,
        parent_summary_id: str | None = None,
        schema_version: int = SUMMARY_SCHEMA_VERSION,
    ) -> CompactionSummary:
        if covers_from_seq < 1 or covers_through_seq < covers_from_seq:
            raise FactStoreError("summary event range is invalid")
        artifact = await self.put_object(text.encode("utf-8"), media_type="text/plain; charset=utf-8")
        db = self._require_db()
        async with self._write_lock:
            await begin_immediate(db)
            try:
                rows = await self._event_rows(session_id, covers_from_seq, covers_through_seq)
                expected = covers_through_seq - covers_from_seq + 1
                if len(rows) != expected:
                    raise FactStoreError("summary range does not contain a complete event sequence")
                source_hash = self._source_hash(rows)
                cursor = await db.execute(
                    "SELECT version, summary_id FROM compaction_summary "
                    "WHERE session_id = ? ORDER BY version DESC LIMIT 1",
                    (session_id,),
                )
                latest = await cursor.fetchone()
                version = int(latest[0]) + 1 if latest is not None else 1
                if parent_summary_id is None and latest is not None:
                    parent_summary_id = str(latest[1])
                if parent_summary_id is not None:
                    parent = await db.execute(
                        "SELECT 1 FROM compaction_summary "
                        "WHERE session_id = ? AND summary_id = ?",
                        (session_id, parent_summary_id),
                    )
                    if await parent.fetchone() is None:
                        raise FactStoreError("parent summary does not belong to the session")
                summary = CompactionSummary(
                    session_id=session_id,
                    version=version,
                    summary_id=f"sum_{uuid.uuid4().hex}",
                    parent_summary_id=parent_summary_id,
                    covers_from_seq=covers_from_seq,
                    covers_through_seq=covers_through_seq,
                    source_hash=source_hash,
                    schema_version=schema_version,
                    prompt_version=prompt_version,
                    model=model,
                    artifact_digest=artifact.digest,
                    created_at_ns=time.time_ns(),
                )
                await db.execute(
                    "INSERT INTO compaction_summary "
                    "(session_id, version, summary_id, parent_summary_id, covers_from_seq, "
                    "covers_through_seq, source_hash, schema_version, prompt_version, model, "
                    "artifact_digest, created_at_ns) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        summary.session_id,
                        summary.version,
                        summary.summary_id,
                        summary.parent_summary_id,
                        summary.covers_from_seq,
                        summary.covers_through_seq,
                        summary.source_hash,
                        summary.schema_version,
                        summary.prompt_version,
                        summary.model,
                        summary.artifact_digest,
                        summary.created_at_ns,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return summary

    async def build_context_projection(self, session_id: str) -> ContextProjection:
        active_events = await self._active_events(session_id)
        active_sequences = {event.seq for event in active_events}
        summary = await self._latest_active_summary(session_id, active_sequences)
        cutoff = summary.covers_through_seq if summary is not None else 0
        messages = tuple(
            event.payload["message"]
            for event in active_events
            if event.seq > cutoff
            and event.event_type in {"message_committed", "legacy_message_imported"}
            and isinstance(event.payload.get("message"), dict)
        )
        summary_text = None
        if summary is not None:
            summary_text = (await self.read_object(summary.artifact_digest)).decode("utf-8")
        head_seq = active_events[-1].seq if active_events else None
        return ContextProjection(
            session_id=session_id,
            head_seq=head_seq,
            event_ids=tuple(event.event_id for event in active_events),
            summary=summary,
            summary_text=summary_text,
            messages=messages,
        )

    async def import_legacy(self, legacy_db_path: Path) -> LegacyImportReport:
        path = Path(legacy_db_path).resolve(strict=True)
        snapshot = await asyncio.to_thread(self._read_legacy, path)
        source_key = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        imported_sessions = imported_events = skipped_sessions = 0
        db = self._require_db()
        for legacy in snapshot:
            session_id = legacy["session_id"]
            async with self._write_lock:
                await begin_immediate(db)
                try:
                    marker = await db.execute(
                        "SELECT 1 FROM legacy_import WHERE source_key = ? AND source_session_id = ?",
                        (source_key, session_id),
                    )
                    if await marker.fetchone() is not None:
                        skipped_sessions += 1
                        await db.rollback()
                        continue
                    existing = await db.execute(
                        "SELECT 1 FROM session WHERE session_id = ?", (session_id,)
                    )
                    if await existing.fetchone() is not None:
                        raise FactStoreError(
                            f"legacy session conflicts with an existing session: {session_id}"
                        )
                    now = time.time_ns()
                    await db.execute(
                        "INSERT INTO session "
                        "(session_id, mode, title, status, parent_session_id, parent_seq, "
                        "active_head_seq, created_at_ns, updated_at_ns) "
                        "VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                        (
                            session_id,
                            legacy["mode"],
                            legacy["title"],
                            legacy["status"],
                            now,
                            now,
                        ),
                    )
                    count = 0
                    for index, message in enumerate(legacy["messages"]):
                        await self._append_event_tx(
                            session_id,
                            "legacy_message_imported",
                            {
                                "message": message,
                                "legacy_unverified": True,
                                "source": {"table": "session_message", "index": index},
                            },
                            event_id=self._legacy_event_id(source_key, session_id, "message", index),
                        )
                        count += 1
                    for index, event_payload in enumerate(legacy["events"]):
                        await self._append_event_tx(
                            session_id,
                            "legacy_renderer_event_imported",
                            {
                                "event": event_payload,
                                "legacy_unverified": True,
                                "source": {"table": "session_event", "index": index},
                            },
                            event_id=self._legacy_event_id(source_key, session_id, "event", index),
                        )
                        count += 1
                    await db.execute(
                        "INSERT INTO legacy_import "
                        "(source_key, source_session_id, imported_event_count, imported_at_ns) "
                        "VALUES (?, ?, ?, ?)",
                        (source_key, session_id, count, time.time_ns()),
                    )
                    await db.commit()
                    imported_sessions += 1
                    imported_events += count
                except BaseException:
                    await db.rollback()
                    raise
        return LegacyImportReport(imported_sessions, imported_events, skipped_sessions)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _append_event_tx(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        artifact_digest: str | None = None,
        causal_event_id: str | None = None,
        attempt_id: str | None = None,
        event_id: str | None = None,
        schema_version: int = EVENT_SCHEMA_VERSION,
        parent_seq: int | None = None,
    ) -> FactEvent:
        db = self._require_db()
        session = await db.execute(
            "SELECT active_head_seq FROM session WHERE session_id = ?", (session_id,)
        )
        session_row = await session.fetchone()
        if session_row is None:
            raise FactStoreError(f"session does not exist: {session_id}")
        next_seq_cursor = await db.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM event WHERE session_id = ?", (session_id,)
        )
        seq = int((await next_seq_cursor.fetchone())[0])
        effective_parent = session_row[0] if parent_seq is None else parent_seq
        event = FactEvent(
            session_id=session_id,
            seq=seq,
            event_id=event_id or f"evt_{uuid.uuid4().hex}",
            event_type=event_type,
            payload=json.loads(self._canonical_json(payload)),
            parent_seq=int(effective_parent) if effective_parent is not None else None,
            artifact_digest=artifact_digest,
            causal_event_id=causal_event_id,
            attempt_id=attempt_id,
            schema_version=schema_version,
            created_at_ns=time.time_ns(),
        )
        await db.execute(
            "INSERT INTO event "
            "(session_id, seq, event_id, event_type, payload_json, parent_seq, artifact_digest, "
            "causal_event_id, attempt_id, schema_version, created_at_ns) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.session_id,
                event.seq,
                event.event_id,
                event.event_type,
                self._canonical_json(event.payload),
                event.parent_seq,
                event.artifact_digest,
                event.causal_event_id,
                event.attempt_id,
                event.schema_version,
                event.created_at_ns,
            ),
        )
        await db.execute(
            "UPDATE session SET active_head_seq = ?, updated_at_ns = ? WHERE session_id = ?",
            (event.seq, event.created_at_ns, session_id),
        )
        return event

    async def _active_events(self, session_id: str) -> tuple[FactEvent, ...]:
        cursor = await self._require_db().execute(
            """
            WITH RECURSIVE active(seq, parent_seq) AS (
                SELECT event.seq, event.parent_seq
                FROM session JOIN event
                  ON event.session_id = session.session_id
                 AND event.seq = session.active_head_seq
                WHERE session.session_id = ?
                UNION ALL
                SELECT event.seq, event.parent_seq
                FROM event JOIN active ON event.seq = active.parent_seq
                WHERE event.session_id = ?
            )
            SELECT event.session_id, event.seq, event.event_id, event.event_type,
                   event.payload_json, event.parent_seq, event.artifact_digest,
                   event.causal_event_id, event.attempt_id, event.schema_version,
                   event.created_at_ns
            FROM event JOIN active ON event.seq = active.seq
            WHERE event.session_id = ? ORDER BY event.seq
            """,
            (session_id, session_id, session_id),
        )
        return tuple(self._event_from_row(row) for row in await cursor.fetchall())

    async def _latest_active_summary(
        self, session_id: str, active_sequences: set[int]
    ) -> CompactionSummary | None:
        cursor = await self._require_db().execute(
            "SELECT session_id, version, summary_id, parent_summary_id, covers_from_seq, "
            "covers_through_seq, source_hash, schema_version, prompt_version, model, "
            "artifact_digest, created_at_ns FROM compaction_summary "
            "WHERE session_id = ? ORDER BY version DESC",
            (session_id,),
        )
        for row in await cursor.fetchall():
            summary = CompactionSummary(*row)
            covered = set(range(summary.covers_from_seq, summary.covers_through_seq + 1))
            if covered <= active_sequences:
                rows = await self._event_rows(
                    session_id, summary.covers_from_seq, summary.covers_through_seq
                )
                if self._source_hash(rows) == summary.source_hash:
                    return summary
        return None

    async def _event_rows(self, session_id: str, start: int, end: int) -> list[tuple[Any, ...]]:
        cursor = await self._require_db().execute(
            "SELECT session_id, seq, event_id, event_type, payload_json, parent_seq, "
            "artifact_digest, causal_event_id, attempt_id, schema_version, created_at_ns "
            "FROM event WHERE session_id = ? AND seq BETWEEN ? AND ? ORDER BY seq",
            (session_id, start, end),
        )
        return await cursor.fetchall()

    @classmethod
    def _source_hash(cls, rows: list[tuple[Any, ...]]) -> str:
        digest = hashlib.sha256()
        for row in rows:
            digest.update(cls._canonical_json(list(row)).encode("utf-8"))
            digest.update(b"\n")
        return f"sha256:{digest.hexdigest()}"

    @staticmethod
    def _event_from_row(row: tuple[Any, ...]) -> FactEvent:
        return FactEvent(
            session_id=row[0],
            seq=int(row[1]),
            event_id=row[2],
            event_type=row[3],
            payload=json.loads(row[4]),
            parent_seq=int(row[5]) if row[5] is not None else None,
            artifact_digest=row[6],
            causal_event_id=row[7],
            attempt_id=row[8],
            schema_version=int(row[9]),
            created_at_ns=int(row[10]),
        )

    @staticmethod
    def _read_legacy(path: Path) -> list[dict[str, Any]]:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            sessions = connection.execute(
                "SELECT id, mode, title, status FROM session ORDER BY created_at, id"
            ).fetchall()
            result = []
            for session in sessions:
                message_rows = connection.execute(
                    "SELECT content_json FROM session_message WHERE session_id = ? "
                    "ORDER BY message_index",
                    (session["id"],),
                ).fetchall()
                event_rows = connection.execute(
                    "SELECT event_json FROM session_event WHERE session_id = ? ORDER BY event_index",
                    (session["id"],),
                ).fetchall()
                result.append(
                    {
                        "session_id": session["id"],
                        "mode": session["mode"],
                        "title": session["title"],
                        "status": session["status"],
                        "messages": [json.loads(row[0]) for row in message_rows],
                        "events": [json.loads(row[0]) for row in event_rows],
                    }
                )
            return result
        except sqlite3.Error as exc:
            raise FactStoreError(f"legacy session database is not compatible: {exc}") from exc
        finally:
            connection.close()

    @staticmethod
    def _legacy_event_id(source_key: str, session_id: str, kind: str, index: int) -> str:
        value = f"{source_key}:{session_id}:{kind}:{index}"
        return f"evt_legacy_{uuid.uuid5(uuid.NAMESPACE_URL, value).hex}"

    def _object_path(self, digest: str) -> Path:
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise FactStoreError(f"invalid artifact digest: {digest}")
        hexadecimal = digest[7:]
        if any(character not in "0123456789abcdef" for character in hexadecimal):
            raise FactStoreError(f"invalid artifact digest: {digest}")
        return self.objects_root / hexadecimal[:2] / hexadecimal[2:]

    @classmethod
    def _write_object(cls, target: Path, content: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = target.read_bytes()
            if existing != content:
                raise ArtifactIntegrityError(f"content-address collision: {target}")
            return
        descriptor, raw_temp = tempfile.mkstemp(prefix=".object-", dir=target.parent)
        temp = Path(raw_temp)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
            cls._restrict_permissions(target)
        finally:
            temp.unlink(missing_ok=True)

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise FactStoreError("fact store is not open")
        return self._db

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _restrict_permissions(path: Path) -> None:
        try:
            path.chmod(0o600)
        except OSError:
            pass
