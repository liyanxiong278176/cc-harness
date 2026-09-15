"""SQLite/WAL durable store for Run Events and rebuildable projections."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager, suppress
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

import aiosqlite

from .artifacts import ArtifactStore, GarbageCollectionReport
from .fact_store import default_user_data_dir, project_identity
from .run_events import EventActor, EventValidationError, RunEvent
from .run_model import Lease, ResourceLease, Run, SupervisorLease, digest_json
from .run_projection import ProjectionBuilder, ProjectionError, RunProjection
from .sqlite_utils import begin_immediate


_LOGGER = logging.getLogger(__name__)


class RunStoreError(RuntimeError):
    """Base error for durable run store failures."""


class RunNotFound(RunStoreError):
    """Raised when a run stream does not exist."""


class SequenceConflict(RunStoreError):
    """Raised when a caller tries to append at a stale sequence."""


class LeaseFenceError(RunStoreError):
    """Raised when an event comes from an old worker lease epoch."""


class SupervisorLeaseConflict(RunStoreError):
    """Raised when another live process owns project scheduler leadership."""


class SupervisorLeaseFenceError(LeaseFenceError):
    """Raised when a supervisor heartbeat/release comes from a stale epoch."""


class ResourceLeaseConflict(RunStoreError):
    """Raised when an action resource overlaps another live action lease."""

    def __init__(self, run_id: str, conflicts: tuple[tuple[str, str, str], ...]) -> None:
        self.run_id = run_id
        self.conflicts = conflicts
        rendered = ", ".join(
            f"{resource_key} ({mode}, run={owner_run_id})"
            for resource_key, mode, owner_run_id in conflicts
        )
        super().__init__(f"resource lease conflict for run {run_id}: {rendered}")


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
        self._open_lock = asyncio.Lock()
        # Legacy snapshots can be replayed safely, but a terminal Run may be
        # read many times by the supervisor.  Keep the migration diagnostic
        # once per snapshot so compatibility does not turn into log spam.
        self._legacy_projection_warnings: set[tuple[str, int, str]] = set()

    async def open(self) -> "RunStore":
        async with self._open_lock:
            if self._db is not None:
                return self
            self.state_dir.mkdir(parents=True, exist_ok=True)
            # Keep the connection in SQLite autocommit mode.  Every public
            # operation below owns an explicit transaction, which prevents an
            # implicit transaction from surviving task cancellation and later
            # making a cleanup/read path fail with "transaction within a
            # transaction".
            self._db = await aiosqlite.connect(
                self.db_path, timeout=30, isolation_level=None
            )
            await self._db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=30000;
            PRAGMA wal_autocheckpoint=1000;

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
            -- A WebUI deletion is a presentation-level tombstone rather than
            -- a destructive rewrite of the immutable event stream.  Keeping
            -- the stream/snapshots preserves auditability while excluding the
            -- root and its descendants from future scheduling and history
            -- listings.
            CREATE TABLE IF NOT EXISTS run_tombstone (
                run_id TEXT PRIMARY KEY REFERENCES run_record(run_id),
                project_id TEXT NOT NULL REFERENCES project_record(project_id),
                deleted_at REAL NOT NULL
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
            CREATE TABLE IF NOT EXISTS project_supervisor_lease (
                project_id TEXT PRIMARY KEY REFERENCES project_record(project_id),
                owner_id TEXT NOT NULL,
                epoch INTEGER NOT NULL,
                acquired_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_resource_lease (
                lease_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES project_record(project_id),
                run_id TEXT NOT NULL REFERENCES run_record(run_id),
                action_id TEXT,
                resource_key TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('shared', 'exclusive')),
                lease_epoch INTEGER NOT NULL,
                acquired_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS run_resource_lease_active_idx
                ON run_resource_lease(project_id, expires_at);
            CREATE INDEX IF NOT EXISTS run_resource_lease_run_idx
                ON run_resource_lease(run_id, lease_epoch);
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
                    idempotency_key TEXT NOT NULL DEFAULT '',
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
                ("action_attempt", "idempotency_key", "TEXT NOT NULL DEFAULT ''"),
            ):
                cursor = await self._db.execute(f"PRAGMA table_info({table})")
                columns = {str(row[1]) for row in await cursor.fetchall()}
                if column not in columns:
                    await self._db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                    )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS action_attempt_idempotency_idx "
                "ON action_attempt(run_id, idempotency_key)"
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
        async with self._open_lock:
            async with self._write_lock:
                if self._db is not None:
                    await self._rollback_open_transaction(self._db)
                    await self._db.close()
                    self._db = None

    async def referenced_artifact_digests(self) -> set[str]:
        """Collect every content digest reachable from the durable runtime.

        Object publication intentionally happens before the event commit.  If a
        process dies in that gap the object is an orphan; garbage collection
        must therefore derive roots from immutable event/snapshot payloads and
        never from a best-effort in-memory list.
        """

        db = self._require_db()
        digest_re = re.compile(r"^sha256:[0-9a-f]{64}$")
        found: set[str] = set()

        def collect(value: Any) -> None:
            if isinstance(value, str):
                if digest_re.fullmatch(value):
                    found.add(value)
                return
            if isinstance(value, Mapping):
                for item in value.values():
                    collect(item)
                return
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    collect(item)

        async with self._write_lock:
            async with self._transaction(db, write=False):
                cursor = await db.execute(
                    "SELECT payload_json, artifact_refs_json FROM run_event"
                )
                rows = await cursor.fetchall()
                snapshot_cursor = await db.execute(
                    "SELECT projection_json FROM run_snapshot"
                )
                snapshot_rows = await snapshot_cursor.fetchall()
        for payload_json, refs_json in rows:
            for raw in (payload_json, refs_json):
                try:
                    collect(json.loads(raw))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
        for (projection_json,) in snapshot_rows:
            try:
                collect(json.loads(projection_json))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return found

    async def collect_artifact_garbage(
        self,
        *,
        now: float | None = None,
        grace_period_seconds: float | None = None,
    ) -> GarbageCollectionReport:
        """Delete only old objects that are unreachable from durable state."""

        referenced = await self.referenced_artifact_digests()
        # Temp files are not addressable artifacts and can only be abandoned by
        # an interrupted atomic publish; clean them independently.
        self.artifacts.cleanup_temporary_files(
            older_than_seconds=(
                self.artifacts.grace_period_seconds
                if grace_period_seconds is None
                else max(0.0, float(grace_period_seconds))
            )
        )
        return self.artifacts.collect_garbage(
            referenced,
            now=now,
            grace_period_seconds=grace_period_seconds,
        )

    @staticmethod
    async def _rollback_open_transaction(db: aiosqlite.Connection) -> None:
        """Best-effort rollback used at cancellation/close boundaries.

        A worker can be cancelled at any await point, including immediately
        after SQLite has accepted ``BEGIN IMMEDIATE``.  The previous code put
        ``begin_immediate`` before its ``try`` block, so that cancellation left
        the connection inside a transaction and the next ``BEGIN`` raised a
        nested-transaction error.  Rollback is idempotent in autocommit mode;
        shielding it lets the aiosqlite worker finish even when the caller is
        being cancelled.
        """

        # Do not inspect ``db.in_transaction`` before queueing the rollback.
        # That property is read directly from sqlite's connection while
        # aiosqlite executes statements on its worker thread, so it can be
        # stale when a cancelled ``BEGIN`` is still queued.  In that race the
        # old check returned early, the queued BEGIN then opened a transaction,
        # and the next reader raised ``cannot start a transaction within a
        # transaction``.  Queueing an unconditional rollback establishes the
        # ordering on the aiosqlite worker and is harmless when no transaction
        # is active.
        rollback_task = asyncio.create_task(db.rollback())
        try:
            await asyncio.shield(rollback_task)
        except aiosqlite.OperationalError as exc:
            if "no transaction is active" not in str(exc).lower():
                raise
        except BaseException:
            # Preserve the original cancellation/error.  The shielded task is
            # still allowed to drain on the aiosqlite worker thread.
            with suppress(BaseException):
                await asyncio.shield(rollback_task)

    @asynccontextmanager
    async def _transaction(
        self, db: aiosqlite.Connection, *, write: bool
    ) -> AsyncIterator[aiosqlite.Connection]:
        """Own one explicit transaction and always clean it up.

        ``RunStore`` serializes operations with ``_write_lock``; therefore an
        already-open transaction can only be a leftover from a cancelled
        operation.  Roll it back before starting, and roll back again on every
        exceptional exit (including ``asyncio.CancelledError``).
        """

        await self._rollback_open_transaction(db)
        try:
            if write:
                await begin_immediate(db)
            else:
                await db.execute("BEGIN")
            try:
                yield db
            except BaseException:
                await self._rollback_open_transaction(db)
                raise
            else:
                try:
                    await db.commit()
                except BaseException:
                    await self._rollback_open_transaction(db)
                    raise
        except BaseException:
            # Covers cancellation/failure while BEGIN itself is being queued.
            await self._rollback_open_transaction(db)
            raise

    async def create_run(self, run: Run) -> bool:
        db = self._require_db()
        async with self._write_lock:
            try:
                async with self._transaction(db, write=True):
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
            except aiosqlite.IntegrityError as exc:
                raise RunStoreError(f"run already exists or project is invalid: {run.run_id}") from exc
        return cursor.rowcount == 1

    async def run_exists(self, run_id: str) -> bool:
        """Return whether a run record exists without rebuilding its projection."""
        db = self._require_db()
        async with self._write_lock:
            async with self._transaction(db, write=False):
                cursor = await db.execute(
                    "SELECT 1 FROM run_record WHERE run_id = ? LIMIT 1", (run_id,)
                )
                exists = await cursor.fetchone() is not None
                return exists

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
            try:
                async with self._transaction(db, write=True):
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
            except aiosqlite.IntegrityError as exc:
                if "event_id" in str(exc).lower():
                    raise DuplicateEventError(f"event id already exists: {event.event_id}") from exc
                raise RunStoreError(str(exc)) from exc
        return event

    async def read(self, run_id: str, *, after: int = 0, limit: int = 200) -> EventPage:
        if after < 0 or limit < 1:
            raise RunStoreError("after must be non-negative and limit must be positive")
        db = self._require_db()
        async with self._write_lock:
            async with self._transaction(db, write=False):
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
        async with self._write_lock:
            # Rebuilding the projection and validating the denormalized cursor
            # must observe one SQLite snapshot.  A second, short write
            # transaction repairs only derived metadata after a legacy schema
            # replay.  The immutable event stream remains the authority.
            #
            # The retry handles another process appending an event between the
            # read and repair transactions.  Without it a perfectly healthy
            # concurrent append could make this call return a stale projection
            # (or, worse, overwrite the newer cursor).
            for _attempt in range(3):
                repair: tuple[int, str | None, RunProjection, str] | None = None
                async with self._transaction(db, write=False):
                    row = await self._run_row_tx(run_id)
                    expected_sequence = int(row["last_sequence"])
                    expected_digest = row["projection_digest"]
                    diagnostics: list[str] = []
                    projection = await self._projection_tx(
                        run_id,
                        expected_sequence,
                        diagnostics=diagnostics,
                    )
                    if projection.sequence != expected_sequence:
                        raise RunStoreError(
                            "stored projection cursor does not match event rebuild"
                        )
                    if expected_digest != projection.digest:
                        legacy_snapshot = next(
                            (
                                item
                                for item in diagnostics
                                if item.startswith("legacy_snapshot|")
                            ),
                            None,
                        )
                        if legacy_snapshot is None:
                            # ``projection_digest`` is a denormalized cursor,
                            # not part of the immutable event stream.  Older
                            # runtimes could leave it stale after a process
                            # crash (or after a projection schema upgrade),
                            # even though replaying the verified events gives
                            # a valid projection at the exact stored
                            # sequence.  Refusing to repair that derived field
                            # strands the supervisor in a retry loop and
                            # leaves WebUI messages permanently queued.
                            #
                            # A sequence mismatch is still fail-closed above:
                            # this branch is reached only when the event
                            # stream replayed to ``expected_sequence`` and
                            # therefore provides an authoritative repair
                            # target.  Snapshot byte tampering also still
                            # raises from ``_projection_tx`` before reaching
                            # this branch.
                            repair = (
                                expected_sequence,
                                expected_digest,
                                projection,
                                "derived_cursor_rebuild",
                            )
                            warning_key = (run_id, expected_sequence, expected_digest or "")
                            if warning_key not in self._legacy_projection_warnings:
                                self._legacy_projection_warnings.add(warning_key)
                                _LOGGER.warning(
                                    "rebuilt projection cursor for run_id=%s at sequence=%s; "
                                    "repairing stale derived digest",
                                    run_id,
                                    projection.sequence,
                                )
                        else:
                            _, legacy_digest, legacy_sequence = legacy_snapshot.split("|", 2)
                            # The raw snapshot digest was verified before this
                            # diagnostic was emitted.  Its dataclass digest is
                            # different only because the projection schema grew;
                            # replaying events from sequence zero is authoritative.
                            repair = (
                                expected_sequence,
                                expected_digest,
                                projection,
                                f"legacy_snapshot:{legacy_digest}:{legacy_sequence}",
                            )
                            warning_key = (run_id, int(legacy_sequence), legacy_digest)
                            if warning_key not in self._legacy_projection_warnings:
                                self._legacy_projection_warnings.add(warning_key)
                                _LOGGER.warning(
                                    "rebuilt legacy projection for run_id=%s at sequence=%s; "
                                    "repairing derived cursor",
                                    run_id,
                                    projection.sequence,
                                )
                if repair is None:
                    return projection
                if await self._repair_projection_cursor(run_id, *repair):
                    return projection
                # A concurrent append or repair won the race.  Rebuild against
                # the new cursor before returning to the caller.
            raise RunStoreError("projection changed while rebuilding; retry the read")

    async def _repair_projection_cursor(
        self,
        run_id: str,
        expected_sequence: int,
        expected_digest: str | None,
        projection: RunProjection,
        reason: str,
    ) -> bool:
        """Repair denormalized projection metadata after a safe event replay.

        Snapshots and events are append-only and are never rewritten here.
        Only indexes/cursors that are derivable from the immutable stream are
        refreshed.  ``False`` tells the caller that another process changed
        the run while the read transaction was closing, so it should rebuild.
        """

        db = self._require_db()
        async with self._transaction(db, write=True):
            row = await self._run_row_tx(run_id)
            current_sequence = int(row["last_sequence"])
            current_digest = row["projection_digest"]
            if current_sequence != expected_sequence:
                return False
            if current_digest == projection.digest:
                return True
            if current_digest != expected_digest:
                return False
            await db.execute(
                """UPDATE run_record
                   SET status = ?, runtime_contract_digest = COALESCE(?, runtime_contract_digest),
                       projection_digest = ?, updated_at = ?
                   WHERE run_id = ? AND last_sequence = ?""",
                (
                    projection.status.value,
                    projection.runtime_contract_digest,
                    projection.digest,
                    time.time(),
                    run_id,
                    expected_sequence,
                ),
            )
            # These tables are rebuildable indexes, not independent sources of
            # truth.  Refreshing them keeps approvals/actions/follow-ups
            # consistent for the WebUI and supervisor after a schema upgrade.
            await self._persist_projection_tx(projection)
            _LOGGER.info(
                "repaired projection cursor for run_id=%s sequence=%s (%s)",
                run_id,
                expected_sequence,
                reason,
            )
            return True

    async def save_snapshot(self, snapshot: RunProjection) -> None:
        db = self._require_db()
        async with self._write_lock:
            async with self._transaction(db, write=True):
                run_row = await self._run_row_tx(snapshot.run_id)
                if snapshot.sequence > int(run_row["last_sequence"]):
                    raise RunStoreError("snapshot sequence is ahead of the event stream")
                await self._insert_snapshot_tx(snapshot)

    async def checkpoint(self, run_id: str) -> RunProjection:
        """Materialize an atomic projection checkpoint for crash recovery.

        The checkpoint is taken under the same writer transaction as the
        denormalized cursor read.  This avoids the old race where a supervisor
        could observe a snapshot from one sequence together with a cursor from
        another and incorrectly mark a healthy run as corrupt.
        """

        db = self._require_db()
        async with self._write_lock:
            async with self._transaction(db, write=True):
                row = await self._run_row_tx(run_id)
                projection = await self._projection_tx(run_id, int(row["last_sequence"]))
                await self._insert_snapshot_tx(projection)
                return projection

    async def snapshot_sequences(self, run_id: str) -> tuple[int, ...]:
        db = self._require_db()
        async with self._write_lock:
            async with self._transaction(db, write=False):
                await self._ensure_run(run_id)
                cursor = await db.execute(
                    "SELECT sequence FROM run_snapshot WHERE run_id = ? ORDER BY sequence", (run_id,)
                )
                values = tuple(int(row[0]) for row in await cursor.fetchall())
                return values

    async def list_runs(self, statuses: set[str] | None = None) -> tuple[RunRecordView, ...]:
        query = (
            "SELECT run_id, status, last_sequence, runtime_contract_digest, "
            "parent_run_id, predecessor_run_id "
            "FROM run_record "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM run_tombstone hidden WHERE hidden.run_id = run_record.run_id"
            ")"
        )
        params: tuple[Any, ...] = ()
        if statuses:
            ordered = tuple(sorted(statuses))
            placeholders = ",".join("?" for _ in ordered)
            query += f" AND status IN ({placeholders})"
            params = ordered
        query += " ORDER BY updated_at, run_id"
        db = self._require_db()
        async with self._write_lock:
            async with self._transaction(db, write=False):
                cursor = await db.execute(query, params)
                rows = await cursor.fetchall()
                views = tuple(
                    RunRecordView(
                        str(row[0]),
                        str(row[1]),
                        int(row[2]),
                        str(row[3]),
                        (str(row[4]) if row[4] else None),
                        (str(row[5]) if row[5] else None),
                    )
                    for row in rows
                )
                return views

    async def load_run_record(self, run_id: str) -> RunRecordView:
        """Load durable run lineage used by context recall authorization."""
        db = self._require_db()
        async with self._write_lock:
            async with self._transaction(db, write=False):
                cursor = await db.execute(
                    """SELECT run_id, status, last_sequence, runtime_contract_digest,
                              parent_run_id, predecessor_run_id
                       FROM run_record
                       WHERE run_id = ?
                         AND NOT EXISTS (
                             SELECT 1 FROM run_tombstone hidden
                             WHERE hidden.run_id = run_record.run_id
                         )""",
                    (run_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise RunNotFound(run_id)
                view = RunRecordView(
                    str(row[0]),
                    str(row[1]),
                    int(row[2]),
                    str(row[3]),
                    (str(row[4]) if row[4] else None),
                    (str(row[5]) if row[5] else None),
                )
                return view

    async def tombstone_run_tree(self, root_run_id: str) -> tuple[str, ...]:
        """Hide a root conversation and all descendants without rewriting facts.

        The WebUI's delete action must not issue ``DELETE`` against
        ``run_event``/``run_snapshot``: both tables are intentionally
        immutable and are the audit source of truth.  A tombstone is a small,
        transactional index that makes the whole run tree disappear from
        scheduling and user-facing history while retaining the evidence for
        later audit/forensics.
        """

        root = str(root_run_id).strip()
        if not root:
            raise RunNotFound(root_run_id)
        db = self._require_db()
        async with self._write_lock:
            async with self._transaction(db, write=True):
                cursor = await db.execute(
                    """WITH RECURSIVE run_tree(run_id) AS (
                           SELECT run_id FROM run_record
                           WHERE run_id = ? AND parent_run_id IS NULL
                           UNION ALL
                           SELECT child.run_id
                           FROM run_record child
                           JOIN run_tree parent ON child.parent_run_id = parent.run_id
                       )
                       SELECT run_id FROM run_tree""",
                    (root,),
                )
                run_ids = tuple(str(row[0]) for row in await cursor.fetchall())
                if not run_ids:
                    raise RunNotFound(root_run_id)
                now = time.time()
                await db.executemany(
                    """INSERT OR IGNORE INTO run_tombstone(run_id, project_id, deleted_at)
                       SELECT ?, ?, ?""",
                    ((run_id, self.project_id, now) for run_id in run_ids),
                )
                return run_ids

    async def current_lease(self, run_id: str) -> Lease | None:
        db = self._require_db()
        async with self._write_lock:
            async with self._transaction(db, write=False):
                await self._ensure_run(run_id)
                cursor = await db.execute(
                    "SELECT run_id, worker_id, epoch, acquired_at, expires_at FROM run_lease WHERE run_id = ?",
                    (run_id,),
                )
                row = await cursor.fetchone()
                lease = (
                    None
                    if row is None
                    else Lease(str(row[0]), str(row[1]), int(row[2]), float(row[3]), float(row[4]))
                )
                return lease

    async def release_lease(self, run_id: str, epoch: int) -> bool:
        db = self._require_db()
        async with self._write_lock:
            async with self._transaction(db, write=True):
                cursor = await db.execute(
                    "DELETE FROM run_lease WHERE run_id = ? AND epoch = ?", (run_id, epoch)
                )
                # Resource leases are subordinate to the worker lease.  A
                # terminal worker release must never leave a workspace lock
                # behind for the next run; crash recovery also removes rows
                # when the corresponding worker lease expires.
                await db.execute(
                    "DELETE FROM run_resource_lease WHERE run_id = ? AND lease_epoch = ?",
                    (run_id, epoch),
                )
        return cursor.rowcount == 1

    async def claim_supervisor_lease(
        self,
        owner_id: str,
        *,
        ttl_seconds: float = 120.0,
    ) -> SupervisorLease:
        """Atomically acquire project scheduler leadership.

        The project lease is a leader-election record only.  It does not
        serialize Runs; the selected supervisor may dispatch as many runs as
        ``LocalSupervisor.max_workers`` allows.  A takeover advances the
        epoch, fencing heartbeats from a stale process.
        """

        owner = str(owner_id).strip()
        if not owner:
            raise RunStoreError("supervisor lease owner_id is required")
        ttl = max(1.0, float(ttl_seconds))
        db = self._require_db()
        async with self._write_lock:
            async with self._transaction(db, write=True):
                now = time.time()
                cursor = await db.execute(
                    """SELECT owner_id, epoch, acquired_at, heartbeat_at, expires_at
                       FROM project_supervisor_lease WHERE project_id = ?""",
                    (self.project_id,),
                )
                row = await cursor.fetchone()
                if row is not None and float(row[4]) > now and str(row[0]) != owner:
                    raise SupervisorLeaseConflict(
                        f"project {self.project_id} supervisor lease is owned by {row[0]}"
                    )
                if row is not None and str(row[0]) == owner and float(row[4]) > now:
                    epoch = int(row[1])
                    acquired_at = float(row[2])
                else:
                    epoch = (int(row[1]) + 1) if row is not None else 1
                    acquired_at = now
                expires_at = now + ttl
                await db.execute(
                    """INSERT INTO project_supervisor_lease
                       (project_id, owner_id, epoch, acquired_at, heartbeat_at, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(project_id) DO UPDATE SET owner_id=excluded.owner_id,
                       epoch=excluded.epoch, acquired_at=excluded.acquired_at,
                       heartbeat_at=excluded.heartbeat_at, expires_at=excluded.expires_at""",
                    (self.project_id, owner, epoch, acquired_at, now, expires_at),
                )
                return SupervisorLease(
                    self.project_id,
                    owner,
                    epoch,
                    acquired_at,
                    expires_at,
                    heartbeat_at=now,
                )

    async def current_supervisor_lease(self) -> SupervisorLease | None:
        db = self._require_db()
        async with self._write_lock:
            async with self._transaction(db, write=False):
                cursor = await db.execute(
                    """SELECT project_id, owner_id, epoch, acquired_at, heartbeat_at, expires_at
                       FROM project_supervisor_lease WHERE project_id = ?""",
                    (self.project_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    return None
                return SupervisorLease(
                    str(row[0]),
                    str(row[1]),
                    int(row[2]),
                    float(row[3]),
                    float(row[5]),
                    heartbeat_at=float(row[4]),
                )

    async def heartbeat_supervisor_lease(
        self,
        lease: SupervisorLease,
        *,
        ttl_seconds: float = 120.0,
    ) -> SupervisorLease:
        """Renew leadership only when owner and epoch are still current."""

        if lease.project_id != self.project_id:
            raise SupervisorLeaseFenceError("supervisor lease belongs to another project")
        ttl = max(1.0, float(ttl_seconds))
        db = self._require_db()
        async with self._write_lock:
            async with self._transaction(db, write=True):
                now = time.time()
                cursor = await db.execute(
                    """SELECT owner_id, epoch, acquired_at, expires_at
                       FROM project_supervisor_lease WHERE project_id = ?""",
                    (self.project_id,),
                )
                row = await cursor.fetchone()
                if (
                    row is None
                    or str(row[0]) != lease.owner_id
                    or int(row[1]) != lease.epoch
                    or float(row[3]) <= now
                ):
                    raise SupervisorLeaseFenceError("supervisor lease is no longer current")
                expires_at = now + ttl
                await db.execute(
                    """UPDATE project_supervisor_lease
                       SET heartbeat_at = ?, expires_at = ?
                       WHERE project_id = ? AND owner_id = ? AND epoch = ?""",
                    (now, expires_at, self.project_id, lease.owner_id, lease.epoch),
                )
                return SupervisorLease(
                    self.project_id,
                    lease.owner_id,
                    lease.epoch,
                    float(row[2]),
                    expires_at,
                    heartbeat_at=now,
                )

    async def release_supervisor_lease(self, lease: SupervisorLease) -> bool:
        """Release leadership without allowing a stale owner to delete it."""

        if lease.project_id != self.project_id:
            return False
        db = self._require_db()
        async with self._write_lock:
            async with self._transaction(db, write=True):
                cursor = await db.execute(
                    """DELETE FROM project_supervisor_lease
                       WHERE project_id = ? AND owner_id = ? AND epoch = ?""",
                    (self.project_id, lease.owner_id, lease.epoch),
                )
                return cursor.rowcount == 1

    async def claim_resources(
        self,
        run_id: str,
        lease_epoch: int,
        resources: Iterable[tuple[str, str]],
        *,
        ttl_seconds: float = 120.0,
        action_id: str | None = None,
    ) -> tuple[ResourceLease, ...]:
        """Atomically claim shared/exclusive resources for a worker action.

        Rows are subordinate to the worker ``run_lease`` and are cleaned up
        whenever that lease is released or expires.  Conflict detection is
        done inside the same SQLite write transaction, so two processes cannot
        both observe an available file and acquire it concurrently.
        """

        if lease_epoch < 1:
            raise LeaseFenceError("resource lease requires a positive worker epoch")
        requested: list[tuple[str, str]] = []
        seen: dict[str, str] = {}
        for raw_key, raw_mode in resources:
            key = str(raw_key).strip()
            mode = str(raw_mode).strip().lower()
            if not key or mode not in {"shared", "exclusive"}:
                raise RunStoreError("resource key and mode must be valid")
            previous = seen.get(key)
            if previous == "exclusive" or mode == previous:
                continue
            if previous == "shared" and mode == "exclusive":
                seen[key] = mode
                requested = [(item_key, item_mode) for item_key, item_mode in requested if item_key != key]
                requested.append((key, mode))
                continue
            seen[key] = mode
            requested.append((key, mode))
        if not requested:
            return ()
        ttl = max(1.0, float(ttl_seconds))
        db = self._require_db()
        async with self._write_lock:
            async with self._transaction(db, write=True):
                now = time.time()
                await self._ensure_run(run_id)
                cursor = await db.execute(
                    "SELECT epoch, expires_at FROM run_lease WHERE run_id = ?",
                    (run_id,),
                )
                worker_row = await cursor.fetchone()
                if (
                    worker_row is None
                    or int(worker_row[0]) != lease_epoch
                    or float(worker_row[1]) <= now
                ):
                    raise LeaseFenceError("worker lease is not current for resource claim")
                # A crash can leave a resource row behind until its own TTL.
                # It is safe to remove it earlier when the owning worker lease
                # is gone or fenced, which lets a replacement proceed promptly.
                await db.execute(
                    """DELETE FROM run_resource_lease
                       WHERE project_id = ? AND
                       (expires_at <= ? OR NOT EXISTS (
                           SELECT 1 FROM run_lease AS worker_lease
                           WHERE worker_lease.run_id = run_resource_lease.run_id
                             AND worker_lease.epoch = run_resource_lease.lease_epoch
                             AND worker_lease.expires_at > ?
                       ))""",
                    (self.project_id, now, now),
                )
                cursor = await db.execute(
                    """SELECT resource_key, mode, run_id
                       FROM run_resource_lease
                       WHERE project_id = ? AND expires_at > ? AND run_id != ?""",
                    (self.project_id, now, run_id),
                )
                active = tuple(
                    (str(row[0]), str(row[1]), str(row[2]))
                    for row in await cursor.fetchall()
                )
                conflicts = tuple(
                    (existing_key, existing_mode, existing_run_id)
                    for requested_key, requested_mode in requested
                    for existing_key, existing_mode, existing_run_id in active
                    if _resource_keys_overlap(requested_key, existing_key)
                    and (requested_mode == "exclusive" or existing_mode == "exclusive")
                )
                if conflicts:
                    raise ResourceLeaseConflict(run_id, conflicts)
                expires_at = now + ttl
                claimed: list[ResourceLease] = []
                for resource_key, mode in requested:
                    lease_id = uuid4().hex
                    await db.execute(
                        """INSERT INTO run_resource_lease
                           (lease_id, project_id, run_id, action_id, resource_key, mode,
                            lease_epoch, acquired_at, expires_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            lease_id,
                            self.project_id,
                            run_id,
                            action_id,
                            resource_key,
                            mode,
                            lease_epoch,
                            now,
                            expires_at,
                        ),
                    )
                    claimed.append(
                        ResourceLease(
                            lease_id,
                            self.project_id,
                            run_id,
                            resource_key,
                            mode,
                            lease_epoch,
                            now,
                            expires_at,
                            action_id=action_id,
                        )
                    )
                return tuple(claimed)

    async def renew_resources(
        self,
        leases: Iterable[ResourceLease],
        *,
        ttl_seconds: float = 120.0,
    ) -> tuple[ResourceLease, ...]:
        """Renew resource rows while fencing stale worker epochs."""

        current_leases = tuple(leases)
        if not current_leases:
            return ()
        ttl = max(1.0, float(ttl_seconds))
        db = self._require_db()
        async with self._write_lock:
            async with self._transaction(db, write=True):
                now = time.time()
                renewed: list[ResourceLease] = []
                for resource in current_leases:
                    if resource.project_id != self.project_id:
                        raise LeaseFenceError("resource lease belongs to another project")
                    cursor = await db.execute(
                        """SELECT run_id, resource_key, mode, lease_epoch,
                                  acquired_at, action_id
                           FROM run_resource_lease WHERE lease_id = ?""",
                        (resource.lease_id,),
                    )
                    row = await cursor.fetchone()
                    worker_cursor = await db.execute(
                        "SELECT epoch, expires_at FROM run_lease WHERE run_id = ?",
                        (resource.run_id,),
                    )
                    worker_row = await worker_cursor.fetchone()
                    if (
                        row is None
                        or str(row[0]) != resource.run_id
                        or int(row[3]) != resource.lease_epoch
                        or worker_row is None
                        or int(worker_row[0]) != resource.lease_epoch
                        or float(worker_row[1]) <= now
                    ):
                        raise LeaseFenceError("resource lease is no longer current")
                    expires_at = now + ttl
                    await db.execute(
                        "UPDATE run_resource_lease SET expires_at = ? WHERE lease_id = ?",
                        (expires_at, resource.lease_id),
                    )
                    renewed.append(
                        ResourceLease(
                            resource.lease_id,
                            self.project_id,
                            resource.run_id,
                            str(row[1]),
                            str(row[2]),
                            resource.lease_epoch,
                            float(row[4]),
                            expires_at,
                            action_id=(str(row[5]) if row[5] is not None else None),
                        )
                    )
                return tuple(renewed)

    async def release_resources(self, leases: Iterable[ResourceLease]) -> int:
        """Release only the exact resource rows owned by the caller."""

        current_leases = tuple(leases)
        if not current_leases:
            return 0
        db = self._require_db()
        async with self._write_lock:
            async with self._transaction(db, write=True):
                released = 0
                for resource in current_leases:
                    cursor = await db.execute(
                        """DELETE FROM run_resource_lease
                           WHERE lease_id = ? AND project_id = ? AND run_id = ?
                             AND lease_epoch = ?""",
                        (
                            resource.lease_id,
                            self.project_id,
                            resource.run_id,
                            resource.lease_epoch,
                        ),
                    )
                    released += max(0, int(cursor.rowcount))
                return released

    async def release_resources_for_run(self, run_id: str, *, lease_epoch: int | None = None) -> int:
        """Best-effort cleanup used by recovery and terminal transitions."""

        db = self._require_db()
        async with self._write_lock:
            async with self._transaction(db, write=True):
                if lease_epoch is None:
                    cursor = await db.execute(
                        "DELETE FROM run_resource_lease WHERE project_id = ? AND run_id = ?",
                        (self.project_id, run_id),
                    )
                else:
                    cursor = await db.execute(
                        """DELETE FROM run_resource_lease
                           WHERE project_id = ? AND run_id = ? AND lease_epoch = ?""",
                        (self.project_id, run_id, lease_epoch),
                    )
                return max(0, int(cursor.rowcount))

    async def list_resource_leases(
        self,
        run_id: str | None = None,
        *,
        include_expired: bool = False,
    ) -> tuple[ResourceLease, ...]:
        """Inspect resource ownership for diagnostics and tests."""

        db = self._require_db()
        async with self._write_lock:
            async with self._transaction(db, write=False):
                query = (
                    "SELECT lease_id, project_id, run_id, resource_key, mode, "
                    "lease_epoch, acquired_at, expires_at, action_id "
                    "FROM run_resource_lease WHERE project_id = ?"
                )
                params: list[Any] = [self.project_id]
                if run_id is not None:
                    query += " AND run_id = ?"
                    params.append(run_id)
                if not include_expired:
                    query += " AND expires_at > ?"
                    params.append(time.time())
                query += " ORDER BY acquired_at, lease_id"
                cursor = await db.execute(query, tuple(params))
                return tuple(
                    ResourceLease(
                        str(row[0]),
                        str(row[1]),
                        str(row[2]),
                        str(row[3]),
                        str(row[4]),
                        int(row[5]),
                        float(row[6]),
                        float(row[7]),
                        action_id=(str(row[8]) if row[8] is not None else None),
                    )
                    for row in await cursor.fetchall()
                )

    async def _projection_tx(
        self,
        run_id: str,
        through_sequence: int | None = None,
        *,
        diagnostics: list[str] | None = None,
    ) -> RunProjection:
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
            raw_snapshot = json.loads(snapshot_row[1])
            # First validate the bytes that were actually stored.  This keeps
            # tampering fail-closed even though we tolerate a schema-evolved
            # projection whose current dataclass digest is different.
            if digest_json(raw_snapshot) != snapshot_row[2]:
                raise RunStoreError("snapshot digest mismatch")
            parsed_snapshot = RunProjection.from_dict(raw_snapshot)
            if parsed_snapshot.digest != snapshot_row[2]:
                # Older releases serialized a smaller ActionAttempt shape.  A
                # current parser can still read it, but replay from the event
                # stream is safer than trusting an accelerator whose canonical
                # shape no longer matches the current projection contract.
                if diagnostics is not None:
                    diagnostics.append(
                        f"legacy_snapshot|{snapshot_row[2]}|{parsed_snapshot.sequence}"
                    )
                snapshot = None
                start_sequence = 0
            else:
                snapshot = parsed_snapshot
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
                    contract_digest, lease_epoch, arguments_artifact, result_artifact,
                    error_kind, idempotency_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    action.idempotency_key,
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
            # A fresh worker epoch starts with no resource ownership from a
            # previously fenced worker.  The old rows are safe to remove here
            # because the append is already advancing the authoritative run
            # lease in the same SQLite transaction.
            await db.execute(
                "DELETE FROM run_resource_lease WHERE run_id = ?",
                (event.run_id,),
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
            await db.execute(
                "DELETE FROM run_resource_lease WHERE run_id = ?",
                (event.run_id,),
            )
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
                "SELECT projection_json, projection_digest FROM run_snapshot "
                "WHERE run_id = ? AND sequence = ?",
                (snapshot.run_id, snapshot.sequence),
            )
            row = await cursor.fetchone()
            if row is None:
                # The INSERT may have failed for a reason other than the
                # immutable primary-key collision (for example, a missing
                # run foreign key).  Do not turn that failure into a false
                # success by assuming the row already exists.
                raise RunStoreError("snapshot insert failed") from exc
            if row[1] == snapshot.digest:
                return
            # A snapshot written by an older schema may be byte-integrity
            # valid while its parsed dataclass digest differs (for example,
            # when ``outcome`` was added).  Snapshots are intentionally
            # immutable, so keep the old accelerator and let _projection_tx
            # replay events instead of treating a harmless migration as a
            # conflicting write.  Any raw-byte mismatch remains fail-closed.
            try:
                raw = json.loads(row[0])
                raw_digest = digest_json(raw)
                parsed = RunProjection.from_dict(raw)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as parse_error:
                raise RunStoreError("snapshot already exists with a different digest") from parse_error
            if raw_digest != row[1] or parsed.digest == row[1]:
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
               FROM run_record
               WHERE run_id = ?
                 AND NOT EXISTS (
                     SELECT 1 FROM run_tombstone hidden
                     WHERE hidden.run_id = run_record.run_id
                 )""",
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


def _resource_keys_overlap(left: str, right: str) -> bool:
    """Return whether two normalized resource keys may touch one another."""

    a = str(left).strip().rstrip("/") or "."
    b = str(right).strip().rstrip("/") or "."
    if a == b:
        return True
    a_kind, _, a_value = a.partition(":")
    b_kind, _, b_value = b.partition(":")
    if a_kind == "project" or b_kind == "project":
        return True
    hierarchical = {"path", "workspace"}
    if a_kind not in hierarchical or b_kind not in hierarchical:
        return False
    a_value = a_value.rstrip("/") or "."
    b_value = b_value.rstrip("/") or "."
    return (
        a_value == b_value
        or a_value.startswith(f"{b_value}/")
        or b_value.startswith(f"{a_value}/")
    )


__all__ = [
    "AppendEvent",
    "DuplicateEventError",
    "EventPage",
    "LeaseFenceError",
    "ResourceLeaseConflict",
    "RunNotFound",
    "RunRecordView",
    "RunStore",
    "RunStoreError",
    "SequenceConflict",
    "SupervisorLeaseConflict",
    "SupervisorLeaseFenceError",
    "StoredEvent",
]
