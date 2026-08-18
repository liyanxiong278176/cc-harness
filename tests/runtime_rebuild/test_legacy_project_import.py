from __future__ import annotations

import pytest

from cc_harness.legacy_import import LegacyImporter
from cc_harness.run_store import RunStore
from cc_harness.session_store import SessionStore
from cc_harness.worker import RunWorker


@pytest.mark.asyncio
async def test_real_project_legacy_sources_import_idempotently(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    legacy = await SessionStore(project).open()
    await legacy.save(
        "legacy-session-1",
        [{"role": "user", "content": "preserve this request"}],
        mode="coding",
    )
    await legacy.close()
    source_size = (project / ".cc-harness" / "sessions.db").stat().st_size

    store = RunStore(project, data_root=tmp_path / "new-data")
    await store.open()
    try:
        importer = LegacyImporter(store)
        first = await importer.import_project(project)
        assert first.errors == ()
        assert first.imported_events >= 3
        assert first.run_ids
        second = await importer.import_project(project)
        assert second.imported_events == 0
        assert (project / ".cc-harness" / "sessions.db").stat().st_size == source_size
        events = (await store.read(first.run_ids[0])).events
        assert any(event.event_type == "LegacyRunImported" for event in events)
        projection = await store.load_projection(first.run_ids[0])
        assert projection.status.value == "blocked"
        messages = await RunWorker(store, object(), worker_id="context-check")._messages_for_projection(projection)
        assert "preserve this request" in str(messages[-1]["content"])
    finally:
        await store.close()
