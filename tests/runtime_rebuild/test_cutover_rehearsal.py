import sqlite3
from types import SimpleNamespace
from pathlib import Path

import pytest

from cc_harness.capability_gate import evaluate_capability_continuity
from cc_harness.cutover import CutoverError, CutoverManager


@pytest.mark.asyncio
async def test_cutover_rehearsal_and_rollback_preserve_both_stores(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    old_db = tmp_path / "old" / "legacy.db"
    old_db.parent.mkdir()
    with sqlite3.connect(old_db) as db:
        db.execute("CREATE TABLE marker(value TEXT)")
        db.execute("INSERT INTO marker VALUES ('legacy')")
        db.commit()
    manager = CutoverManager(project)
    report = await manager.rehearse(
        legacy_fixture=Path(__file__).parent / "fixtures" / "legacy",
        new_data_root=tmp_path / "new-data",
        backup_root=tmp_path / "backups",
        old_db=old_db,
    )
    assert report.ok
    assert report.backup_path and report.backup_path.is_file()
    rolled_back = manager.rollback_rehearsal(report, tmp_path / "restore")
    assert rolled_back.rollback_restore_path and rolled_back.rollback_restore_path.is_file()
    assert (tmp_path / "new-data").exists()
    with sqlite3.connect(rolled_back.rollback_restore_path) as db:
        assert db.execute("SELECT value FROM marker").fetchone()[0] == "legacy"


@pytest.mark.asyncio
async def test_live_cutover_requires_explicit_confirmation_and_preserves_backup(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    old_db = tmp_path / "old" / "legacy.db"
    old_db.parent.mkdir()
    with sqlite3.connect(old_db) as db:
        db.execute("CREATE TABLE marker(value TEXT)")
        db.execute("INSERT INTO marker VALUES ('legacy')")
        db.commit()
    manager = CutoverManager(project)
    with pytest.raises(CutoverError):
        await manager.cutover(
            new_data_root=tmp_path / "new-data",
            backup_root=tmp_path / "backups",
            old_db=old_db,
            operator_confirmation="no",
        )
    report = await manager.cutover(
        new_data_root=tmp_path / "new-data",
        backup_root=tmp_path / "backups",
        old_db=old_db,
        operator_confirmation="CUTOVER_DURABLE_RUNTIME",
        continuity=evaluate_capability_continuity(
            [
                SimpleNamespace(event_type="ModelInvocationStarted", payload={}, actor="worker"),
                SimpleNamespace(event_type="ContextProjectionBuilt", payload={}, actor="worker"),
                SimpleNamespace(event_type="ToolObservationCommitted", payload={}, actor="worker"),
            ]
        ),
    )
    assert report.ok
    assert report.dry_run is False
    assert report.object_manifest_path and report.object_manifest_path.is_file()
    assert report.backup_path and report.backup_path.is_file()
