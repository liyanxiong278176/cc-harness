from __future__ import annotations

import pytest
from unittest.mock import AsyncMock
from unittest.mock import patch
import uuid

from cc_harness.coordinator import RunCoordinator, RunRequest
from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness.durable_subagents import ChildTaskSpec, durable_dispatch_subagent_handler
from cc_harness.run_events import EventActor
from cc_harness.run_model import EffectClass
from cc_harness.run_store import RunStore


@pytest.mark.asyncio
async def test_dynamic_children_are_persisted_once_and_read_only_children_share_root(tmp_path):
    store = await RunStore(tmp_path, data_root=tmp_path / "state").open()
    coordinator = RunCoordinator(store)
    try:
        parent = await coordinator.submit(
            RunRequest("coordinate a project", ("return a report",))
        )
        specs = (
            ChildTaskSpec(
                node_id="inspect-a",
                objective="inspect module A",
                acceptance_criteria=("report module A",),
            ),
            ChildTaskSpec(
                node_id="inspect-b",
                objective="inspect module B",
                acceptance_criteria=("report module B",),
                depends_on=("inspect-a",),
            ),
        )
        receipts = await coordinator.create_children(parent.run_id, specs)
        assert len(receipts) == 2
        view = await coordinator.inspect(parent.run_id)
        nodes = {node.node_id: node for node in view.projection.plan.nodes}
        assert nodes["inspect-a"].effect_class == EffectClass.READ_ONLY.value
        assert nodes["inspect-a"].worktree_id is None
        assert nodes["inspect-b"].depends_on == ("inspect-a",)
        assert all(child.required for child in view.projection.children)

        # Replaying the same model tool call is an idempotent receipt, not a
        # second child Run or a second worktree.
        replay = await coordinator.create_children(parent.run_id, specs)
        assert replay == receipts
        assert len((await coordinator.inspect(parent.run_id)).projection.children) == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_dispatch_handler_uses_parent_context_and_rejects_unbounded_mutation(tmp_path):
    store = await RunStore(tmp_path, data_root=tmp_path / "state").open()
    coordinator = RunCoordinator(store)
    try:
        parent = await coordinator.submit(
            RunRequest("coordinate a project", ("return a report",))
        )
        result = await durable_dispatch_subagent_handler(
            {
                "sub_specs": [
                    {
                        "node_id": "dynamic-check",
                        "objective": "check the current failure evidence",
                        "acceptance_criteria": ["produce a cited finding"],
                        "required": True,
                    }
                ]
            },
            coordinator=coordinator,
            run_id=parent.run_id,
        )
        assert not result.is_error
        assert "dynamic-check" in result.llm_text

        rejected = await durable_dispatch_subagent_handler(
            {
                "sub_specs": [
                    {
                        "node_id": "unsafe-change",
                        "objective": "change the workspace",
                        "acceptance_criteria": ["tests pass"],
                        "effect_class": "workspace_mutation",
                        "owned_paths": ["src"],
                    }
                ]
            },
            coordinator=coordinator,
            run_id=parent.run_id,
        )
        assert rejected.is_error
        assert "WorktreeManager" in rejected.llm_text
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_child_scope_is_bounded_and_sibling_batches_keep_parent_depth(tmp_path):
    store = await RunStore(tmp_path, data_root=tmp_path / "state").open()
    coordinator = RunCoordinator(store)
    try:
        parent = await coordinator.submit(
            RunRequest(
                "coordinate a scoped project",
                ("return a report",),
                allowed_scope=("src",),
                excluded_scope=("src/generated",),
            )
        )
        with pytest.raises(ValueError, match="exceeds parent scope"):
            await coordinator.create_children(
                parent.run_id,
                (ChildTaskSpec(
                    "outside",
                    "inspect docs",
                    ("report",),
                    allowed_scope=("docs",),
                ),),
            )

        first = (await coordinator.create_children(
            parent.run_id,
            (ChildTaskSpec("first", "inspect source", ("report",)),),
        ))[0]
        second = (await coordinator.create_children(
            parent.run_id,
            (ChildTaskSpec("second", "inspect another source", ("report",)),),
        ))[0]
        view = await coordinator.inspect(parent.run_id)
        nodes = {node.node_id: node for node in view.projection.plan.nodes}
        assert nodes["first"].depth == nodes["second"].depth == 1
        assert first.child_run_id != second.child_run_id
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_unknown_child_effect_is_rejected_before_dispatch(tmp_path):
    store = await RunStore(tmp_path, data_root=tmp_path / "state").open()
    coordinator = RunCoordinator(store)
    try:
        parent = await coordinator.submit(RunRequest("coordinate", ("report",)))
        result = await durable_dispatch_subagent_handler(
            {
                "sub_specs": [{
                    "node_id": "unknown-effect",
                    "objective": "probe",
                    "acceptance_criteria": ["report"],
                    "effect_class": "unknown",
                }]
            },
            coordinator=coordinator,
            run_id=parent.run_id,
        )
        assert result.is_error
        assert "effect_class" in result.llm_text
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_child_peer_run_refs_are_rejected(tmp_path):
    store = await RunStore(tmp_path, data_root=tmp_path / "state").open()
    coordinator = RunCoordinator(store)
    try:
        parent = await coordinator.submit(RunRequest("coordinate", ("report",)))
        sibling_like = str(uuid.uuid4())
        result = await durable_dispatch_subagent_handler(
            {
                "sub_specs": [{
                    "node_id": "no-peer-channel",
                    "objective": "inspect the parent-owned evidence",
                    "acceptance_criteria": ["report"],
                    "context_refs": [sibling_like],
                }]
            },
            coordinator=coordinator,
            run_id=parent.run_id,
        )
        assert result.is_error
        assert "peer communication is disabled" in result.llm_text
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_failed_child_is_durable_and_blocks_required_parent_completion(tmp_path):
    store = await RunStore(tmp_path, data_root=tmp_path / "state").open()
    coordinator = RunCoordinator(store)
    try:
        parent = await coordinator.submit(
            RunRequest("coordinate a project", ("return a report",))
        )
        receipt = (await coordinator.create_children(
            parent.run_id,
            (ChildTaskSpec("required-check", "check", ("checked",)),),
        ))[0]
        await coordinator._append(
            parent.run_id,
            "ChildRunFailed",
            {"child_run_id": receipt.child_run_id, "reason": "provider timeout"},
            EventActor("coordinator", "test"),
        )
        view = await coordinator.inspect(parent.run_id)
        assert view.projection.children[0].status == "failed"
        assert view.projection.children[0].failure_reason == "provider timeout"
        assert view.projection.children[0].required is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_natural_continuation_resumes_root_and_recoverable_descendants(tmp_path):
    store = await RunStore(tmp_path, data_root=tmp_path / "state").open()
    coordinator = RunCoordinator(store)
    client = DurableRuntimeClient(store, coordinator)
    client.start_supervisor = AsyncMock()
    try:
        parent = await coordinator.submit(RunRequest("resume the project", ("report",)))
        child = (await coordinator.create_children(
            parent.run_id,
            (ChildTaskSpec("resume-check", "check the checkpoint", ("report",)),),
        ))[0]
        await coordinator.cancel(parent.run_id, "Ctrl+C")
        await coordinator.cancel(child.child_run_id, "Ctrl+C")

        await client.continue_run(parent.run_id)

        assert (await coordinator.inspect(parent.run_id)).status.value == "queued"
        assert (await coordinator.inspect(child.child_run_id)).status.value == "queued"
        client.start_supervisor.assert_awaited_once()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_interactive_control_starts_one_detached_supervisor_marker(tmp_path):
    store = await RunStore(tmp_path, data_root=tmp_path / "state").open()
    client = DurableRuntimeClient(store, RunCoordinator(store))

    class _Process:
        pid = 4242

    try:
        with patch("cc_harness.durable_runtime.subprocess.Popen", return_value=_Process()) as popen:
            pid = client.start_detached_supervisor(reasoning_effort="high")
        assert pid == 4242
        assert (store.state_dir / "supervisor.pid").read_text(encoding="utf-8") == "4242"
        command = popen.call_args.args[0]
        assert command[command.index("--command") + 1] == "supervisor"
        assert "--data-root" in command
    finally:
        await client.close()
