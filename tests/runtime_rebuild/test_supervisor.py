from __future__ import annotations

import asyncio

import pytest

from cc_harness.coordinator import RunCoordinator, RunRequest
from cc_harness.run_kernel import ModelSegment, ReActKernel
from cc_harness.run_store import RunStore
from cc_harness.supervisor import LocalSupervisor
from cc_harness.worker import RunWorker


class EmptyModel:
    async def complete(self, messages, tools):
        return ModelSegment(text="segment")


@pytest.mark.asyncio
async def test_supervisor_detaches_worker_from_client_tick(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        handle = await RunCoordinator(store).submit(RunRequest("supervise", ("segment",)))

        def factory(_run_id):
            return RunWorker(store, ReActKernel(EmptyModel()), worker_id="worker-supervisor")

        supervisor = LocalSupervisor(store, factory, max_workers=1, poll_interval=0.01)
        stats = await supervisor.tick()
        assert handle.run_id in stats.active_runs
        await asyncio.sleep(0.05)
        await supervisor.tick()
        assert (await store.load_projection(handle.run_id)).sequence >= 5
        await supervisor.stop(drain=True)
    finally:
        await store.close()
