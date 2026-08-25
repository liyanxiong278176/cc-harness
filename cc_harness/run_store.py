"""SQLite/WAL durable store for Run Events and rebuildable projections."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from .artifacts import ArtifactStore
from .fact_store import default_user_data_dir, project_identity
from .run_events import EventActor, EventValidationError, RunEvent
from .run_model import Lease, Run
from .run_projection import ProjectionBuilder, ProjectionError, RunProjection


class RunStoreError(RuntimeError):
    """Base error for durable run store failures."""


class RunNotFound(RunStoreError):
    """Raised when a run stream does not exist."""


class SequenceConflict(RunStoreError):
    """Raised when a caller tries to append at a stale sequence."""


class LeaseFenceError(RunStoreError):
    """Raised when an event comes from an old worker lease epoch."""


class DuplicateEventError(RunStoreError):
    """Raised when an event ID has already been used."""


@dataclass(frozen=True)
class EventPage:
    events: tuple[RunEvent, ...]
    next_cursor: int | None


@dataclass(frozen=True)
class RunRecordView:
    run_id: str
    status: str
    sequence: int
    runtime_contract_digest: str
    parent_run_id: str | None = None
    predecessor_run_id: str | None = None


@dataclass(frozen=True)
class AppendEvent:
    event: RunEvent
    expected_sequence: int | None = None
    expected_lease_epoch: int | None = None
    snapshot: RunProjection | None = None


StoredEvent = RunEvent


class RunStore:
    """Single-logical-writer Run Store backed by one project SQLite database."""

    def __init__(
        self,
        project_root: Path,
        *,
        data_root: Path | None = None,
        identity_root: Path | None = None,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=True)
        stable_root = Path(identity_root or self.project_root).resolve(strict=True)
        self.project_id, self.canonical_root = project_identity(stable_root)
        self.data_root = Path(data_root) if data_root is not None else default_user_data_dir()
        self.state_dir = self.data_root / "projects" / self.project_id
        self.db_path = self.state_dir / "runtime.db"
        self.artifacts = artifact_store or ArtifactStore(self.state_dir / "objects")
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def open(self) -> "RunStore":
        if self._db is not None:
            return self
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path, timeout=30)
        await self._db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=30000;

            CREATE TABLE IF NOT EXISTS project_record (
                project_id TEXT PRIMARY KEY,
                canonical_root TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_record (
                run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES project_record(project_id),
                parent_run_id TEXT,
                predecessor_run_id TEXT,
                status TEXT NOT NULL,
                        runtime_contract_digest TEXT NOT NULL,
                        last_sequence INTEGER NOT NULL DEFAULT 0,
                        lease_epoch INTEGER NOT NULL DEFAULT 0,
                        projection_digest TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_event (
                run_id TEXT NOT NULL REFERENCES run_record(run_id),
                sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                occurred_at TEXT NOT NULL,
                actor_kind TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                causation_id TEXT,
                correlation_id TEXT NOT NULL,
                lease_epoch INTEGER NOT NULL,
                runtime_contract_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                artifact_refs_json TEXT NOT NULL,
                PRIMARY KEY (run_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS run_snapshot (
                run_id TEXT NOT NULL REFERENCES run_record(run_id),
                sequence INTEGER NOT NULL,
                projection_json TEXT NOT NULL,
                projection_digest TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (run_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS run_lease (
                run_id TEXT PRIMARY KEY REFERENCES run_record(run_id),
                worker_id TEXT NOT NULL,
                epoch INTEGER NOT NULL,
                acquired_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_approval (
                approval_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES run_record(run_id),
                action_id TEXT NOT NULL,
                action_args_digest TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                status TEXT NOT NULL,
                decided_by TEXT,
                decided_at TEXT
            );
            CREATE TABLE IF NOT EXISTS follow_up_queue (
                follow_up_run_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES run_record(run_id),
                predecessor_run_id TEXT,
                message_artifact TEXT NOT NULL,
                gate TEXT NOT NULL,
                status TEXT NOT NULL,
                queued_sequence INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS action_attempt (
                run_id TEXT NOT NULL REFERENCES run_record(run_id),
                action_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                tool_name TEXT NOT NULL,
                status TEXT NOT NULL,
                effect_class TEXT NOT NULL,
                contract_digest TEXT NOT NULL,
                lease_epoch INTEGER NOT NULL,
                arguments_artifact TEXT,
                result_artifact TEXT,
                error_kind TEXT,
                PRIMARY KEY (run_id, action_id, attempt)
            );

            CREATE TRIGGER IF NOT EXISTS run_event_no_update
            BEFORE UPDATE ON run_event BEGIN
                SELECT RAISE(ABORT, 'run events are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS run_event_no_delete
            BEFORE DELETE ON run_event BEGIN
                SELECT RAISE(ABORT, 'run events are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS run_snapshot_no_update
            BEFORE UPDATE ON run_snapshot BEGIN
                SELECT RAISE(ABORT, 'run snapshots are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS run_snapshot_no_delete
            BEFORE DELETE ON run_snapshot BEGIN
                SELECT RAISE(ABORT, 'run snapshots are immutable');
            END;
            """
        )
        # The rebuild is allowed to open a store created by an earlier
        # rehearsal. Keep schema evolution additive and transactional so a
        # restart never loses the lease fencing cursor or action references.
        for table, column, definition in (
            ("run_record", "lease_epoch", "INTEGER NOT NULL DEFAULT 0"),
            ("action_attempt", "arguments_artifact", "TEXT"),
        ):
            cursor = await self._db.execute(f"PRAGMA table_info({table})")
            columns = {str(row[1]) for row in await cursor.fetchall()}
            if column not in columns:
                await self._db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )
        now = time.time()
        await self._db.execute(
            "INSERT OR IGNORE INTO project_record(project_id, canonical_root, created_at) VALUES (?, ?, ?)",
            (self.project_id, self.canonical_root, now),
        )
        cursor = await self._db.execute(
            "SELECT canonical_root FROM project_record WHERE project_id = ?", (self.project_id,)
        )
        row = await cursor.fetchone()
        if row is None or row[0] != self.canonical_root:
            raise RunStoreError("project identity collision or mismatched project root")
        await self._db.commit()
        return self

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def create_run(self, run: Run) -> bool:
        db = self._require_db()
        async with self._write_lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """INSERT INTO run_record
                       (run_id, project_id, parent_run_id, predecessor_run_id, status,
                        runtime_contract_digest, last_sequence, projection_digest,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                    (
                        run.run_id,
                        self.project_id,
                        run.parent_run_id,
                        run.predecessor_run_id,
                        run.status.value,
                        run.runtime_contract.digest,
                        RunProjection.empty(run.run_id).digest,
                        run.created_at,
                        run.created_at,
                    ),
                )
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise RunStoreError(f"run already exists or project is invalid: {run.run_id}") from exc
        return cursor.rowcount == 1

    async def run_exists(self, run_id: str) -> bool:
        """Return whether a run record exists without rebuilding its projection."""
        cursor = await self._require_db().execute(
            "SELECT 1 FROM run_record WHERE run_id = ? LIMIT 1", (run_id,)
        )
        return await cursor.fetchone() is not None

    async def append(
        self,
        event_or_command: RunEvent | AppendEvent,
        *,
        expected_sequence: int | None = None,
        expected_lease_epoch: int | None = None,
        snapshot: RunProjection | None = None,
    ) -> StoredEvent:
        if isinstance(event_or_command, AppendEvent):
            command = event_or_command
            event = command.event
            expected_sequence = command.expected_sequence
            expected_lease_epoch = command.expected_lease_epoch
            snapshot = command.snapshot
        else:
            event = event_or_command
        db = self._require_db()
        async with self._write_lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                run_row = await self._run_row_tx(event.run_id)
                current_sequence = int(run_row["last_sequence"])
                if expected_sequence is not None and expected_sequence != current_sequence:
                    raise SequenceConflict(
                        f"expected sequence {expected_sequence}, current is {current_sequence}"
                    )
                if event.sequence != current_sequence + 1:
                    raise SequenceConflict(
                        f"event sequence {event.sequence}, expected {current_sequence + 1}"
                    )
                if current_sequence == 0 and event.event_type != "RunCreated":
                    raise RunStoreError("a run stream must begin with RunCreated")
                if current_sequence > 0 and event.event_type == "RunCreated":
                    raise RunStoreError("RunCreated can only be the first event")
                await self._validate_lease_tx(event, run_row, expected_lease_epoch)
                current_projection = await self._projection_tx(event.run_id, current_sequence)
                if event.event_type != "RunRuntimeMigrated":
                    if event.runtime_contract_digest != str(run_row["runtime_contract_digest"]):
                        raise LeaseFenceError("event runtime contract digest is stale")
                elif str(event.payload["previous_runtime_contract_digest"]) != str(
                    run_row["runtime_contract_digest"]
                ):
                    raise LeaseFenceError("runtime migration does not start from the pinned contract")
                new_projection = ProjectionBuilder().rebuild([event], snapshot=current_projection)
                if snapshot is not None:
                    if snapshot.run_id != new_projection.run_id:
                        raise RunStoreError("snapshot run_id does not match event stream")
                    if snapshot.sequence != new_projection.sequence:
                        raise RunStoreError("snapshot must cover the appended event")
                    if snapshot.digest != new_projection.digest:
                        raise RunStoreError("snapshot digest does not match projection")
                await db.execute(
                    """INSERT INTO run_event
                       (run_id, sequence, event_id, event_type, schema_version, occurred_at,
                        actor_kind, actor_id, causation_id, correlation_id, lease_epoch,
                        runtime_contract_digest, payload_json, artifact_refs_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.run_id,
                        event.sequence,
                        event.event_id,
                        event.event_type,
                        event.schema_version,
                        event.occurred_at,
                        event.actor.kind,
                        event.actor.actor_id,
                        event.causation_id,
                        event.correlation_id,
                        event.lease_epoch,
                        event.runtime_contract_digest,
                        json.dumps(event.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        json.dumps(list(event.artifact_refs), ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                runtime_digest = (
                    str(event.payload["new_runtime_contract_digest"])
                    if event.event_type == "RunRuntimeMigrated"
                    else str(run_row["runtime_contract_digest"])
                )
                await db.execute(
                    """UPDATE run_record
                       SET status = ?, runtime_contract_digest = ?, last_sequence = ?,
                           projection_digest = ?, updated_at = ?
                       WHERE run_id = ?""",
                    (
                        new_projection.status.value,
                        runtime_digest,
                        new_projection.sequence,
                        new_projection.digest,
                        time.time(),
                        event.run_id,
                    ),
                )
                await self._persist_projection_tx(new_projection)
                await self._persist_lease_tx(event, run_row)
                if snapshot is not None:
                    await self._insert_snapshot_tx(snapshot)
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                if "event_id" in str(exc).lower():
                    raise DuplicateEventError(f"event id already exists: {event.event_id}") from exc
                raise RunStoreError(str(exc)) from exc
            except BaseException:
                await db.rollback()
                raise
        return event

    async def read(self, run_id: str, *, after: int = 0, limit: int = 200) -> EventPage:
        if after < 0 or limit < 1:
            raise RunStoreError("after must be non-negative and limit must be positive")
        db = self._require_db()
        await self._ensure_run(run_id)
        cursor = await db.execute(
            """SELECT run_id, sequence, event_id, event_type, schema_version, occurred_at,
                      actor_kind, actor_id, causation_id, correlation_id, lease_epoch,
                      runtime_contract_digest, payload_json, artifact_refs_json
               FROM run_event WHERE run_id = ? AND sequence > ? ORDER BY sequence LIMIT ?""",
            (run_id, after, limit + 1),
        )
        rows = await cursor.fetchall()
        has_more = len(rows) > limit
        events = tuple(self._event_from_row(row) for row in rows[:limit])
        return EventPage(events, events[-1].sequence if has_more and events else None)

    async def load_projection(self, run_id: str) -> RunProjection:
        db = self._require_db()
        await self._ensure_run(run_id)
        async with self._write_lock:
            projection = await self._projection_tx(run_id)
        cursor = await db.execute(
            "SELECT projection_digest, last_sequence FROM run_record WHERE run_id = ?", (run_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise RunNotFound(run_id)
        if int(row[1]) != projection.sequence or row[0] != projection.digest:
            raise RunStoreError("stored projection cursor does not match event rebuild")
        return projection

    async def save_snapshot(self, snapshot: RunProjection) -> None:
        db = self._require_db()
        async with self._write_lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                run_row = await self._run_row_tx(snapshot.run_id)
                if snapshot.sequence > int(run_row["last_sequence"]):
                    raise RunStoreError("snapshot sequence is ahead of the event stream")
                await self._insert_snapshot_tx(snapshot)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def snapshot_sequences(self, run_id: str) -> tuple[int, ...]:
        await self._ensure_run(run_id)
        cursor = await self._require_db().execute(
            "SELECT sequence FROM run_snapshot WHERE run_id = ? ORDER BY sequence", (run_id,)
        )
        return tuple(int(row[0]) for row in await cursor.fetchall())

    async def list_runs(self, statuses: set[str] | None = None) -> tuple[RunRecordView, ...]:
        query = (
            "SELECT run_id, status, last_sequence, runtime_contract_digest, "
            "parent_run_id, predecessor_run_id "
            "FROM run_record"
        )
        params: tuple[Any, ...] = ()
        if statuses:
            ordered = tuple(sorted(statuses))
            placeholders = ",".join("?" for _ in ordered)
            query += f" WHERE status IN ({placeholders})"
            params = ordered
        query += " ORDER BY updated_at, run_id"
        cursor = await self._require_db().execute(query, params)
        return tuple(
            RunRecordView(
                str(row[0]),
                str(row[1]),
                int(row[2]),
                str(row[3]),
                (str(row[4]) if row[4] else None),
                (str(row[5]) if row[5] else None),
            )
            for row in await cursor.fetchall()
        )

    async def load_run_record(self, run_id: str) -> RunRecordView:
        """Load durable run lineage used by context recall authorization."""

        cursor = await self._require_db().execute(
            """SELECT run_id, status, last_sequence, runtime_contract_digest,
                      parent_run_id, predecessor_run_id
               FROM run_record WHERE run_id = ?""",
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RunNotFound(run_id)
        return RunRecordView(
            str(row[0]),
            str(row[1]),
            int(row[2]),
            str(row[3]),
            (str(row[4]) if row[4] else None),
            (str(row[5]) if row[5] else None),
        )

    async def current_lease(self, run_id: str) -> Lease | None:
        await self._ensure_run(run_id)
        cursor = await self._require_db().execute(
            "SELECT run_id, worker_id, epoch, acquired_at, expires_at FROM run_lease WHERE run_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return Lease(str(row[0]), str(row[1]), int(row[2]), float(row[3]), float(row[4]))

    async def release_lease(self, run_id: str, epoch: int) -> bool:
        db = self._require_db()
        async with self._write_lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "DELETE FROM run_lease WHERE run_id = ? AND epoch = ?", (run_id, epoch)
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return cursor.rowcount == 1

    async def _projection_tx(self, run_id: str, through_sequence: int | None = None) -> RunProjection:
        db = self._require_db()
        current_sequence = through_sequence
        if current_sequence is None:
            row = await self._run_row_tx(run_id)
            current_sequence = int(row["last_sequence"])
        snapshot_cursor = await db.execute(
            """SELECT sequence, projection_json, projection_digest FROM run_snapshot
               WHERE run_id = ? AND sequence <= ? ORDER BY sequence DESC LIMIT 1""",
            (run_id, current_sequence),
        )
        snapshot_row = await snapshot_cursor.fetchone()
        snapshot: RunProjection | None = None
        start_sequence = 0
        if snapshot_row is not None:
            snapshot = RunProjection.from_dict(json.loads(snapshot_row[1]))
            if snapshot.digest != snapshot_row[2]:
                raise RunStoreError("snapshot digest mismatch")
            start_sequence = snapshot.sequence
        cursor = await db.execute(
            """SELECT run_id, sequence, event_id, event_type, schema_version, occurred_at,
                      actor_kind, actor_id, causation_id, correlation_id, lease_epoch,
                      runtime_contract_digest, payload_json, artifact_refs_json
               FROM run_event WHERE run_id = ? AND sequence > ? AND sequence <= ?
               ORDER BY sequence""",
            (run_id, start_sequence, current_sequence),
        )
        events = tuple(self._event_from_row(row) for row in await cursor.fetchall())
        if snapshot is None and not events:
            return RunProjection.empty(run_id)
        try:
            return ProjectionBuilder().rebuild(events, snapshot=snapshot, run_id=run_id)
        except (ProjectionError, EventValidationError) as exc:
            raise RunStoreError(f"event projection failed for {run_id}: {exc}") from exc

    async def _persist_projection_tx(self, projection: RunProjection) -> None:
        db = self._require_db()
        await db.execute("DELETE FROM action_attempt WHERE run_id = ?", (projection.run_id,))
        for action in projection.actions:
            effect = action.effect_class.value if hasattr(action.effect_class, "value") else action.effect_class
            status = action.status.value if hasattr(action.status, "value") else action.status
            await db.execute(
                """INSERT INTO action_attempt
                   (run_id, action_id, attempt, tool_name, status, effect_class,
                    contract_digest, lease_epoch, arguments_artifact, result_artifact, error_kind)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    projection.run_id,
                    action.action_id,
                    action.attempt,
                    action.tool_name,
                    status,
                    effect,
                    action.contract_digest,
                    action.lease_epoch,
                    action.arguments_artifact,
                    action.result_artifact,
                    action.error_kind,
                ),
            )
        await db.execute("DELETE FROM run_approval WHERE run_id = ?", (projection.run_id,))
        for approval in projection.approvals:
            await db.execute(
                """INSERT INTO run_approval
                   (approval_id, run_id, action_id, action_args_digest, scope_json,
                    status, decided_by, decided_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    approval.approval_id,
                    approval.run_id,
                    approval.action_id,
                    approval.action_args_digest,
                    json.dumps(list(approval.scope), separators=(",", ":")),
                    approval.status.value,
                    approval.decided_by,
                    approval.decided_at,
                ),
            )
        await db.execute("DELETE FROM follow_up_queue WHERE run_id = ?", (projection.run_id,))
        for item in projection.queue:
            await db.execute(
                """INSERT INTO follow_up_queue
                   (follow_up_run_id, run_id, predecessor_run_id, message_artifact,
                    gate, status, queued_sequence)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.follow_up_run_id,
                    projection.run_id,
                    item.predecessor_run_id,
                    item.message_artifact,
                    item.gate,
                    item.status,
                    item.queued_sequence,
                ),
            )

    async def _persist_lease_tx(self, event: RunEvent, run_row: Any) -> None:
        db = self._require_db()
        if event.event_type == "RunClaimed":
            await db.execute(
                "UPDATE run_record SET lease_epoch = ? WHERE run_id = ?",
                (event.lease_epoch, event.run_id),
            )
            expires_at = float(event.payload.get("expires_at", time.time() + 60.0))
            await db.execute(
                """INSERT INTO run_lease(run_id, worker_id, epoch, acquired_at, expires_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET worker_id=excluded.worker_id,
                   epoch=excluded.epoch, acquired_at=excluded.acquired_at,
                   expires_at=excluded.expires_at""",
                (
                    event.run_id,
                    str(event.payload["worker_id"]),
                    event.lease_epoch,
                    time.time(),
                    expires_at,
                ),
            )
        elif event.event_type == "WorkerHeartbeat":
            await db.execute(
                "UPDATE run_lease SET expires_at = ? WHERE run_id = ? AND epoch = ?",
                (float(event.payload.get("expires_at", time.time() + 60.0)), event.run_id, event.lease_epoch),
            )
        elif event.event_type in {"RunResumed", "RunBlocked", "RunStalled", "RunCancelled", "ApprovalRequested"}:
            if event.event_type != "RunResumed":
                await db.execute("DELETE FROM run_lease WHERE run_id = ?", (event.run_id,))

    async def _insert_snapshot_tx(self, snapshot: RunProjection) -> None:
        db = self._require_db()
        encoded = json.dumps(snapshot.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        try:
            await db.execute(
                """INSERT INTO run_snapshot
                   (run_id, sequence, projection_json, projection_digest, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (snapshot.run_id, snapshot.sequence, encoded, snapshot.digest, time.time()),
            )
        except aiosqlite.IntegrityError as exc:
            cursor = await db.execute(
                "SELECT projection_digest FROM run_snapshot WHERE run_id = ? AND sequence = ?",
                (snapshot.run_id, snapshot.sequence),
            )
            row = await cursor.fetchone()
            if row is None or row[0] != snapshot.digest:
                raise RunStoreError("snapshot already exists with a different digest") from exc

    async def _validate_lease_tx(
        self, event: RunEvent, run_row: Any, expected_lease_epoch: int | None
    ) -> None:
        current_epoch = int(run_row["lease_epoch"] or 0)
        if expected_lease_epoch is not None and expected_lease_epoch != current_epoch:
            raise LeaseFenceError(
                f"expected lease epoch {expected_lease_epoch}, current is {current_epoch}"
            )
        if event.event_type == "RunClaimed":
            if event.lease_epoch <= current_epoch:
                raise LeaseFenceError("new RunClaimed event must advance the lease epoch")
            return
        if event.lease_epoch > 0 and event.lease_epoch != current_epoch:
            raise LeaseFenceError("event lease epoch is stale")
        if event.lease_epoch == 0 and current_epoch > 0 and event.actor.kind == "worker":
            raise LeaseFenceError("worker event must carry the active lease epoch")
        if event.lease_epoch > 0:
            cursor = await self._require_db().execute(
                "SELECT worker_id, epoch FROM run_lease WHERE run_id = ?", (event.run_id,)
            )
            lease_row = await cursor.fetchone()
            if lease_row is None or int(lease_row[1]) != event.lease_epoch:
                raise LeaseFenceError("event lease epoch is no longer active")
            if event.actor.kind == "worker" and str(lease_row[0]) != event.actor.actor_id:
                raise LeaseFenceError("worker is not the owner of the active lease")

    async def _run_row_tx(self, run_id: str) -> Any:
        cursor = await self._require_db().execute(
            """SELECT run_id, status, runtime_contract_digest, last_sequence,
                      projection_digest, lease_epoch
               FROM run_record WHERE run_id = ?""",
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RunNotFound(run_id)
        columns = ["run_id", "status", "runtime_contract_digest", "last_sequence", "projection_digest", "lease_epoch"]
        return dict(zip(columns, row, strict=True))

    async def _ensure_run(self, run_id: str) -> None:
        await self._run_row_tx(run_id)

    def _event_from_row(self, row: Any) -> RunEvent:
        try:
            return RunEvent.create(
                event_id=str(row[2]),
                run_id=str(row[0]),
                sequence=int(row[1]),
                event_type=str(row[3]),
                actor=EventActor(str(row[6]), str(row[7])),
                runtime_contract_digest=str(row[11]),
                payload=json.loads(row[12]),
                lease_epoch=int(row[10]),
                causation_id=row[8],
                correlation_id=str(row[9]),
                occurred_at=str(row[5]),
                artifact_refs=tuple(json.loads(row[13])),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RunStoreError("stored event is malformed") from exc

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RunStoreError("RunStore is not open")
        return self._db


__all__ = [
    "AppendEvent",
    "DuplicateEventError",
    "EventPage",
    "LeaseFenceError",
    "RunNotFound",
    "RunRecordView",
    "RunStore",
    "RunStoreError",
    "SequenceConflict",
    "StoredEvent",
]
