from pathlib import Path

import pytest

from cc_harness.legacy_import import LegacyImporter
from cc_harness.run_store import RunStore


@pytest.mark.asyncio
async def test_legacy_import_is_auditable_and_idempotent(tmp_path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "legacy"
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        importer = LegacyImporter(store)
        first = await importer.import_directory(fixture)
        assert "session.json" in first.imported_sources
        assert "corrupt.json: Expecting value" in " ".join(first.errors)
        assert first.artifact_digests
        assert any("missing trustworthy source" in value for value in first.unverified_claims)
        run_id = first.run_ids[0]
        events = (await store.read(run_id, limit=1000)).events
        assert any(event.event_type == "LegacyRunImported" for event in events)
        assert any(event.event_type == "ActionOutcomeUnknown" for event in events)
        second = await importer.import_directory(fixture)
        assert second.imported_events == 0
        assert second.skipped_sources == ()
        assert (await store.load_projection(run_id)).sequence == len(events)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_legacy_dry_run_does_not_create_run(tmp_path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "legacy"
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        report = await LegacyImporter(store).import_directory(fixture, dry_run=True)
        assert report.run_ids
        assert await store.list_runs() == ()
    finally:
        await store.close()
