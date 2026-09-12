from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from cc_harness.webui import (
    WebRuntimeManager,
    WebSettingsStore,
    _public_capability_details,
    _visible_assistant_text,
    create_web_app,
)
from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness.lease import LeaseManager
from cc_harness.run_events import EventActor, RunEvent
from cc_harness.run_model import GoalContract, Run, RunStatus


def _client(tmp_path: Path) -> tuple[WebRuntimeManager, TestClient]:
    manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / "runtime-data")
    manager.settings = WebSettingsStore(tmp_path / "settings.json")
    client = TestClient(create_web_app(manager))
    client.__enter__()
    return manager, client


def test_webui_bootstrap_and_project_gate(tmp_path: Path) -> None:
    manager, client = _client(tmp_path)
    try:
        assert client.get("/api/health").json()["runtime"] == "durable"
        assert client.get("/api/bootstrap").json()["project"] is None
        response = client.post("/api/messages", json={"text": "先读项目"})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "project_required"
    finally:
        client.__exit__(None, None, None)
        assert manager._clients == {}


def test_webui_settings_mask_and_explicit_reveal(tmp_path: Path) -> None:
    manager, client = _client(tmp_path)
    try:
        response = client.post(
            "/api/settings",
            json={
                "base_url": "https://api.example.test/v1",
                "model": "demo-model",
                "api_key": "sk-test-secret-value",
            },
        )
        assert response.status_code == 200
        public = client.get("/api/settings").json()
        assert public["has_api_key"] is True
        assert public["api_key_masked"] != "sk-test-secret-value"
        assert "api_key" not in public
        revealed = client.get("/api/settings?reveal=true").json()
        assert revealed["api_key"] == "sk-test-secret-value"
    finally:
        client.__exit__(None, None, None)
        assert manager._clients == {}


def test_webui_permission_mode_is_persisted_and_validated(tmp_path: Path) -> None:
    manager, client = _client(tmp_path)
    try:
        response = client.post("/api/settings", json={"permission_mode": "auto-edit"})
        assert response.status_code == 200
        assert response.json()["settings"]["permission_mode"] == "auto-edit"
        assert client.get("/api/settings").json()["permission_mode"] == "auto-edit"

        invalid = client.post("/api/settings", json={"permission_mode": "unsafe"})
        assert invalid.status_code == 400
        assert invalid.json()["detail"]["code"] == "invalid_permission_mode"
    finally:
        client.__exit__(None, None, None)
        assert manager._clients == {}


def test_project_selection_lists_empty_durable_sessions(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manager, client = _client(tmp_path)
    try:
        response = client.post("/api/projects/select", json={"path": str(project)})
        assert response.status_code == 200
        assert response.json()["selected"] is True
        assert response.json()["settings"]["permission_mode"] == "default"
        assert response.json()["sessions"] == []
    finally:
        client.__exit__(None, None, None)
        assert manager._clients == {}


def test_completion_candidate_is_not_rendered_as_chat_text() -> None:
    candidate = '{"acceptance_criteria":["request addressed"],"evidence":["test passed"]}'
    assert _visible_assistant_text(candidate) == ""
    assert _visible_assistant_text(
        '已完成实现。\n```json\n{"acceptance_criteria":["request addressed"],"evidence":[]}\n```'
    ) == "已完成实现。"
    assert _visible_assistant_text("普通 JSON: {\"answer\": \"ok\"}") == '普通 JSON: {"answer": "ok"}'


def test_webui_capability_projection_honors_memory_kill_switch() -> None:
    """A stale safety snapshot must not claim disabled memory is active."""

    capabilities = {
        "memory": {"enabled": True, "details": {"configured_enabled": False}},
        "safety": {"enabled": True},
    }
    details = _public_capability_details(
        capabilities,
        "safety",
        {
            "memory_capture_enabled": True,
            "memory_layered_injection": True,
            "policy_enabled": True,
        },
    )
    assert details["memory_capture_enabled"] is False
    assert details["memory_layered_injection"] is False
    assert details["policy_enabled"] is True


def test_webui_capability_projection_marks_memory_disabled() -> None:
    """The top-level status flag follows MEMORY_ENABLED, not initialization."""

    state = {"enabled": True, "initialized": True, "triggered": False,
             "details": {"configured_enabled": False}}
    details = _public_capability_details(state | {}, "memory", state["details"])
    assert details["configured_enabled"] is False


def test_tree_events_include_child_runs_without_duplicate_user_handoff(tmp_path: Path) -> None:
    """Follow-up assistant activity must be visible through the WebUI stream."""

    async def exercise() -> None:
        project = tmp_path / "project"
        project.mkdir()
        manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / "runtime-data")
        client = await DurableRuntimeClient.create(project, data_root=tmp_path / "runtime-data")
        manager.selected_root = project.resolve()
        manager._clients[str(project.resolve())] = client
        try:
            root_id = await client.submit("root task")
            root = await client.store.load_projection(root_id)
            assert root.runtime_contract is not None
            child_id = str(uuid.uuid4())
            child_goal = GoalContract("follow-up task", ("follow-up request addressed",))
            await client.store.create_run(
                Run(
                    run_id=child_id,
                    goal=child_goal,
                    runtime_contract=root.runtime_contract,
                    parent_run_id=root_id,
                    predecessor_run_id=root_id,
                )
            )
            await client.store.append(
                RunEvent.create(
                    run_id=child_id,
                    sequence=1,
                    event_type="RunCreated",
                    actor=EventActor("client", "test"),
                    runtime_contract_digest=root.runtime_contract.digest,
                    payload={"goal": child_goal.to_dict(), "runtime_contract": root.runtime_contract.to_dict()},
                ),
                expected_sequence=0,
            )
            items, cursors = await manager.tree_events(root_id, cursors={root_id: 10_000})
            assert cursors[child_id] == 1
            child_item = next(item for item in items if item["run_id"] == child_id)
            assert child_item["kind"] == "status"
            assert child_item.get("content") is None
        finally:
            await client.close()

    asyncio.run(exercise())


