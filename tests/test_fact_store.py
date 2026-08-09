import sqlite3

import pytest

from cc_harness.fact_store import ArtifactIntegrityError, ProjectFactStore
from cc_harness.session_store import SessionStore


@pytest.mark.asyncio
async def test_fact_store_is_outside_workspace_and_events_are_immutable(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    data_root = tmp_path / "user-data"
    store = await ProjectFactStore(project, data_root=data_root).open()
    assert not (project / ".cc-harness").exists()
    assert store.db_path.is_relative_to(data_root)

    await store.create_session("s1", mode="coding")
    event = await store.append_event(
        "s1", "message_committed", {"message": {"role": "user", "content": "hello"}}
    )
    assert event.seq == 1

    connection = sqlite3.connect(store.db_path)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("UPDATE event SET event_type='changed'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("DELETE FROM event")
    connection.close()
    await store.close()


@pytest.mark.asyncio
async def test_concurrent_appends_allocate_contiguous_sequences(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = await ProjectFactStore(project, data_root=tmp_path / "data").open()
    await store.create_session("parallel", mode="coding")

    import asyncio

    await asyncio.gather(*(
        store.append_event("parallel", "observation", {"index": index})
        for index in range(25)
    ))
    page = await store.read_events("parallel", limit=100)
    assert [event.seq for event in page.events] == list(range(1, 26))
    assert [event.parent_seq for event in page.events] == [None, *range(1, 25)]
    assert page.next_cursor is None
    await store.close()


@pytest.mark.asyncio
async def test_rewind_preserves_raw_history_and_rebuilds_active_projection(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = await ProjectFactStore(project, data_root=tmp_path / "data").open()
    await store.create_session("rewind", mode="coding")
    first = await store.append_event(
        "rewind", "message_committed", {"message": {"role": "user", "content": "keep"}}
    )
    await store.append_event(
        "rewind", "message_committed", {"message": {"role": "assistant", "content": "drop"}}
    )
    await store.rewind("rewind", first.seq)
    await store.append_event(
        "rewind", "message_committed", {"message": {"role": "assistant", "content": "new"}}
    )

    raw = await store.read_events("rewind")
    projection = await store.build_context_projection("rewind")
    assert len(raw.events) == 4
    assert [message["content"] for message in projection.messages] == ["keep", "new"]
    assert raw.events[1].event_id not in projection.event_ids
    await store.close()


@pytest.mark.asyncio
async def test_content_addressed_objects_are_deduplicated_and_verified(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = await ProjectFactStore(project, data_root=tmp_path / "data").open()
    first = await store.put_object(b"complete tool output", media_type="text/plain")
    second = await store.put_object(b"complete tool output", media_type="text/plain")
    assert first.digest == second.digest
    assert await store.read_object(first.digest) == b"complete tool output"

    store._object_path(first.digest).write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError):
        await store.read_object(first.digest)
    await store.close()


@pytest.mark.asyncio
async def test_versioned_summary_is_projection_only_and_invalidates_after_rewind(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = await ProjectFactStore(project, data_root=tmp_path / "data").open()
    await store.create_session("summary", mode="coding")
    first = await store.append_event(
        "summary", "message_committed", {"message": {"role": "user", "content": "one"}}
    )
    second = await store.append_event(
        "summary", "message_committed", {"message": {"role": "assistant", "content": "two"}}
    )
    summary = await store.append_summary(
        "summary",
        "one and two",
        covers_from_seq=first.seq,
        covers_through_seq=second.seq,
        prompt_version="summary-v1",
        model="deepseek-v4-flash",
    )
    await store.append_event(
        "summary", "message_committed", {"message": {"role": "user", "content": "three"}}
    )
    projected = await store.build_context_projection("summary")
    assert projected.summary == summary
    assert projected.summary_text == "one and two"
    assert [message["content"] for message in projected.messages] == ["three"]

    await store.rewind("summary", first.seq)
    rewound = await store.build_context_projection("summary")
    assert rewound.summary is None
    assert [message["content"] for message in rewound.messages] == ["one"]
    assert len((await store.read_events("summary")).events) == 4
    await store.close()


@pytest.mark.asyncio
async def test_legacy_import_is_read_only_idempotent_and_rebuildable(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    legacy = await SessionStore(project).open()
    messages = [
        {"role": "user", "content": "legacy request"},
        {"role": "assistant", "content": "legacy result"},
    ]
    await legacy.save("legacy-session", messages, mode="coding", status="closed")
    await legacy.save_events(
        "legacy-session", [{"type": "assistant_committed", "text": "legacy result"}]
    )
    legacy_path = legacy.db_path
    await legacy.close()
    before = legacy_path.read_bytes()

    store = await ProjectFactStore(project, data_root=tmp_path / "data").open()
    imported = await store.import_legacy(legacy_path)
    repeated = await store.import_legacy(legacy_path)
    projection = await store.build_context_projection("legacy-session")
    raw = await store.read_events("legacy-session")

    assert imported.imported_sessions == 1
    assert imported.imported_events == 3
    assert repeated.skipped_sessions == 1
    assert list(projection.messages) == messages
    assert all(event.payload["legacy_unverified"] for event in raw.events)
    assert legacy_path.read_bytes() == before
    await store.close()
