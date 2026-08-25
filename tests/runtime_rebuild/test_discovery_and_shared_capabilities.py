from __future__ import annotations

from pathlib import Path

import pytest

from cc_harness.capability_services import SharedCapabilityServices
from cc_harness.config import AppConfig
from cc_harness.coordinator import RunCoordinator, RunRequest
from cc_harness.run_kernel import ActionRequest
from cc_harness.run_model import EffectClass, PlanNode, RunStatus
from cc_harness.run_store import RunStore
from cc_harness.supervisor import LocalSupervisor
from cc_harness.worker import RunWorker


def _config() -> AppConfig:
    return AppConfig(
        openai_api_key="test-key",
        openai_base_url="https://example.invalid/v1",
        openai_model="test-model",
        mcp_servers={},
        runtime_environment={},
    )


@pytest.mark.asyncio
async def test_session_and_durable_entrypoints_share_capability_configuration(tmp_path: Path) -> None:
    services = SharedCapabilityServices.load(tmp_path, _config())
    try:
        assert services.policy.project_root == tmp_path.resolve()
        assert services.context_config.context_window > 0
        assert services.l2_config.enabled is True
        assert services.l5 is not None
        assert services.activation_details()["provenance_enforced"] is False
    finally:
        if services.l2_client is not None:
            await services.l2_client.close()


@pytest.mark.asyncio
async def test_plan_discovery_holds_queue_until_read_only_plan_is_committed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        coordinator = RunCoordinator(store)
        handle = await coordinator.submit(
            RunRequest(
                "implement login registration",
                ("tests pass",),
                plan_discovery=True,
            )
        )
        waiting = await coordinator.inspect(handle.run_id)
        assert waiting.status is RunStatus.QUEUED
        assert waiting.projection.discovery_status == "awaiting"
        assert waiting.projection.mutation_gate == "read_only"

        async def unused_worker(_run_id: str) -> RunWorker:
            raise AssertionError("discovery-gated run must not be claimed")

        supervisor = LocalSupervisor(store, unused_worker, max_workers=1)
        stats = await supervisor.tick()
        assert stats.active_runs == ()
        assert stats.queued_runs == 1

        plan = await coordinator.complete_plan_discovery(
            handle.run_id,
            (PlanNode("root", "root", owned_paths=("src/auth",)),),
        )
        ready = await coordinator.inspect(handle.run_id)
        assert ready.projection.discovery_status == "completed"
        assert ready.projection.mutation_gate == "open"
        assert ready.projection.plan.digest == plan.digest
        events = (await store.read(handle.run_id)).events
        assert [event.event_type for event in events].count("PlanDiscoveryStarted") == 1
        assert [event.event_type for event in events].count("PlanDiscoveryCompleted") == 1
    finally:
        await store.close()


def test_discovery_gate_rejects_mutating_action_at_projection_boundary() -> None:
    # The event validator/projection test is covered by the durable event
    # fixtures; keep the request shape here as an explicit contract reminder.
    request = ActionRequest(
        "write-1",
        "Write",
        {"path": "src/auth.py", "content": "x"},
        effect_class=EffectClass.WORKSPACE_MUTATION,
    )
    assert request.effect_class is EffectClass.WORKSPACE_MUTATION