def test_webui_follow_up_resumes_stalled_conversation_in_same_run(tmp_path: Path) -> None:
    """A plain assistant answer must not strand the next chat turn.

    Coding tasks still require a CompletionCandidate.  A conversational turn
    that reached the safe ``stalled`` boundary, however, can accept the next
    user message as a durable ``RunResumed`` event so the previous transcript
    remains in the model context instead of waiting behind a child queue item.
    """

    async def exercise() -> None:
        project = tmp_path / "project"
        project.mkdir()
        manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / "runtime-data")
        client = await DurableRuntimeClient.create(project, data_root=tmp_path / "runtime-data")
        manager.selected_root = project.resolve()
        manager._clients[str(project.resolve())] = client
        # Avoid starting a real provider worker; this test exercises only the
        # WebUI routing decision and durable event written by ``resume``.
        client.start_supervisor = AsyncMock()  # type: ignore[method-assign]
        try:
            run_id = await client.submit("你好")
            lease = await LeaseManager(client.store).claim(run_id, "test-worker")
            await client.coordinator._append(
                run_id,
                "RunStalled",
                {"reason": "no verifiable progress"},
                EventActor("worker", "test-worker"),
            )
            await LeaseManager(client.store).release(lease)
            view = await client.coordinator.inspect(run_id)
            assert view.status is RunStatus.STALLED

            result = await manager.send_message("继续说明", session_id=run_id)
            assert result["continuation"] == "same_run"
            assert result["session_id"] == run_id
            resumed = await client.coordinator.inspect(run_id)
            assert resumed.status is RunStatus.QUEUED
            events = (await client.store.read(run_id, limit=100)).events
            resume_event = next(event for event in events if event.event_type == "RunResumed")
            assert resume_event.payload["reason"] == "继续说明"
        finally:
            await client.close()

    asyncio.run(exercise())


def test_webui_can_explicitly_resume_goal_review_block(tmp_path: Path) -> None:
    """A preflight review must expose a recoverable path instead of a dead end."""

    async def exercise() -> None:
        project = tmp_path / "project"
        project.mkdir()
        manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / "runtime-data")
        client = await DurableRuntimeClient.create(project, data_root=tmp_path / "runtime-data")
        manager.selected_root = project.resolve()
        manager._clients[str(project.resolve())] = client
        client.start_supervisor = AsyncMock()  # type: ignore[method-assign]
        try:
            run_id = await client.submit("deploy the completed service to production")
            assert (await client.coordinator.inspect(run_id)).status is RunStatus.BLOCKED

            result = await manager.send_message("确认目标范围，继续执行", session_id=run_id)
            assert result["continuation"] == "same_run"
            assert (await client.coordinator.inspect(run_id)).status is RunStatus.QUEUED
        finally:
            await client.close()

    asyncio.run(exercise())
