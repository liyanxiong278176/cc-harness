import pytest

import cc_harness.run_store as store_module
from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness.run_events import EventActor
from cc_harness.run_projection import ProjectionBuilder


@pytest.mark.asyncio
async def test_periodic_checkpoint_matches_full_replay_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, 'PROJECTION_SNAPSHOT_INTERVAL', 10)
    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    try:
        run_id = await client.submit('运行长会话', ('状态一致',))
        while (await client.store.load_run_record(run_id)).sequence < 23:
            await client.coordinator._append(run_id, 'TaskBudgetConfigured', {'limits': {'steps': 1000}}, EventActor('client', 'audit'))
        assert 10 in await client.store.snapshot_sequences(run_id)
        assert 20 in await client.store.snapshot_sequences(run_id)
        events = (await client.store.read(run_id, limit=1000)).events
        replay = ProjectionBuilder().rebuild(events)
        await client.store.close()
        await client.store.open()
        assert (await client.store.load_projection(run_id)).digest == replay.digest
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_periodic_checkpoint_failure_rolls_back_event_and_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, 'PROJECTION_SNAPSHOT_INTERVAL', 10)
    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    try:
        run_id = await client.submit('验证事务边界', ('原子提交',))
        while (await client.store.load_run_record(run_id)).sequence < 9:
            await client.coordinator._append(run_id, 'TaskBudgetConfigured', {'limits': {'steps': 1000}}, EventActor('client', 'audit'))
        before = await client.store.load_projection(run_id)

        async def interrupted(snapshot):
            raise OSError('checkpoint storage interrupted')

        monkeypatch.setattr(client.store, '_insert_snapshot_tx', interrupted)
        with pytest.raises(OSError, match='checkpoint storage interrupted'):
            await client.coordinator._append(run_id, 'TaskBudgetConfigured', {'limits': {'steps': 2000}}, EventActor('client', 'audit'))
        assert (await client.store.load_run_record(run_id)).sequence == 9
        assert not (await client.store.read(run_id, after=9)).events
        assert (await client.store.load_projection(run_id)).digest == before.digest
    finally:
        await client.close()
