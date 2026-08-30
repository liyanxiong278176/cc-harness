from __future__ import annotations

import asyncio

import pytest

from cc_harness.coordinator import RunCoordinator, RunRequest
from cc_harness.lease import LeaseManager
from cc_harness.run_kernel import ModelSegment, ReActKernel
from cc_harness.run_store import RunStore
from cc_harness.supervisor import LocalSupervisor, SupervisorStats
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
        assert supervisor._active[handle.run_id][1].lease_manager.ttl_seconds == 120.0
        await asyncio.sleep(0.2)
        await supervisor.tick()
        assert (await store.load_projection(handle.run_id)).sequence >= 5
        await supervisor.stop(drain=True)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_supervisor_reclaims_expired_lease_from_still_active_task(tmp_path) -> None:
    """A wedged worker must not leave a RUNNING run invisible forever."""

    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    cancelled = asyncio.Event()
    created = 0

    class SlowWorker(RunWorker):
        async def execute(self, lease):
            del lease
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    try:
        handle = await RunCoordinator(store).submit(RunRequest("stale worker", ("segment",)))

        def factory(_run_id):
            nonlocal created
            created += 1
            return SlowWorker(
                store,
                ReActKernel(EmptyModel()),
                worker_id=f"slow-{created}",
                lease_manager=LeaseManager(store, ttl_seconds=1.0),
            )

        supervisor = LocalSupervisor(store, factory, max_workers=1, poll_interval=0.01)
        await supervisor.tick()
        await asyncio.sleep(1.1)
        await supervisor.tick()

        events = (await store.read(handle.run_id)).events
        assert any(event.event_type == "WorkerLeaseExpired" for event in events)
        assert cancelled.is_set()
        await supervisor.stop(drain=False)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_supervisor_tick_error_does_not_stop_recovery_loop(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        supervisor = LocalSupervisor(store, lambda _run_id: RunWorker(
            store,
            ReActKernel(EmptyModel()),
            worker_id="unused",
        ), poll_interval=0.01)
        calls = 0

        async def flaky_tick():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient tick failure")
            return SupervisorStats((), 0)

        monkeypatch.setattr(supervisor, "tick", flaky_tick)
        await supervisor.start()
        await asyncio.sleep(0.05)
        await supervisor.stop()
        assert calls >= 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_supervisor_start_restarts_failed_loop_task(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        supervisor = LocalSupervisor(store, lambda _run_id: RunWorker(
            store,
            ReActKernel(EmptyModel()),
            worker_id="unused",
        ), poll_interval=0.01)
        await supervisor.start()
        first = supervisor._loop_task
        assert first is not None
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await supervisor.start()
        assert supervisor._loop_task is not first
        await supervisor.stop()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_supervisor_bounds_a_stuck_tick(tmp_path, monkeypatch) -> None:
    """A hung tick must not permanently disable lease recovery."""

    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        supervisor = LocalSupervisor(
            store,
            lambda _run_id: RunWorker(store, ReActKernel(EmptyModel()), worker_id="unused"),
            poll_interval=0.01,
            tick_timeout=0.02,
        )
        calls = 0

        async def stuck_tick():
            nonlocal calls
            calls += 1
            await asyncio.Event().wait()

        monkeypatch.setattr(supervisor, "tick", stuck_tick)
        await supervisor.start()
        await asyncio.sleep(0.1)
        await supervisor.stop()
        assert calls >= 2
    finally:
        await store.close()
