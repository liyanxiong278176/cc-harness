from types import SimpleNamespace

import pytest

from cc_harness.memory.store import MemoryStore
from cc_harness.memory.worker import LayeredMemoryWorker


class _Pipeline:
    def __init__(self):
        self.calls = []
        self._service = SimpleNamespace(embedder=None)

    async def maybe_run(self, messages, counter, context_window, **kwargs):
        del counter, context_window
        self.calls.append((messages, kwargs))
        return None


def _config():
    return SimpleNamespace(
        pipeline_every_n=1,
        scenario_min_atoms=8,
        persona_trigger_every_n=50,
    )


@pytest.mark.asyncio
async def test_worker_durably_processes_enqueued_turn(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", embedding_dim=4)
    await store.init_schema()
    pipeline = _Pipeline()
    events = []
    worker = LayeredMemoryWorker(
        store=store,
        pipeline=pipeline,
        config=_config(),
        context_window=128_000,
        scenarios_dir=tmp_path / "scenarios",
        persona_path=tmp_path / "persona.md",
        artifact_dir=tmp_path / "pipeline",
        event_callback=events.append,
    )
    await worker.start()
    assert await worker.enqueue("session-1", 1, [{"role": "user", "content": "remember"}])
    assert not await worker.enqueue(
        "session-1", 1, [{"role": "user", "content": "duplicate"}]
    )
    assert await worker.flush(timeout_s=2.0)

    cur = await store._db.execute(
        "SELECT status,attempts FROM memory_pipeline_job WHERE session_id='session-1'"
    )
    assert await cur.fetchone() == ("done", 1)
    assert len(pipeline.calls) == 1
    assert any(event.get("stage") == "extracted" for event in events)
    assert len(list((tmp_path / "pipeline").glob("*.json"))) == 1
    await worker.stop()
    await store.close()


@pytest.mark.asyncio
async def test_worker_recovers_interrupted_running_job(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", embedding_dim=4)
    await store.init_schema()
    now = 1.0
    await store._db.execute(
        "INSERT INTO memory_pipeline_job("
        "id,session_id,turn_idx,payload_json,status,attempts,created_at,updated_at) "
        "VALUES('job','session-1',2,'[]','running',1,?,?)",
        (now, now),
    )
    await store._db.commit()
    pipeline = _Pipeline()
    worker = LayeredMemoryWorker(
        store=store,
        pipeline=pipeline,
        config=_config(),
        context_window=128_000,
        scenarios_dir=tmp_path / "scenarios",
        persona_path=tmp_path / "persona.md",
        artifact_dir=tmp_path / "pipeline",
    )
    await worker.start()
    assert await worker.flush(timeout_s=2.0)
    cur = await store._db.execute(
        "SELECT status,attempts FROM memory_pipeline_job WHERE id='job'"
    )
    assert await cur.fetchone() == ("done", 2)
    await worker.stop()
    await store.close()
