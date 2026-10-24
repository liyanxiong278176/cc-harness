from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from fastapi.testclient import TestClient

from cc_harness.webui import WebRuntimeManager, WebSettingsStore, create_web_app


def test_explicit_settings_project_survives_another_browser_selection(tmp_path: Path):
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    manager = WebRuntimeManager(initial_cwd=first, data_root=tmp_path / 'runtime')
    manager.settings = WebSettingsStore(tmp_path / 'settings.json')
    with TestClient(create_web_app(manager)) as client:
        client.post('/api/projects/select', json={'path': str(second)})
        response = client.post('/api/settings', json={
            'permission_mode': 'auto-edit',
            'project_scoped': True,
            'project_root': str(first),
        })
        assert response.status_code == 200
        assert response.json()['settings']['permission_mode'] == 'auto-edit'
        assert manager.settings._effective_sync(first)['permission_mode'] == 'auto-edit'
        assert manager.settings._effective_sync(second)['permission_mode'] == 'default'
        assert manager.selected_root == second


def test_missing_explicit_settings_project_does_not_fall_back(tmp_path: Path):
    manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / 'runtime')
    manager.settings = WebSettingsStore(tmp_path / 'settings.json')
    with TestClient(create_web_app(manager)) as client:
        response = client.post('/api/settings', json={
            'permission_mode': 'auto-edit',
            'project_scoped': True,
            'project_root': str(tmp_path / 'missing'),
        })
        assert response.status_code == 400
        assert manager.settings._effective_sync(tmp_path)['permission_mode'] == 'default'


@pytest.mark.asyncio
async def test_reading_old_session_does_not_retarget_selected_workspace(tmp_path):
    from cc_harness.durable_runtime import DurableRuntimeClient

    first = tmp_path / 'first'
    second = tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    client = await DurableRuntimeClient.create(first, data_root=tmp_path / 'runtime')
    manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / 'runtime')
    manager._clients[str(first)] = client
    manager.selected_root = second
    manager._start_pending_if_configured = AsyncMock()
    try:
        run_id = await client.submit('读取历史会话', ('已读取',))
        assert await manager._client_for_run(run_id) is client
        assert manager.selected_root == second
        manager._project_roots = AsyncMock(side_effect=AssertionError('known session should not rescan registry'))
        original_load = client.store.load_run_record
        client.store.load_run_record = AsyncMock(wraps=original_load)
        assert await manager._client_for_run(run_id) is client
        client.store.load_run_record.assert_awaited_once_with(run_id)
    finally:
        # The lookup also opens the selected project's store before finding
        # the historical run; close every client created by the manager.
        await manager.shutdown()
