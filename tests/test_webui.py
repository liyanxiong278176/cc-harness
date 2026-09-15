from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from cc_harness.webui import (
    WebRuntimeManager,
    WebSettingsStore,
    _public_capability_details,
    _public_event,
    _visible_assistant_text,
    classify_interaction_mode,
    create_web_app,
)
from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness.lease import LeaseManager
from cc_harness.run_kernel import ModelSegment, ReActKernel
from cc_harness.run_events import EventActor, RunEvent
from cc_harness.run_model import GoalContract, Run, RunStatus
from cc_harness.run_store import RunNotFound, SupervisorLeaseConflict
from cc_harness.supervisor import LocalSupervisor
from cc_harness.worker import RunWorker


def test_webui_classifies_only_short_tool_free_prompts_as_conversation() -> None:
    assert classify_interaction_mode("你好") == "conversation"
    assert classify_interaction_mode("你是谁？") == "conversation"
    assert classify_interaction_mode("请用中文简短回复，不要执行工具") == "conversation"
    assert classify_interaction_mode("帮我检查项目结构") == "coding"
    assert classify_interaction_mode("解释这个文件") == "coding"
    assert classify_interaction_mode("你好", "coding") == "coding"
    assert classify_interaction_mode("你好", "conversation") == "conversation"


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


def test_webui_returns_not_found_for_stale_session_id(tmp_path: Path) -> None:
    """A removed browser session must not produce an endless 400 poll loop."""

    project = tmp_path / "project"
    project.mkdir()
    manager, client = _client(tmp_path)
    try:
        selected = client.post("/api/projects/select", json={"path": str(project)})
        assert selected.status_code == 200
        response = client.get("/api/sessions/not-a-real-run")
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "session_not_found"
    finally:
        client.__exit__(None, None, None)
        assert manager._clients == {}


def test_webui_delete_session_hides_tree_and_retains_audit_events(tmp_path: Path) -> None:
    """The sidebar delete action tombstones a run instead of rewriting facts."""

    async def exercise() -> None:
        project = tmp_path / "project"
        project.mkdir()
        manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / "runtime-data")
        runtime = await DurableRuntimeClient.create(project, data_root=tmp_path / "runtime-data")
        manager.selected_root = project.resolve()
        manager._clients[str(project.resolve())] = runtime
        try:
            run_id = await runtime.submit("会话删除测试")
            before = (await runtime.store.read(run_id, limit=20)).events
            assert before and before[0].event_type == "RunCreated"

            result = await manager.delete_session(run_id)
            assert result["deleted"] is True
            assert result["audit_retained"] is True
            assert await runtime.store.list_runs() == ()
            with pytest.raises(RunNotFound):
                await runtime.store.load_run_record(run_id)
            # The immutable stream remains readable only through the audit
            # store's physical records; the normal control path is hidden.
            db = runtime.store._require_db()
            cursor = await db.execute(
                "SELECT COUNT(*) FROM run_event WHERE run_id = ?", (run_id,)
            )
            assert int((await cursor.fetchone())[0]) >= len(before)
            assert await manager.sessions() == []
        finally:
            await runtime.close()

    asyncio.run(exercise())


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


