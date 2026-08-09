import base64

import pytest

from cc_harness.session_store import SessionStore


@pytest.mark.asyncio
async def test_project_session_store_round_trip_and_delete_attachments(tmp_path):
    store = await SessionStore(tmp_path).open()
    messages = [
        {"role": "user", "content": "hello project"},
        {"role": "assistant", "content": "done"},
    ]
    await store.save("s1", messages, mode="coding")
    assert await store.load("s1") == messages
    latest = await store.latest()
    assert latest is not None
    assert latest.session_id == "s1"
    assert latest.title == "hello project"

    attachment_dir = store.attachments_root / "s1"
    attachment_dir.mkdir(parents=True)
    (attachment_dir / "x.png").write_bytes(b"x")
    await store.delete("s1")
    assert not attachment_dir.exists()
    assert await store.load("s1") == []
    await store.close()


@pytest.mark.asyncio
async def test_session_store_externalizes_and_restores_image_data(tmp_path):
    store = await SessionStore(tmp_path).open()
    payload = base64.b64encode(b"fake-png").decode("ascii")
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "inspect"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{payload}"}},
    ]}]
    await store.save("image-session", messages, mode="coding")

    cursor = await store._db.execute(
        "SELECT content_json FROM session_message WHERE session_id='image-session'"
    )
    stored = (await cursor.fetchone())[0]
    assert "cc-harness-attachment://" in stored
    assert payload not in stored

    loaded = await store.load("image-session")
    assert loaded[0]["content"][1]["image_url"]["url"] == f"data:image/png;base64,{payload}"
    await store.close()


@pytest.mark.asyncio
async def test_checkpoint_restores_messages_events_and_edited_files(tmp_path):
    store = await SessionStore(tmp_path).open()
    messages = [{"role": "user", "content": "before"}]
    events = [{"type": "user_committed", "text": "before", "ts": 1.0}]
    await store.save("rewind-session", messages, mode="coding")
    await store.save_events("rewind-session", events)
    existing = tmp_path / "existing.txt"
    created = tmp_path / "created.txt"
    existing.write_text("old", encoding="utf-8")

    checkpoint = await store.create_checkpoint(
        "rewind-session", "change files", messages, event_count=len(events),
    )
    assert await store.snapshot_checkpoint_file("rewind-session", checkpoint, existing)
    assert await store.snapshot_checkpoint_file("rewind-session", checkpoint, created)
    existing.write_text("new", encoding="utf-8")
    created.write_text("new file", encoding="utf-8")

    restored, removed = await store.restore_checkpoint_files("rewind-session", checkpoint)
    assert (restored, removed) == (1, 1)
    assert existing.read_text(encoding="utf-8") == "old"
    assert not created.exists()
    assert await store.load_checkpoint_messages("rewind-session", checkpoint) == messages
    assert await store.load_checkpoint_events("rewind-session", checkpoint) == events
    records = await store.list_checkpoints("rewind-session")
    assert records[0].label == "change files"
    await store.close()


@pytest.mark.asyncio
async def test_checkpoint_refuses_file_outside_workspace(tmp_path):
    store = await SessionStore(tmp_path / "project").open()
    await store.save("safe", [], mode="coding")
    checkpoint = await store.create_checkpoint("safe", "safe", [], event_count=0)
    assert not await store.snapshot_checkpoint_file(
        "safe", checkpoint, tmp_path / "outside.txt",
    )
    await store.close()


@pytest.mark.asyncio
async def test_explicit_session_title_survives_later_saves(tmp_path):
    store = await SessionStore(tmp_path).open()
    await store.save("named", [{"role": "user", "content": "generated"}], mode="coding")
    await store.rename("named", "My persistent name")
    await store.save("named", [{"role": "user", "content": "different"}], mode="coding")
    records = await store.list_recent()
    assert records[0].title == "My persistent name"
    await store.close()
@pytest.mark.asyncio
async def test_message_source_events_are_append_only_while_projection_updates(tmp_path):
    from cc_harness.session_store import SessionStore

    store = await SessionStore(tmp_path).open()
    try:
        messages = [{"role": "user", "content": "original"}]
        await store.save("s1", messages, mode="coding")
        messages[0] = {"role": "user", "content": "revised"}
        messages.append({"role": "assistant", "content": "answer"})
        await store.save("s1", messages, mode="coding")
        await store.save("s1", messages, mode="coding")

        events = await store.load_message_events("s1")
        assert [event["event_type"] for event in events] == [
            "message_appended",
            "message_revised",
            "message_appended",
        ]
        assert events[0]["message"]["content"] == "original"
        assert events[1]["message"]["content"] == "revised"
        assert all(event["content_digest"].startswith("sha256:") for event in events)
        assert await store.load("s1") == messages
    finally:
        await store.close()
