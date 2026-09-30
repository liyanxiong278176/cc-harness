from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness.webui import WebRuntimeManager, create_web_app


class _Request:
    headers: dict[str, str] = {}

    def __init__(self) -> None:
        self.calls = 0

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return self.calls > 1


@pytest.mark.asyncio
async def test_events_endpoint_replays_durable_events_before_waiting_for_live_stream(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / "runtime-data")
    client = await DurableRuntimeClient.create(project, data_root=tmp_path / "runtime-data")
    manager.selected_root = project.resolve()
    manager._clients[str(project.resolve())] = client
    try:
        run_id = await client.submit("replay this run")
        app = create_web_app(manager)
        route = next(
            route
            for route in app.routes
            if getattr(route, "path", None) == "/api/sessions/{run_id}/events"
        )
        response = await route.endpoint(run_id, _Request(), after=0)
        first = await response.body_iterator.__anext__()
        text = first.decode("utf-8") if isinstance(first, bytes) else first
        assert "event: runtime" in text
        assert run_id in text
        assert "event: stream" not in text
        await response.body_iterator.aclose()
    finally:
        await client.close()
        await manager.live_stream.close()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_events_endpoint_uses_last_event_id_as_durable_cursor(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / "runtime-data")
    client = await DurableRuntimeClient.create(project, data_root=tmp_path / "runtime-data")
    manager.selected_root = project.resolve()
    manager._clients[str(project.resolve())] = client
    try:
        run_id = await client.submit("resume from the current durable cursor")
        page = await client.store.read(run_id, limit=100)
        cursor = page.events[-1].sequence
        observed_cursors: list[dict[str, int]] = []

        async def capture_tree_events(
            _requested_run_id: str, *, cursors: dict[str, int]
        ) -> tuple[list[dict[str, object]], dict[str, int]]:
            observed_cursors.append(dict(cursors))
            return [], dict(cursors)

        manager.tree_events = capture_tree_events  # type: ignore[method-assign]
        app = create_web_app(manager)
        route = next(
            route
            for route in app.routes
            if getattr(route, "path", None) == "/api/sessions/{run_id}/events"
        )
        request = _Request()
        request.headers = {"last-event-id": f"{run_id}:{cursor}"}
        response = await route.endpoint(run_id, request, after=0)
        first = await response.body_iterator.__anext__()
        assert first
        assert observed_cursors
        assert observed_cursors[0][run_id] == cursor
        await response.body_iterator.aclose()
    finally:
        await client.close()
        await manager.live_stream.close()
        await asyncio.sleep(0)