def test_webui_treats_existing_supervisor_as_control_plane_owner(tmp_path: Path) -> None:
    """A second UI can resume durable work without a lease-conflict 500."""

    async def exercise() -> None:
        project = tmp_path / "project"
        project.mkdir()
        manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / "runtime-data")
        manager.settings = WebSettingsStore(tmp_path / "settings.json")
        await manager.settings.update(
            {
                "base_url": "https://provider.example/v1",
                "model": "test-model",
                "api_key": "test-key",
            }
        )
        client = await DurableRuntimeClient.create(project, data_root=tmp_path / "runtime-data")
        client.start_supervisor = AsyncMock(  # type: ignore[method-assign]
            side_effect=SupervisorLeaseConflict("owned by supervisor-other")
        )
        manager.selected_root = project.resolve()
        manager._clients[str(project.resolve())] = client
        try:
            resolved = await manager._client_for_root(project, start=True)
            assert resolved is client
            assert client.supervisor is None
            assert client.supervisor_owned_elsewhere is True
            assert manager._scheduler_status(client) == {
                "mode": "external",
                "running": False,
                "label": "其他窗口运行中",
            }

            run_id = await client.submit("resume after another UI owns the scheduler")
            await client.coordinator.cancel(run_id, "prepare resume test")
            client.continue_run = AsyncMock(return_value=run_id)  # type: ignore[method-assign]
            await manager.resume(run_id)
            client.continue_run.assert_awaited_once()
        finally:
            await client.close()

    asyncio.run(exercise())


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


def test_webui_rejection_wakes_runtime_and_continues(tmp_path: Path) -> None:
    """Rejecting one approval must cancel only that action and resume the Run."""

    async def exercise() -> None:
        project = tmp_path / "project"
        project.mkdir()
        manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / "runtime-data")
        manager.settings = WebSettingsStore(tmp_path / "settings.json")
        await manager.settings.update(
            {
                "base_url": "https://provider.example/v1",
                "model": "test-model",
                "api_key": "test-key",
            }
        )
        client = await DurableRuntimeClient.create(project, data_root=tmp_path / "runtime-data")
        manager.selected_root = project.resolve()
        manager._clients[str(project.resolve())] = client
        executor_calls = 0

        class RejectionAwareModel:
            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, _messages, _tools):
                self.calls += 1
                if self.calls == 1:
                    return ModelSegment(
                        text="需要执行一个动作",
                        tool_calls=(
                            {
                                "id": "approval-action",
                                "name": "run_command",
                                "arguments": {"command": "echo rejected"},
                            },
                        ),
                    )
                return ModelSegment(
                    text="已按你的选择继续处理。",
                    completion_candidate={
                        "acceptance_criteria": ["done"],
                        "evidence": [
                            {
                                "evidence_id": "webui-rejection-test",
                                "kind": "test",
                                "digest": "sha256:" + "a" * 64,
                                "source": "webui rejection test",
                                "recorded_at": 0.0,
                                "confidence": 1.0,
                            }
                        ],
                    },
                )

        model = RejectionAwareModel()

        async def must_not_execute(_request):
            nonlocal executor_calls
            executor_calls += 1
            raise AssertionError("a rejected action must not execute")

        def factory(_run_id: str):
            return RunWorker(
                client.store,
                ReActKernel(model),
                worker_id="webui-rejection-worker",
                action_executor=must_not_execute,
                # This test isolates the rejection/continuation contract;
                # production workers still use the durable-evidence gate.
                completion_verifier=lambda _candidate: True,
            )

        supervisor = LocalSupervisor(
            client.store,
            factory,
            max_workers=1,
            # A long interval makes the explicit wake-up observable: after
            # rejection the worker must not wait for the next polling cycle.
            poll_interval=5.0,
        )
        client.supervisor = supervisor
        try:
            run_id = await client.submit("webui rejection task", ("done",))
            await supervisor.start()
            for _ in range(100):
                await asyncio.sleep(0.01)
                view = await client.coordinator.inspect(run_id)
                if view.status is RunStatus.AWAITING_APPROVAL:
                    break
            assert view.status is RunStatus.AWAITING_APPROVAL
            approval = view.projection.approvals[0]

            result = await manager.reject(run_id, approval.approval_id, "WebUI 用户拒绝")
            assert result["decision"] == "rejected"
            assert result["status"] in {"queued", "running", "completed"}

            # A stale card or a double-click must be harmless.  The second
            # request observes the terminal ApprovalRejected projection and
            # returns it without appending a duplicate decision event.
            duplicate = await manager.reject(run_id, approval.approval_id, "WebUI 用户拒绝")
            assert duplicate["decision"] == "rejected"
            assert duplicate["idempotent"] is True

            for _ in range(200):
                await asyncio.sleep(0.01)
                view = await client.coordinator.inspect(run_id)
                if view.status in {
                    RunStatus.COMPLETED,
                    RunStatus.STALLED,
                    RunStatus.BLOCKED,
                    RunStatus.FAILED_TERMINAL,
                }:
                    break
            assert view.status is RunStatus.COMPLETED
            assert executor_calls == 0
            assert model.calls == 2
            events = (await client.store.read(run_id, limit=200)).events
            event_types = [event.event_type for event in events]
            assert "ApprovalRejected" in event_types
            assert "ToolObservationCommitted" in event_types
            assert "ActionCancelled" in event_types
            assert "ActionStarted" not in event_types
            rejected_observation = next(
                event for event in events if event.event_type == "ToolObservationCommitted"
            )
            public_observation = _public_event(client, rejected_observation, root_run_id=run_id)
            assert public_observation is not None
            assert public_observation["status"] == "rejected"
            assert public_observation["rejected"] is True
            assert "用户拒绝了本次工具调用" in public_observation["content"]
            assert event_types.count("ApprovalRejected") == 1
        finally:
            await supervisor.stop(drain=False)
            await client.close()

    asyncio.run(exercise())


