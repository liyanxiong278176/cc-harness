"""Durable local journal and content-addressed store for evaluation runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from .gates import ReleaseDecision

from .models import (
    ArtifactRef,
    AttemptLease,
    AttemptState,
    EvalRunManifest,
    RunState,
    TrialRequest,
    TrialResult,
    TrialState,
)
from .serialization import canonical_json_bytes, content_fingerprint


class EvalStoreError(RuntimeError):
    """Base error for evaluation persistence."""


class EvidenceIntegrityError(EvalStoreError):
    """Raised when evidence bytes or cross-record identities do not match."""


class StateTransitionError(EvalStoreError):
    """Raised when a lifecycle transition is not legal from the current state."""


@dataclass(frozen=True)
class AttemptSnapshot:
    attempt_id: str
    attempt: int
    state: AttemptState
    worker_id: str
    parent_attempt_id: str | None
    heartbeat_at: datetime


class EvalStore:
    """SQLite lifecycle journal with object-first content-addressed writes."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.db_path = self.root / "eval.sqlite3"
        self.objects_root = self.root / "objects"
        self.workspaces_root = self.root / "workspaces"
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def open(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects_root.mkdir(parents=True, exist_ok=True)
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA synchronous = FULL")
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS artifact (
                digest TEXT PRIMARY KEY,
                media_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                created_at_ns INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS eval_run (
                run_id TEXT PRIMARY KEY,
                manifest_digest TEXT NOT NULL UNIQUE,
                manifest_json TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at_ns INTEGER NOT NULL,
                updated_at_ns INTEGER NOT NULL,
                FOREIGN KEY (manifest_digest) REFERENCES artifact(digest)
            );

            CREATE TABLE IF NOT EXISTS trial (
                trial_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                request_json TEXT NOT NULL,
                state TEXT NOT NULL,
                current_attempt_id TEXT,
                result_digest TEXT,
                result_json TEXT,
                created_at_ns INTEGER NOT NULL,
                updated_at_ns INTEGER NOT NULL,
                FOREIGN KEY (run_id) REFERENCES eval_run(run_id),
                FOREIGN KEY (result_digest) REFERENCES artifact(digest)
            );

            CREATE TABLE IF NOT EXISTS attempt (
                attempt_id TEXT PRIMARY KEY,
                trial_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
                parent_attempt_id TEXT,
                worker_id TEXT NOT NULL,
                state TEXT NOT NULL,
                claimed_at_ns INTEGER NOT NULL,
                heartbeat_at_ns INTEGER NOT NULL,
                finished_at_ns INTEGER,
                error TEXT,
                result_digest TEXT,
                UNIQUE (trial_id, attempt_number),
                FOREIGN KEY (trial_id) REFERENCES trial(trial_id),
                FOREIGN KEY (parent_attempt_id) REFERENCES attempt(attempt_id),
                FOREIGN KEY (result_digest) REFERENCES artifact(digest)
            );

            CREATE TABLE IF NOT EXISTS lifecycle_event (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                trial_id TEXT,
                attempt_id TEXT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at_ns INTEGER NOT NULL,
                FOREIGN KEY (run_id) REFERENCES eval_run(run_id),
                FOREIGN KEY (trial_id) REFERENCES trial(trial_id),
                FOREIGN KEY (attempt_id) REFERENCES attempt(attempt_id)
            );

            CREATE TABLE IF NOT EXISTS release_decision (
                decision_digest TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                created_at_ns INTEGER NOT NULL,
                FOREIGN KEY (decision_digest) REFERENCES artifact(digest),
                FOREIGN KEY (run_id) REFERENCES eval_run(run_id)
            );

            CREATE INDEX IF NOT EXISTS idx_trial_state ON trial(state, created_at_ns);
            CREATE INDEX IF NOT EXISTS idx_attempt_heartbeat
                ON attempt(state, heartbeat_at_ns);
            CREATE INDEX IF NOT EXISTS idx_event_run ON lifecycle_event(run_id, seq);
            """
        )
        await self._db.commit()

    async def put_artifact(self, content: bytes, media_type: str) -> ArtifactRef:
        if not isinstance(content, bytes):
            raise TypeError("artifact content must be bytes")
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        reference = ArtifactRef(
            digest=digest,
            media_type=media_type,
            size_bytes=len(content),
        )
        target = self._object_path(reference.digest)
        await asyncio.to_thread(self._write_object, target, content)

        async with self._write_lock:
            db = self._require_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT media_type, size_bytes FROM artifact WHERE digest = ?",
                        (reference.digest,),
                    )
                ).fetchone()
                if row is not None and int(row["size_bytes"]) != reference.size_bytes:
                    raise EvidenceIntegrityError(
                        "an artifact digest was registered with conflicting metadata"
                    )
                await db.execute(
                    "INSERT OR IGNORE INTO artifact "
                    "(digest, media_type, size_bytes, created_at_ns) VALUES (?, ?, ?, ?)",
                    (
                        reference.digest,
                        reference.media_type,
                        reference.size_bytes,
                        time.time_ns(),
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return reference

    async def read_artifact(self, reference: ArtifactRef) -> bytes:
        row = await (
            await self._require_db().execute(
                "SELECT media_type, size_bytes FROM artifact WHERE digest = ?",
                (reference.digest,),
            )
        ).fetchone()
        if row is None:
            raise EvidenceIntegrityError(f"artifact is not registered: {reference.digest}")
        if int(row["size_bytes"]) != reference.size_bytes:
            raise EvidenceIntegrityError("artifact reference metadata does not match the registry")
        try:
            content = await asyncio.to_thread(self._object_path(reference.digest).read_bytes)
        except OSError as exc:
            raise EvidenceIntegrityError(
                f"artifact object is unavailable: {reference.digest}"
            ) from exc
        actual = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if actual != reference.digest or len(content) != reference.size_bytes:
            raise EvidenceIntegrityError(f"artifact content is corrupt: {reference.digest}")
        return content

    async def create_run(self, manifest: EvalRunManifest) -> str:
        manifest_bytes = canonical_json_bytes(manifest)
        manifest_ref = await self.put_artifact(
            manifest_bytes, "application/vnd.cc-harness.eval-run+json"
        )
        manifest_digest = content_fingerprint(manifest)
        if manifest_ref.digest != manifest_digest:
            raise EvidenceIntegrityError("manifest serialization did not preserve its fingerprint")

        now = time.time_ns()
        async with self._write_lock:
            db = self._require_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "INSERT INTO eval_run "
                    "(run_id, manifest_digest, manifest_json, state, created_at_ns, updated_at_ns) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        manifest.run_id,
                        manifest_digest,
                        manifest_bytes.decode("utf-8"),
                        RunState.PREPARED.value,
                        now,
                        now,
                    ),
                )
                await self._append_event_tx(
                    manifest.run_id,
                    "run_created",
                    {"manifest_digest": manifest_digest, "state": RunState.PREPARED.value},
                    now=now,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return manifest_digest

    async def enqueue_trial(self, request: TrialRequest) -> None:
        run_row = await self._run_row(request.run_id)
        if RunState(run_row["state"]) not in {RunState.PREPARED, RunState.RUNNING}:
            raise StateTransitionError(
                "trials can be queued only while a run is prepared or running"
            )
        if run_row["manifest_digest"] != request.run_manifest_digest:
            raise EvidenceIntegrityError("trial request references the wrong run manifest")
        manifest = EvalRunManifest.model_validate_json(run_row["manifest_json"])
        task_digest = content_fingerprint(request.task)
        if task_digest not in manifest.task_contract_digests:
            raise EvidenceIntegrityError("task contract is not declared by the run manifest")
        await self._verify_task_artifacts(request)

        request_json = canonical_json_bytes(request).decode("utf-8")
        now = time.time_ns()
        async with self._write_lock:
            db = self._require_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                locked_run = await (
                    await db.execute(
                        "SELECT state FROM eval_run WHERE run_id = ?", (request.run_id,)
                    )
                ).fetchone()
                if locked_run is None or RunState(locked_run["state"]) not in {
                    RunState.PREPARED,
                    RunState.RUNNING,
                }:
                    raise StateTransitionError("run completed while the trial was being queued")
                await db.execute(
                    "INSERT INTO trial "
                    "(trial_id, run_id, request_json, state, current_attempt_id, result_digest, "
                    "result_json, created_at_ns, updated_at_ns) "
                    "VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                    (
                        request.trial_id,
                        request.run_id,
                        request_json,
                        TrialState.QUEUED.value,
                        now,
                        now,
                    ),
                )
                await self._append_event_tx(
                    request.run_id,
                    "trial_queued",
                    {
                        "task_digest": task_digest,
                        "adapter": request.adapter.model_dump(mode="json"),
                    },
                    trial_id=request.trial_id,
                    now=now,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def claim_next(self, worker_id: str) -> AttemptLease | None:
        now = time.time_ns()
        async with self._write_lock:
            db = self._require_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT trial_id, run_id, request_json FROM trial "
                        "WHERE state = ? ORDER BY created_at_ns, trial_id LIMIT 1",
                        (TrialState.QUEUED.value,),
                    )
                ).fetchone()
                if row is None:
                    await db.commit()
                    return None
                latest = await (
                    await db.execute(
                        "SELECT attempt_id, attempt_number FROM attempt "
                        "WHERE trial_id = ? ORDER BY attempt_number DESC LIMIT 1",
                        (row["trial_id"],),
                    )
                ).fetchone()
                attempt_number = int(latest["attempt_number"]) + 1 if latest else 1
                parent_attempt_id = str(latest["attempt_id"]) if latest else None
                attempt_id = f"attempt-{uuid.uuid4().hex}"
                await db.execute(
                    "INSERT INTO attempt "
                    "(attempt_id, trial_id, attempt_number, parent_attempt_id, worker_id, state, "
                    "claimed_at_ns, heartbeat_at_ns) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        attempt_id,
                        row["trial_id"],
                        attempt_number,
                        parent_attempt_id,
                        worker_id,
                        AttemptState.RUNNING.value,
                        now,
                        now,
                    ),
                )
                await db.execute(
                    "UPDATE trial SET state = ?, current_attempt_id = ?, updated_at_ns = ? "
                    "WHERE trial_id = ? AND state = ?",
                    (
                        TrialState.RUNNING.value,
                        attempt_id,
                        now,
                        row["trial_id"],
                        TrialState.QUEUED.value,
                    ),
                )
                await db.execute(
                    "UPDATE eval_run SET state = ?, updated_at_ns = ? "
                    "WHERE run_id = ? AND state = ?",
                    (RunState.RUNNING.value, now, row["run_id"], RunState.PREPARED.value),
                )
                await self._append_event_tx(
                    row["run_id"],
                    "attempt_claimed",
                    {
                        "attempt": attempt_number,
                        "worker_id": worker_id,
                        "parent_attempt_id": parent_attempt_id,
                    },
                    trial_id=row["trial_id"],
                    attempt_id=attempt_id,
                    now=now,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

        timestamp = self._datetime(now)
        return AttemptLease(
            attempt_id=attempt_id,
            trial_id=row["trial_id"],
            attempt=attempt_number,
            worker_id=worker_id,
            request=TrialRequest.model_validate_json(row["request_json"]),
            claimed_at=timestamp,
            heartbeat_at=timestamp,
        )

    async def heartbeat(self, attempt_id: str, worker_id: str) -> None:
        now = time.time_ns()
        async with self._write_lock:
            db = self._require_db()
            cursor = await db.execute(
                "UPDATE attempt SET heartbeat_at_ns = ? "
                "WHERE attempt_id = ? AND worker_id = ? AND state = ?",
                (now, attempt_id, worker_id, AttemptState.RUNNING.value),
            )
            await db.commit()
        if cursor.rowcount != 1:
            raise StateTransitionError("attempt lease is no longer owned by this worker")

    async def request_cancel(self, trial_id: str) -> bool:
        now = time.time_ns()
        async with self._write_lock:
            db = self._require_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT run_id, state, current_attempt_id FROM trial WHERE trial_id = ?",
                        (trial_id,),
                    )
                ).fetchone()
                if row is None:
                    raise EvalStoreError(f"trial does not exist: {trial_id}")
                state = TrialState(row["state"])
                if state is TrialState.QUEUED:
                    new_state = TrialState.CANCELLED
                elif state is TrialState.RUNNING:
                    new_state = TrialState.CANCEL_REQUESTED
                else:
                    await db.commit()
                    return False
                await db.execute(
                    "UPDATE trial SET state = ?, updated_at_ns = ? WHERE trial_id = ?",
                    (new_state.value, now, trial_id),
                )
                await self._append_event_tx(
                    row["run_id"],
                    "trial_cancel_requested",
                    {"previous_state": state.value, "state": new_state.value},
                    trial_id=trial_id,
                    attempt_id=row["current_attempt_id"],
                    now=now,
                )
                await db.commit()
                return True
            except BaseException:
                await db.rollback()
                raise

    async def is_cancel_requested(self, trial_id: str) -> bool:
        return await self.get_trial_state(trial_id) is TrialState.CANCEL_REQUESTED

    async def complete_attempt(self, lease: AttemptLease, result: TrialResult) -> str:
        self._validate_result(lease, result)
        for reference in self._result_artifacts(result):
            await self.read_artifact(reference)

        result_bytes = canonical_json_bytes(result)
        result_ref = await self.put_artifact(
            result_bytes,
            "application/vnd.cc-harness.eval-trial-result+json",
        )
        now = time.time_ns()
        async with self._write_lock:
            db = self._require_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT trial.run_id, trial.state AS trial_state, "
                        "trial.current_attempt_id, attempt.state AS attempt_state, "
                        "attempt.worker_id FROM trial JOIN attempt "
                        "ON attempt.attempt_id = trial.current_attempt_id "
                        "WHERE trial.trial_id = ?",
                        (lease.trial_id,),
                    )
                ).fetchone()
                if row is None or row["current_attempt_id"] != lease.attempt_id:
                    raise StateTransitionError("attempt is not the active trial attempt")
                if row["worker_id"] != lease.worker_id:
                    raise StateTransitionError("attempt is owned by a different worker")
                if AttemptState(row["attempt_state"]) is not AttemptState.RUNNING:
                    raise StateTransitionError("attempt is no longer running")
                trial_state = TrialState(row["trial_state"])
                if trial_state not in {TrialState.RUNNING, TrialState.CANCEL_REQUESTED}:
                    raise StateTransitionError(
                        "trial cannot accept a result from its current state"
                    )

                attempt_state = (
                    AttemptState.CANCELLED
                    if trial_state is TrialState.CANCEL_REQUESTED
                    else AttemptState.COMPLETED
                )
                await db.execute(
                    "UPDATE attempt SET state = ?, finished_at_ns = ?, result_digest = ? "
                    "WHERE attempt_id = ?",
                    (attempt_state.value, now, result_ref.digest, lease.attempt_id),
                )
                await db.execute(
                    "UPDATE trial SET state = ?, result_digest = ?, result_json = ?, "
                    "updated_at_ns = ? WHERE trial_id = ?",
                    (
                        TrialState.COMPLETED.value,
                        result_ref.digest,
                        result_bytes.decode("utf-8"),
                        now,
                        lease.trial_id,
                    ),
                )
                await self._append_event_tx(
                    row["run_id"],
                    "trial_completed",
                    {"result_digest": result_ref.digest, "result_status": result.status.value},
                    trial_id=lease.trial_id,
                    attempt_id=lease.attempt_id,
                    now=now,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return result_ref.digest

    async def mark_attempt_outcome_unknown(self, lease: AttemptLease, reason: str) -> None:
        await self._mark_unknown(lease.attempt_id, lease.worker_id, reason)

    async def recover_stale_attempts(self, stale_before: datetime) -> tuple[str, ...]:
        if stale_before.tzinfo is None or stale_before.utcoffset() is None:
            raise ValueError("stale_before must be timezone-aware")
        cutoff = int(stale_before.timestamp() * 1_000_000_000)
        rows = await (
            await self._require_db().execute(
                "SELECT attempt_id, worker_id FROM attempt "
                "WHERE state = ? AND heartbeat_at_ns < ? ORDER BY claimed_at_ns",
                (AttemptState.RUNNING.value, cutoff),
            )
        ).fetchall()
        recovered: list[str] = []
        for row in rows:
            try:
                await self._mark_unknown(
                    row["attempt_id"],
                    row["worker_id"],
                    "worker heartbeat expired; external outcome is unknown",
                )
            except StateTransitionError:
                continue
            recovered.append(row["attempt_id"])
        return tuple(recovered)

    async def retry_trial(self, trial_id: str) -> None:
        now = time.time_ns()
        async with self._write_lock:
            db = self._require_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT run_id, state, current_attempt_id FROM trial WHERE trial_id = ?",
                        (trial_id,),
                    )
                ).fetchone()
                if row is None:
                    raise EvalStoreError(f"trial does not exist: {trial_id}")
                if TrialState(row["state"]) is not TrialState.OUTCOME_UNKNOWN:
                    raise StateTransitionError("only outcome_unknown trials can be retried")
                await db.execute(
                    "UPDATE trial SET state = ?, current_attempt_id = NULL, updated_at_ns = ? "
                    "WHERE trial_id = ?",
                    (TrialState.QUEUED.value, now, trial_id),
                )
                await db.execute(
                    "UPDATE eval_run SET state = ?, updated_at_ns = ? WHERE run_id = ?",
                    (RunState.RUNNING.value, now, row["run_id"]),
                )
                await self._append_event_tx(
                    row["run_id"],
                    "trial_retry_queued",
                    {"previous_attempt_id": row["current_attempt_id"]},
                    trial_id=trial_id,
                    now=now,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def get_trial_state(self, trial_id: str) -> TrialState:
        row = await (
            await self._require_db().execute(
                "SELECT state FROM trial WHERE trial_id = ?", (trial_id,)
            )
        ).fetchone()
        if row is None:
            raise EvalStoreError(f"trial does not exist: {trial_id}")
        return TrialState(row["state"])

    async def get_run_state(self, run_id: str) -> RunState:
        row = await self._run_row(run_id)
        return RunState(row["state"])

    async def get_manifest(self, run_id: str) -> EvalRunManifest:
        row = await self._run_row(run_id)
        return EvalRunManifest.model_validate_json(row["manifest_json"])

    async def get_result(self, trial_id: str) -> TrialResult | None:
        row = await (
            await self._require_db().execute(
                "SELECT result_json FROM trial WHERE trial_id = ?", (trial_id,)
            )
        ).fetchone()
        if row is None:
            raise EvalStoreError(f"trial does not exist: {trial_id}")
        return TrialResult.model_validate_json(row["result_json"]) if row["result_json"] else None

    async def list_results(self, run_id: str) -> tuple[TrialResult, ...]:
        await self._run_row(run_id)
        rows = await (
            await self._require_db().execute(
                "SELECT result_json FROM trial WHERE run_id = ? AND result_json IS NOT NULL "
                "ORDER BY created_at_ns, trial_id",
                (run_id,),
            )
        ).fetchall()
        return tuple(TrialResult.model_validate_json(row["result_json"]) for row in rows)

    async def record_release_decision(self, decision: ReleaseDecision) -> str:
        from .gates import ReleaseDecision

        if not isinstance(decision, ReleaseDecision):
            raise TypeError("decision must be a ReleaseDecision")
        run_row = await self._run_row(decision.run_id)
        if run_row["manifest_digest"] != decision.run_manifest_digest:
            raise EvidenceIntegrityError("release decision references the wrong run manifest")
        decision_bytes = canonical_json_bytes(decision)
        reference = await self.put_artifact(
            decision_bytes,
            "application/vnd.cc-harness.release-decision+json",
        )
        decision_digest = content_fingerprint(decision)
        if reference.digest != decision_digest:
            raise EvidenceIntegrityError("release decision fingerprint is not canonical")

        now = time.time_ns()
        async with self._write_lock:
            db = self._require_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                existing = await (
                    await db.execute(
                        "SELECT 1 FROM release_decision WHERE decision_digest = ?",
                        (decision_digest,),
                    )
                ).fetchone()
                if existing is not None:
                    await db.commit()
                    return decision_digest
                await db.execute(
                    "INSERT INTO release_decision "
                    "(decision_digest, run_id, decision_json, created_at_ns) VALUES (?, ?, ?, ?)",
                    (decision_digest, decision.run_id, decision_bytes.decode("utf-8"), now),
                )
                await self._append_event_tx(
                    decision.run_id,
                    "release_decision_recorded",
                    {
                        "decision_digest": decision_digest,
                        "status": decision.status.value,
                        "valid": decision.valid,
                        "complete": decision.complete,
                    },
                    now=now,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return decision_digest

    async def read_release_decision(self, decision_digest: str) -> ReleaseDecision:
        from .gates import ReleaseDecision

        row = await (
            await self._require_db().execute(
                "SELECT decision_json FROM release_decision WHERE decision_digest = ?",
                (decision_digest,),
            )
        ).fetchone()
        if row is None:
            raise EvalStoreError(f"release decision does not exist: {decision_digest}")
        return ReleaseDecision.model_validate_json(row["decision_json"])

    async def get_attempts(self, trial_id: str) -> tuple[AttemptSnapshot, ...]:
        rows = await (
            await self._require_db().execute(
                "SELECT attempt_id, attempt_number, state, worker_id, parent_attempt_id, "
                "heartbeat_at_ns FROM attempt WHERE trial_id = ? ORDER BY attempt_number",
                (trial_id,),
            )
        ).fetchall()
        return tuple(
            AttemptSnapshot(
                attempt_id=row["attempt_id"],
                attempt=int(row["attempt_number"]),
                state=AttemptState(row["state"]),
                worker_id=row["worker_id"],
                parent_attempt_id=row["parent_attempt_id"],
                heartbeat_at=self._datetime(int(row["heartbeat_at_ns"])),
            )
            for row in rows
        )

    async def lifecycle_events(self, run_id: str) -> tuple[dict[str, Any], ...]:
        rows = await (
            await self._require_db().execute(
                "SELECT seq, trial_id, attempt_id, event_type, payload_json, created_at_ns "
                "FROM lifecycle_event WHERE run_id = ? ORDER BY seq",
                (run_id,),
            )
        ).fetchall()
        return tuple(
            {
                "seq": int(row["seq"]),
                "trial_id": row["trial_id"],
                "attempt_id": row["attempt_id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "created_at": self._datetime(int(row["created_at_ns"])),
            }
            for row in rows
        )

    async def finalize_ready_runs(self) -> tuple[str, ...]:
        rows = await (
            await self._require_db().execute(
                "SELECT run_id FROM eval_run WHERE state IN (?, ?)",
                (RunState.PREPARED.value, RunState.RUNNING.value),
            )
        ).fetchall()
        finalized: list[str] = []
        terminal = {
            TrialState.COMPLETED.value,
            TrialState.CANCELLED.value,
            TrialState.OUTCOME_UNKNOWN.value,
        }
        for run_row in rows:
            run_id = run_row["run_id"]
            states = await (
                await self._require_db().execute(
                    "SELECT state, COUNT(*) AS count FROM trial WHERE run_id = ? GROUP BY state",
                    (run_id,),
                )
            ).fetchall()
            if not states or any(row["state"] not in terminal for row in states):
                continue
            now = time.time_ns()
            async with self._write_lock:
                db = self._require_db()
                await db.execute("BEGIN IMMEDIATE")
                try:
                    current = await (
                        await db.execute("SELECT state FROM eval_run WHERE run_id = ?", (run_id,))
                    ).fetchone()
                    if current is None or RunState(current["state"]) is RunState.COMPLETED:
                        await db.commit()
                        continue
                    await db.execute(
                        "UPDATE eval_run SET state = ?, updated_at_ns = ? WHERE run_id = ?",
                        (RunState.COMPLETED.value, now, run_id),
                    )
                    await self._append_event_tx(
                        run_id,
                        "run_completed",
                        {row["state"]: int(row["count"]) for row in states},
                        now=now,
                    )
                    await db.commit()
                    finalized.append(run_id)
                except BaseException:
                    await db.rollback()
                    raise
        return tuple(finalized)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _mark_unknown(self, attempt_id: str, worker_id: str, reason: str) -> None:
        now = time.time_ns()
        async with self._write_lock:
            db = self._require_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT attempt.trial_id, trial.run_id, attempt.state, attempt.worker_id "
                        "FROM attempt JOIN trial ON trial.trial_id = attempt.trial_id "
                        "WHERE attempt.attempt_id = ?",
                        (attempt_id,),
                    )
                ).fetchone()
                if row is None:
                    raise EvalStoreError(f"attempt does not exist: {attempt_id}")
                if (
                    row["worker_id"] != worker_id
                    or AttemptState(row["state"]) is not AttemptState.RUNNING
                ):
                    raise StateTransitionError("attempt is no longer an active lease")
                await db.execute(
                    "UPDATE attempt SET state = ?, finished_at_ns = ?, error = ? "
                    "WHERE attempt_id = ?",
                    (AttemptState.OUTCOME_UNKNOWN.value, now, reason, attempt_id),
                )
                await db.execute(
                    "UPDATE trial SET state = ?, updated_at_ns = ? WHERE trial_id = ?",
                    (TrialState.OUTCOME_UNKNOWN.value, now, row["trial_id"]),
                )
                await self._append_event_tx(
                    row["run_id"],
                    "attempt_outcome_unknown",
                    {"reason": reason},
                    trial_id=row["trial_id"],
                    attempt_id=attempt_id,
                    now=now,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def _verify_task_artifacts(self, request: TrialRequest) -> None:
        references = [request.task.instruction_ref, request.task.initial_state_ref]
        references.extend(
            grader.rubric_ref for grader in request.task.graders if grader.rubric_ref is not None
        )
        for reference in references:
            await self.read_artifact(reference)

    @staticmethod
    def _validate_result(lease: AttemptLease, result: TrialResult) -> None:
        request = lease.request
        expected = {
            "trial_id": lease.trial_id,
            "run_id": request.run_id,
            "run_manifest_digest": request.run_manifest_digest,
            "task_id": request.task.task_id,
            "task_contract_digest": content_fingerprint(request.task),
            "attempt": lease.attempt,
            "adapter": request.adapter,
        }
        actual = {
            "trial_id": result.trial_id,
            "run_id": result.run_id,
            "run_manifest_digest": result.run_manifest_digest,
            "task_id": result.task_id,
            "task_contract_digest": result.task_contract_digest,
            "attempt": result.attempt,
            "adapter": result.adapter,
        }
        if actual != expected:
            raise EvidenceIntegrityError("trial result identity does not match its active lease")

    @staticmethod
    def _result_artifacts(result: TrialResult) -> tuple[ArtifactRef, ...]:
        references: list[ArtifactRef] = list(result.artifacts)
        if result.outcome_ref is not None:
            references.append(result.outcome_ref)
        if result.trajectory_ref is not None:
            references.append(result.trajectory_ref)
        references.extend(
            grader.details_ref for grader in result.grader_results if grader.details_ref is not None
        )
        if result.failure is not None and result.failure.evidence_ref is not None:
            references.append(result.failure.evidence_ref)
        unique: dict[tuple[str, str, int], ArtifactRef] = {}
        for reference in references:
            key = (reference.digest, reference.media_type, reference.size_bytes)
            unique[key] = reference
        return tuple(unique.values())

    async def _run_row(self, run_id: str) -> aiosqlite.Row:
        row = await (
            await self._require_db().execute("SELECT * FROM eval_run WHERE run_id = ?", (run_id,))
        ).fetchone()
        if row is None:
            raise EvalStoreError(f"run does not exist: {run_id}")
        return row

    async def _append_event_tx(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        trial_id: str | None = None,
        attempt_id: str | None = None,
        now: int,
    ) -> None:
        await self._require_db().execute(
            "INSERT INTO lifecycle_event "
            "(run_id, trial_id, attempt_id, event_type, payload_json, created_at_ns) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                trial_id,
                attempt_id,
                event_type,
                canonical_json_bytes(payload).decode("utf-8"),
                now,
            ),
        )

    def _object_path(self, digest: str) -> Path:
        hexadecimal = digest.removeprefix("sha256:")
        if len(hexadecimal) != 64 or any(c not in "0123456789abcdef" for c in hexadecimal):
            raise EvidenceIntegrityError(f"invalid artifact digest: {digest}")
        return self.objects_root / hexadecimal[:2] / hexadecimal[2:]

    @staticmethod
    def _write_object(target: Path, content: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != content:
                raise EvidenceIntegrityError(f"content-address collision: {target}")
            return
        descriptor, raw_temp = tempfile.mkstemp(prefix=".artifact-", dir=target.parent)
        temp = Path(raw_temp)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
            try:
                target.chmod(0o600)
            except OSError:
                pass
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _datetime(value_ns: int) -> datetime:
        return datetime.fromtimestamp(value_ns / 1_000_000_000, tz=UTC)

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise EvalStoreError("evaluation store is not open")
        return self._db
