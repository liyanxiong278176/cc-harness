from __future__ import annotations

from pathlib import Path

import pytest

from cc_harness.action_contracts import ToolContractRegistry
from cc_harness.coordinator import RunCoordinator
from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness.mcp_client import ToolResult
from cc_harness.policy import PolicyEngine
from cc_harness.run_kernel import ActionRequest
from cc_harness.run_model import ActionStatus
from cc_harness.run_store import RunStore


@pytest.mark.asyncio
async def test_durable_policy_hard_deny_prevents_sensitive_native_dispatch(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    client = DurableRuntimeClient(store, RunCoordinator(store))
    called = False

    async def read_handler(_args, *, cwd: str):
        nonlocal called
        called = True
        return ToolResult.success(f"read from {cwd}")

    try:
        handle = await client.submit("inspect credentials", ("report",))
        client._policy = PolicyEngine(project)
        client._security_capability_metadata["Read"] = {
            "effect": "read",
            "requires_user_intent": False,
            "source": "test",
        }
        result = await client._execute_tool(
            ActionRequest("read-secret", "Read", {"path": ".env"}),
            run_id=handle,
            handlers={"Read": read_handler},
            handler_deps={},
            contracts=ToolContractRegistry.first_party(),
            l5=None,
        )
        assert result.status is ActionStatus.FAILED
        assert called is False
        assert result.observation is not None
        assert result.observation.metadata["security_decision"] == "sensitive_credential_path"
    finally:
        await client.close()