def test_webui_does_not_expose_approval_after_run_is_cancelled(tmp_path: Path) -> None:
    """A terminal Run keeps approval evidence but no longer exposes a card."""

    async def exercise() -> None:
        project = tmp_path / "project"
        project.mkdir()
        manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / "runtime-data")
        client = await DurableRuntimeClient.create(project, data_root=tmp_path / "runtime-data")
        manager.selected_root = project.resolve()
        manager._clients[str(project.resolve())] = client
        try:
            run_id = await client.submit("approval becomes stale after stop")
            lease = await LeaseManager(client.store).claim(run_id, "test-worker")
            await client.coordinator._append(
                run_id,
                "ActionPlanned",
                {
                    "action_id": "approval-action",
                    "tool_name": "run_command",
                    "attempt": 1,
                    "normalized_args_digest": "sha256:" + "a" * 64,
                    "arguments_artifact": "sha256:" + "b" * 64,
                    "contract_digest": "sha256:" + "c" * 64,
                    "effect_class": "unknown",
                    "worker_id": "test-worker",
                },
                EventActor("worker", "test-worker"),
            )
            await client.coordinator._append(
                run_id,
                "ApprovalRequested",
                {
                    "approval_id": "approval-approval-action",
                    "action_id": "approval-action",
                    "action_args_digest": "sha256:" + "a" * 64,
                    "scope": ["run_command"],
                },
                EventActor("worker", "test-worker"),
            )
            assert (await client.coordinator.inspect(run_id)).status is RunStatus.AWAITING_APPROVAL
            await client.coordinator.cancel(run_id, "test stop")
            view = await client.coordinator.inspect(run_id)
            assert view.status is RunStatus.CANCELLED
            assert view.projection.approvals[0].status.value == "expired"

            _root, _effective, _head, approvals, _tree = await manager._conversation_snapshot(client, run_id)
            assert approvals == ()
            session = await manager.session(run_id)
            assert session["status"] == RunStatus.CANCELLED.value
            assert session["projection"]["approvals"] == []
            # The browser may still have rendered the card when Stop wins the
            # race.  Treat a follow-up click as an idempotent no-op and let the
            # UI refresh the terminal state instead of surfacing a false
            # "approval does not exist" error.
            stale_click = await manager.reject(run_id, "approval-approval-action", "WebUI 用户拒绝")
            assert stale_click["decision"] == "expired"
            assert stale_click["idempotent"] is True
            await LeaseManager(client.store).release(lease)
        finally:
            await client.close()

    asyncio.run(exercise())
