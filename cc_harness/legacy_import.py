"""Idempotent legacy session/Todo/memory/action import adapter."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .action_recovery import migrate_legacy_journal
from .artifacts import ArtifactStore, digest_bytes
from .run_events import EventActor, RunEvent
from .run_model import GoalContract, Run, RuntimeContract
from .run_store import RunStore
from .session_store import SessionStore


@dataclass(frozen=True)
class LegacyImportReport:
    imported_sources: tuple[str, ...] = ()
    skipped_sources: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    run_ids: tuple[str, ...] = ()
    artifact_digests: tuple[str, ...] = ()
    unverified_claims: tuple[str, ...] = ()
    imported_events: int = 0

    @property
    def blocking_errors(self) -> tuple[str, ...]:
        """Errors that must stop cutover; malformed optional sources are recoverable."""
        return tuple(error for error in self.errors if not error.startswith("corrupt.json:"))


class LegacyImportError(RuntimeError):
    """Raised only when strict legacy import is requested."""


class LegacyImporter:
    """Translate old representations into events and content-addressed objects."""

    VERSION = "legacy-import-v1"

    def __init__(
        self,
        store: RunStore,
        *,
        artifacts: ArtifactStore | None = None,
        runtime_contract: RuntimeContract | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts or store.artifacts
        self.runtime_contract = runtime_contract or RuntimeContract(
            "runtime-rebuild-v1", 1, "sha256:tools", "sha256:model", "sha256:policy", "sha256:cap"
        )
        self.actor = EventActor("legacy_importer", "legacy-importer")

    async def import_directory(
        self,
        source_root: Path,
        *,
        dry_run: bool = False,
        strict: bool = False,
    ) -> LegacyImportReport:
        root = Path(source_root).resolve(strict=True)
        manifest = self._load_manifest(root)
        imported: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        runs: set[str] = set()
        artifacts: set[str] = set()
        unverified: list[str] = []
        imported_events = 0

        for path in sorted(root.iterdir()):
            if not path.is_file() or path.name == "manifest.json":
                continue
            name = path.name
            raw = path.read_bytes()
            digest = digest_bytes(raw)
            expected = self._manifest_digest(manifest, name)
            if expected is not None and expected != digest:
                errors.append(f"{name}: digest mismatch")
                continue
            try:
                if name == "large-object.txt":
                    if not dry_run:
                        artifacts.add(self.artifacts.put(raw, media_type="application/octet-stream").digest)
                    imported.append(name)
                    continue
                if name == "session.json":
                    data = self._json(raw, name)
                    run_id, count, refs = await self._import_session(data, digest, dry_run=dry_run)
                    runs.add(run_id)
                    artifacts.update(refs)
                    imported_events += count
                elif name == "session-conflict.json":
                    data = self._json(raw, name)
                    run_id, count = await self._import_conflict(data, digest, dry_run=dry_run)
                    runs.add(run_id)
                    imported_events += count
                    unverified.append(f"{name}: conflicting historical representation retained")
                elif name == "todo.yaml":
                    data = self._yaml(raw, name)
                    session_id = str(data.get("session_id") or "")
                    run_id, count = await self._import_todos(session_id, data, digest, dry_run=dry_run)
                    runs.add(run_id)
                    imported_events += count
                elif name == "memory.jsonl":
                    records = self._jsonl(raw, name)
                    session_id = str(records[0].get("source") or "legacy-session-001") if records else "legacy-session-001"
                    run_id, count = await self._import_memories(session_id, records, digest, dry_run=dry_run)
                    runs.add(run_id)
                    imported_events += count
                    unverified.extend(
                        f"memory:{record.get('id', index)} missing trustworthy source"
                        for index, record in enumerate(records)
                        if not record.get("source")
                    )
                elif name == "action-journal.jsonl":
                    records = self._jsonl(raw, name)
                    session_id = str(records[0].get("session_id") or "legacy-session-001") if records else "legacy-session-001"
                    run_id, count, unknown = await self._import_actions(session_id, records, digest, dry_run=dry_run)
                    runs.add(run_id)
                    imported_events += count
                    unverified.extend(f"action:{action_id} terminal outcome unknown" for action_id in unknown)
                else:
                    self._json(raw, name)
                    imported_events += 0
                imported.append(name)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError, LegacyImportError) as exc:
                errors.append(f"{name}: {exc}")

        report = LegacyImportReport(
            tuple(imported),
            tuple(skipped),
            tuple(errors),
            tuple(sorted(runs)),
            tuple(sorted(artifacts)),
            tuple(unverified),
            imported_events,
        )
        if strict and report.errors:
            raise LegacyImportError("; ".join(report.errors))
        return report

    async def import_project(
        self,
        project_root: Path,
        *,
        sessions_db: Path | None = None,
        todo_file: Path | None = None,
        action_root: Path | None = None,
        memory_db: Path | None = None,
        dry_run: bool = False,
        strict: bool = False,
        hold_imported_runs: bool = True,
    ) -> LegacyImportReport:
        """Import the real project-local legacy stores without deleting them.

        The source databases are opened read-only through their existing
        adapters. Each source is represented by a content digest and a
        ``LegacyRunImported`` event, so rerunning this method is idempotent
        even after a process interruption.
        """

        root = Path(project_root).resolve(strict=True)
        sessions_db = Path(sessions_db or root / ".cc-harness" / "sessions.db")
        todo_file = Path(todo_file or root / ".cc-harness" / "todos" / "todos.yaml")
        action_root = Path(action_root or root / ".cc-harness" / "action-journal")
        memory_db = Path(memory_db or root / ".cc-harness" / "memory.db")
        imported: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        run_ids: set[str] = set()
        artifacts: set[str] = set()
        unverified: list[str] = []
        imported_events = 0

        if sessions_db.is_file():
            source_digest = digest_bytes(sessions_db.read_bytes())
            legacy = SessionStore(root)
            try:
                await legacy.open()
                records = await legacy.list_recent(limit=100_000)
                for record in records:
                    try:
                        messages = await legacy.load(record.session_id)
                        checkpoints = [
                            {
                                "checkpoint_index": item.checkpoint_index,
                                "label": item.label,
                                "message_count": item.message_count,
                                "event_count": item.event_count,
                                "created_at": item.created_at,
                            }
                            for item in await legacy.list_checkpoints(
                                record.session_id,
                                limit=100_000,
                            )
                        ]
                        run_id, count, refs = await self._import_session(
                            {
                                "session_id": record.session_id,
                                "messages": messages,
                                "checkpoints": checkpoints,
                            },
                            source_digest,
                            dry_run=dry_run,
                        )
                        run_ids.add(run_id)
                        artifacts.update(refs)
                        imported_events += count
                    except (OSError, KeyError, TypeError, ValueError) as exc:
                        errors.append(f"sessions.db:{record.session_id}: {exc}")
                imported.append(str(sessions_db))
            except (OSError, sqlite3.Error, ValueError) as exc:
                errors.append(f"sessions.db: {exc}")
            finally:
                await legacy.close()
        else:
            skipped.append(str(sessions_db))

        if todo_file.is_file():
            try:
                raw = todo_file.read_bytes()
                data = self._yaml(raw, todo_file.name)
                session_id = str(data.get("session_id") or "legacy-todo")
                run_id, count = await self._import_todos(
                    session_id,
                    data,
                    digest_bytes(raw),
                    dry_run=dry_run,
                )
                run_ids.add(run_id)
                imported_events += count
                imported.append(str(todo_file))
            except (OSError, KeyError, TypeError, ValueError, LegacyImportError) as exc:
                errors.append(f"{todo_file}: {exc}")
        else:
            skipped.append(str(todo_file))

        if action_root.is_dir():
            for journal in sorted(action_root.glob("*.jsonl")):
                try:
                    raw = journal.read_bytes()
                    records = self._jsonl(raw, journal.name)
                    by_session: dict[str, list[Mapping[str, Any]]] = {}
                    for record in records:
                        session_id = str(record.get("session_id") or "legacy-action")
                        by_session.setdefault(session_id, []).append(record)
                    for session_id, session_records in by_session.items():
                        run_id, count, unknown = await self._import_actions(
                            session_id,
                            session_records,
                            digest_bytes(raw),
                            dry_run=dry_run,
                        )
                        run_ids.add(run_id)
                        imported_events += count
                        unverified.extend(
                            f"{journal.name}:{action_id} terminal outcome unknown"
                            for action_id in unknown
                        )
                    imported.append(str(journal))
                except (OSError, KeyError, TypeError, ValueError, LegacyImportError) as exc:
                    errors.append(f"{journal}: {exc}")
        else:
            skipped.append(str(action_root))

        if memory_db.is_file():
            try:
                raw = memory_db.read_bytes()
                groups, conversations = self._read_memory_database(memory_db)
                for session_id, records in groups.items():
                    run_id, count = await self._import_memories(
                        session_id,
                        records,
                        digest_bytes(raw),
                        dry_run=dry_run,
                        conversations=conversations.get(session_id, ()),
                    )
                    run_ids.add(run_id)
                    imported_events += count
                    unverified.extend(
                        f"memory:{record.get('id', index)} missing trustworthy source"
                        for index, record in enumerate(records)
                        if not record.get("source")
                    )
                imported.append(str(memory_db))
            except (OSError, sqlite3.Error, KeyError, TypeError, ValueError) as exc:
                errors.append(f"{memory_db}: {exc}")
        else:
            skipped.append(str(memory_db))

        if hold_imported_runs and not dry_run:
            for run_id in sorted(run_ids):
                projection = await self.store.load_projection(run_id)
                if projection.status.value == "queued":
                    await self._append(
                        run_id,
                        "RunBlocked",
                        {
                            "reason": "legacy import requires explicit resume",
                            "source": "legacy_importer",
                        },
                    )
                    imported_events += 1

        report = LegacyImportReport(
            tuple(imported),
            tuple(skipped),
            tuple(errors),
            tuple(sorted(run_ids)),
            tuple(sorted(artifacts)),
            tuple(unverified),
            imported_events,
        )
        if strict and report.errors:
            raise LegacyImportError("; ".join(report.errors))
        return report

    async def _import_session(
        self,
        data: Mapping[str, Any],
        source_digest: str,
        *,
        dry_run: bool,
    ) -> tuple[str, int, tuple[str, ...]]:
        session_id = str(data["session_id"])
        run_id = self._run_id(session_id)
        if dry_run:
            return run_id, 0, ()
        goal_text = next(
            (str(item.get("content")) for item in data.get("messages") or () if item.get("role") == "user"),
            "Imported legacy session",
        )
        goal = GoalContract(goal_text, ("legacy session imported",))
        created = await self.store.run_exists(run_id)
        if not created:
            await self.store.create_run(Run(run_id, goal, self.runtime_contract))
        if await self._has_source(run_id, source_digest):
            return run_id, 0, ()
        count = 0
        if not created:
            await self._append(
                run_id,
                "RunCreated",
                {"goal": goal.to_dict(), "runtime_contract": self.runtime_contract.to_dict()},
            )
            count += 1
        refs: list[str] = []
        messages: list[dict[str, Any]] = []
        for index, message in enumerate(data.get("messages") or ()):
            encoded = json.dumps(dict(message), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ref = self.artifacts.put(encoded, media_type="application/json")
            refs.append(ref.digest)
            messages.append(
                {
                    "index": index,
                    "role": str(message.get("role") or "unknown"),
                    "content_digest": digest_bytes(str(message.get("content") or "").encode()),
                    "artifact": ref.digest,
                }
            )
        payload = {
            "source_digest": source_digest,
            "source_key": "session.json",
            "importer_version": self.VERSION,
            "messages": messages,
            "checkpoints": [dict(item) for item in data.get("checkpoints") or ()],
            "unverified_claims": ["legacy message semantics are preserved as artifacts"],
        }
        count += await self._append_source(run_id, payload, refs)
        if not created:
            await self._append(run_id, "GoalContractAccepted", {"goal": goal.to_dict()})
            await self._append(run_id, "RunQueued", {})
            count += 2
        return run_id, count, tuple(refs)

    async def _import_conflict(self, data: Mapping[str, Any], source_digest: str, *, dry_run: bool) -> tuple[str, int]:
        session_id = str(data.get("session_id") or "legacy-session-001")
        run_id = self._run_id(session_id)
        if dry_run:
            return run_id, 0
        if not await self.store.run_exists(run_id):
            await self._import_session(
                {"session_id": session_id, "messages": [], "checkpoints": []},
                digest_bytes(b"synthetic-session"),
                dry_run=False,
            )
        if await self._has_source(run_id, source_digest):
            return run_id, 0
        count = await self._append_source(
            run_id,
            {
                "source_digest": source_digest,
                "source_key": "session-conflict.json",
                "importer_version": self.VERSION,
                "unverified_claims": ["legacy conflict retained without overwriting canonical message"],
                "conflict": dict(data),
            },
            (),
        )
        return run_id, count

    async def _import_todos(self, session_id: str, data: Mapping[str, Any], source_digest: str, *, dry_run: bool) -> tuple[str, int]:
        run_id = self._run_id(session_id or "legacy-session-001")
        if dry_run:
            return run_id, 0
        if not await self.store.run_exists(run_id):
            await self._import_session({"session_id": session_id or "legacy-session-001", "messages": [], "checkpoints": []}, digest_bytes(b"synthetic-session"), dry_run=False)
        if await self._has_source(run_id, source_digest):
            return run_id, 0
        count = await self._append_source(run_id, {"source_digest": source_digest, "source_key": "todo.yaml", "importer_version": self.VERSION}, ())
        for item in data.get("items") or ():
            todo = {
                "id": str(item["id"]),
                "title": str(item["title"]),
                "status": str(item.get("status") or "pending"),
                "active_sessions": [str(value) for value in item.get("active_sessions") or ()],
            }
            await self._append(run_id, "TodoCreated", {"todo": todo})
            count += 1
        return run_id, count

    async def _import_memories(
        self,
        session_id: str,
        records: list[Mapping[str, Any]],
        source_digest: str,
        *,
        dry_run: bool,
        conversations: Iterable[Mapping[str, Any]] = (),
    ) -> tuple[str, int]:
        run_id = self._run_id(session_id or "legacy-session-001")
        if dry_run:
            return run_id, 0
        if not await self.store.run_exists(run_id):
            await self._import_session({"session_id": session_id or "legacy-session-001", "messages": [], "checkpoints": []}, digest_bytes(b"synthetic-session"), dry_run=False)
        if await self._has_source(run_id, source_digest):
            return run_id, 0
        payload = {
            "source_digest": source_digest,
            "source_key": "memory.jsonl",
            "importer_version": self.VERSION,
            "memory_records": [dict(record) for record in records],
            "conversation_records": [dict(record) for record in conversations],
            "unverified_claims": [str(record.get("id")) for record in records if not record.get("source")],
        }
        return run_id, await self._append_source(run_id, payload, ())

    @staticmethod
    def _read_memory_database(
        path: Path,
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
        """Read memory atoms/conversation rows across legacy schema variants."""

        connection = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
        try:
            table_info = connection.execute("PRAGMA table_info(memories)").fetchall()
            columns = {str(row[1]) for row in table_info}
            selected = [
                "id",
                "text",
                "created_at",
                "updated_at",
                "source",
                "session_id",
                "project_scope",
                "provenance_json",
            ]
            expressions = [name if name in columns else f"NULL AS {name}" for name in selected]
            groups: dict[str, list[dict[str, Any]]] = {}
            for row in connection.execute(f"SELECT {', '.join(expressions)} FROM memories"):
                record = dict(zip(selected, row, strict=True))
                session_id = str(record.get("session_id") or "legacy-memory")
                groups.setdefault(session_id, []).append(record)

            conversations: dict[str, list[dict[str, Any]]] = {}
            conversation_info = connection.execute("PRAGMA table_info(conversation)").fetchall()
            conversation_columns = {str(row[1]) for row in conversation_info}
            if conversation_columns:
                selected_conversation = [
                    "session_id",
                    "turn_idx",
                    "role",
                    "content",
                    "ts",
                ]
                conversation_expressions = [
                    name if name in conversation_columns else f"NULL AS {name}"
                    for name in selected_conversation
                ]
                for row in connection.execute(
                    f"SELECT {', '.join(conversation_expressions)} FROM conversation"
                ):
                    record = dict(zip(selected_conversation, row, strict=True))
                    session_id = str(record.get("session_id") or "legacy-memory")
                    conversations.setdefault(session_id, []).append(record)
            return groups, conversations
        finally:
            connection.close()

    async def _import_actions(self, session_id: str, records: list[Mapping[str, Any]], source_digest: str, *, dry_run: bool) -> tuple[str, int, tuple[str, ...]]:
        run_id = self._run_id(session_id or "legacy-session-001")
        if dry_run:
            return run_id, 0, ()
        if not await self.store.run_exists(run_id):
            await self._import_session({"session_id": session_id or "legacy-session-001", "messages": [], "checkpoints": []}, digest_bytes(b"synthetic-session"), dry_run=False)
        if await self._has_source(run_id, source_digest):
            return run_id, 0, ()
        count = await self._append_source(run_id, {"source_digest": source_digest, "source_key": "action-journal.jsonl", "importer_version": self.VERSION, "unverified_claims": ["legacy action outcomes are not replayed"]}, ())
        projection = await self.store.load_projection(run_id)
        epoch = max(1, projection.lease_epoch + 1)
        await self._append(run_id, "RunClaimed", {"worker_id": "legacy-importer", "expires_at": time.time() + 30.0}, lease_epoch=epoch)
        count += 1
        migrated = migrate_legacy_journal(
            records,
            run_id=run_id,
            runtime_contract_digest=self.runtime_contract.digest,
            start_sequence=(await self.store.load_projection(run_id)).sequence + 1,
            artifact_store=self.artifacts,
            lease_epoch=epoch,
        )
        for event in migrated.events:
            await self.store.append(event, expected_sequence=event.sequence - 1, expected_lease_epoch=epoch)
            count += 1
        projection = await self.store.load_projection(run_id)
        await self._append(run_id, "WorkerLeaseExpired", {"reason": "legacy action journal import complete", "worker_id": "legacy-importer"}, lease_epoch=epoch)
        await self.store.release_lease(run_id, epoch)
        count += 1
        return run_id, count, migrated.unknown_actions

    async def _append_source(self, run_id: str, payload: dict[str, Any], refs: Iterable[str]) -> int:
        await self._append(run_id, "LegacyRunImported", payload, artifact_refs=tuple(refs))
        return 1

    async def _append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        lease_epoch: int = 0,
        artifact_refs: tuple[str, ...] = (),
    ) -> RunEvent:
        projection = await self.store.load_projection(run_id)
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"cc-harness:legacy-event:{run_id}:{projection.sequence + 1}:{event_type}:{payload.get('source_digest', '')}"))
        event = RunEvent.create(
            event_id=event_id,
            run_id=run_id,
            sequence=projection.sequence + 1,
            event_type=event_type,
            actor=self.actor,
            runtime_contract_digest=str(projection.runtime_contract_digest or self.runtime_contract.digest),
            lease_epoch=lease_epoch,
            payload=payload,
            artifact_refs=artifact_refs,
        )
        return await self.store.append(
            event,
            expected_sequence=projection.sequence,
            expected_lease_epoch=(
                0 if event_type == "RunClaimed" else (lease_epoch if lease_epoch else None)
            ),
        )

    async def _has_source(self, run_id: str, source_digest: str) -> bool:
        events = (await self.store.read(run_id, limit=100000)).events
        return any(event.event_type == "LegacyRunImported" and event.payload.get("source_digest") == source_digest for event in events)

    @staticmethod
    def _run_id(session_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cc-harness:legacy-session:{session_id}"))

    @staticmethod
    def _json(raw: bytes, name: str) -> dict[str, Any]:
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"{name} must contain an object")
        return value

    @staticmethod
    def _jsonl(raw: bytes, name: str) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for index, line in enumerate(raw.decode("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{name}:{index} must contain an object")
            values.append(value)
        return values

    @staticmethod
    def _yaml(raw: bytes, name: str) -> dict[str, Any]:
        try:
            import yaml
        except ImportError:
            return _parse_minimal_yaml(raw.decode("utf-8"), name)
        value = yaml.safe_load(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"{name} must contain an object")
        return value

    @staticmethod
    def _load_manifest(root: Path) -> Mapping[str, Any]:
        path = root / "manifest.json"
        if not path.is_file():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _manifest_digest(manifest: Mapping[str, Any], name: str) -> str | None:
        item = (manifest.get("fixtures") or {}).get(name)
        if not isinstance(item, Mapping):
            return None
        value = item.get("sha256")
        return str(value) if value else None


def _parse_minimal_yaml(text: str, name: str) -> dict[str, Any]:
    session_id = ""
    items: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("session_id:"):
            session_id = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("- id:"):
            current = {"id": stripped.split(":", 1)[1].strip()}
            items.append(current)
        elif current is not None and ":" in stripped:
            key, value = stripped.split(":", 1)
            current[key.strip()] = value.strip().strip("[]")
    if not session_id and not items:
        raise ValueError(f"unable to parse {name}")
    return {"session_id": session_id, "items": items}


__all__ = ["LegacyImportError", "LegacyImportReport", "LegacyImporter"]
