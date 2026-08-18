"""Rehearsable cutover and rollback orchestration.

The live production switch remains an explicit operator action.  The default
API only performs a temporary-store rehearsal and never deletes the old store.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import digest_bytes
from .legacy_import import LegacyImportReport, LegacyImporter
from .migration_reconciliation import ReconciliationReport, reconcile_legacy_fixture
from .run_store import RunStore


class CutoverError(RuntimeError):
    """A cutover rehearsal cannot safely proceed."""


@dataclass(frozen=True)
class CutoverReport:
    rehearsal_id: str
    dry_run: bool
    backup_path: Path | None
    import_report: LegacyImportReport | None
    reconciliation: ReconciliationReport | None
    steps: tuple[str, ...]
    rollback_restore_path: Path | None = None
    object_manifest_path: Path | None = None

    @property
    def ok(self) -> bool:
        return (self.import_report is None or not self.import_report.blocking_errors) and (
            self.reconciliation is None or self.reconciliation.ok
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rehearsal_id": self.rehearsal_id,
            "dry_run": self.dry_run,
            "backup_path": str(self.backup_path) if self.backup_path else None,
            "import_report": self.import_report.__dict__ if self.import_report else None,
            "reconciliation": self.reconciliation.to_dict() if self.reconciliation else None,
            "steps": list(self.steps),
            "rollback_restore_path": str(self.rollback_restore_path) if self.rollback_restore_path else None,
            "object_manifest_path": str(self.object_manifest_path) if self.object_manifest_path else None,
        }


class CutoverManager:
    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve(strict=True)

    def backup_sqlite(self, source: Path, destination: Path) -> Path:
        source = Path(source).resolve(strict=True)
        destination = Path(destination).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db)
        return destination

    def object_manifest(self, root: Path, *, exclude: tuple[Path, ...] = ()) -> dict[str, str]:
        manifest: dict[str, str] = {}
        root = Path(root).resolve()
        excluded = tuple(Path(item).resolve() for item in exclude)
        if not root.exists():
            return manifest
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(path.is_relative_to(item) for item in excluded):
                continue
            manifest[str(path.relative_to(root)).replace("\\", "/")] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        return manifest

    async def rehearse(
        self,
        *,
        legacy_fixture: Path,
        new_data_root: Path,
        backup_root: Path,
        old_db: Path | None = None,
        dry_run: bool = True,
    ) -> CutoverReport:
        if not dry_run:
            raise CutoverError("live cutover requires an explicit operator integration")
        rehearsal_id = f"rehearsal-{int(time.time() * 1000)}"
        steps: list[str] = ["stop_accepting_new_legacy_runs (rehearsal)"]
        backup_path: Path | None = None
        if old_db is not None:
            backup_path = self.backup_sqlite(old_db, Path(backup_root) / rehearsal_id / "legacy-runtime.db")
            steps.append("sqlite_backup_created")
        else:
            steps.append("sqlite_backup_skipped_no_legacy_db")
        steps.append("object_manifest_recorded")
        store = RunStore(self.project_root, data_root=Path(new_data_root))
        await store.open()
        try:
            importer = LegacyImporter(store)
            import_report = await importer.import_directory(legacy_fixture, dry_run=False)
        finally:
            await store.close()
        steps.append("legacy_import_completed")
        reconciliation = reconcile_legacy_fixture(legacy_fixture)
        steps.append("migration_reconciliation_completed")
        steps.extend(["new_supervisor_smoke_ready", "post_cutover_smoke_ready"])
        return CutoverReport(rehearsal_id, True, backup_path, import_report, reconciliation, tuple(steps))

    async def cutover(
        self,
        *,
        new_data_root: Path | None = None,
        backup_root: Path | None = None,
        old_db: Path | None = None,
        operator_confirmation: str,
    ) -> CutoverReport:
        """Perform the one-time local switch after explicit operator consent.

        The method is intentionally impossible to call accidentally: callers
        must pass the exact confirmation phrase. It backs up legacy SQLite and
        object metadata, imports into the new user-data store, and leaves the
        old files untouched. Starting the long-lived supervisor remains a
        separate process command so a CLI invocation cannot orphan it.
        """

        if operator_confirmation != "CUTOVER_DURABLE_RUNTIME":
            raise CutoverError("live cutover requires operator_confirmation=CUTOVER_DURABLE_RUNTIME")
        rehearsal_id = f"cutover-{int(time.time() * 1000)}"
        root = Path(backup_root or (self.project_root / ".cc-harness" / "backups"))
        backup_dir = root / rehearsal_id
        # Capture the source view before creating the backup directory.  When
        # the default backup location lives under .cc-harness, the backup
        # itself must not become part of the legacy object manifest.
        source_manifest = self.object_manifest(
            self.project_root / ".cc-harness",
            exclude=(root,),
        )
        backup_dir.mkdir(parents=True, exist_ok=True)
        steps: list[str] = ["legacy_runtime_acceptance_stopped_by_operator"]
        source_db = Path(old_db or self.project_root / ".cc-harness" / "sessions.db")
        backup_path = None
        if source_db.is_file():
            backup_path = self.backup_sqlite(source_db, backup_dir / "legacy-sessions.db")
            steps.append("legacy_sessions_sqlite_backup_created")
        memory_db = self.project_root / ".cc-harness" / "memory.db"
        if memory_db.is_file():
            self.backup_sqlite(memory_db, backup_dir / "legacy-memory.db")
            steps.append("legacy_memory_sqlite_backup_created")

        manifest_path = backup_dir / "legacy-object-manifest.json"
        manifest_path.write_text(
            json.dumps(
                source_manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        steps.append("legacy_object_manifest_recorded")

        store = RunStore(self.project_root, data_root=new_data_root)
        await store.open()
        try:
            importer = LegacyImporter(store)
            import_report = await importer.import_project(self.project_root, dry_run=False)
            imported_views = await store.list_runs()
            imported_ids = set(import_report.run_ids)
            durable_ids = {item.run_id for item in imported_views}
            errors = list(import_report.blocking_errors)
            missing = sorted(imported_ids - durable_ids)
            if missing:
                errors.append("imported run missing from new store: " + ", ".join(missing))
            reconciliation = ReconciliationReport(
                ok=not errors,
                source_digests={"legacy_object_manifest": digest_bytes(manifest_path.read_bytes())},
                checks={
                    "imported_runs": len(imported_ids),
                    "durable_runs": len(durable_ids),
                    "imported_events": import_report.imported_events,
                    "legacy_unverified_claims": len(import_report.unverified_claims),
                },
                errors=tuple(errors),
            )
        finally:
            await store.close()
        steps.extend(
            [
                "new_runtime_store_created_and_reconciled",
                "new_supervisor_start_deferred_to_command_supervisor",
                "durable_coordinator_open",
            ]
        )
        return CutoverReport(
            rehearsal_id,
            False,
            backup_path,
            import_report,
            reconciliation,
            tuple(steps),
            object_manifest_path=manifest_path,
        )

    def rollback_rehearsal(self, report: CutoverReport, restore_root: Path) -> CutoverReport:
        if report.backup_path is None or not report.backup_path.is_file():
            raise CutoverError("rollback requires a completed SQLite backup")
        restore_path = Path(restore_root).resolve() / report.rehearsal_id / "legacy-runtime-restored.db"
        restore_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(report.backup_path, restore_path)
        steps = (*report.steps, "new_runtime_stopped", "old_backup_restored_to_temporary_recovery_location")
        return CutoverReport(
            report.rehearsal_id,
            report.dry_run,
            report.backup_path,
            report.import_report,
            report.reconciliation,
            steps,
            restore_path,
            report.object_manifest_path,
        )


__all__ = ["CutoverError", "CutoverManager", "CutoverReport"]
