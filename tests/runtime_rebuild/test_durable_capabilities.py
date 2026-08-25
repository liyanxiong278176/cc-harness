from __future__ import annotations

import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from cc_harness.coordinator import RunCoordinator, RunRequest
from cc_harness.config import ContextConfig
from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness.memory.config import MemoryConfig
from cc_harness.memory.offload.runtime import build_context_offload
from cc_harness.mcp_client import ToolResult
from cc_harness.run_kernel import ActionRequest, ModelSegment, ReActKernel
from cc_harness.run_model import ActionStatus, EvidenceKind, EvidenceRef
from cc_harness.run_store import RunStore
from cc_harness.tokens import TokenCounter
from cc_harness.worker import RunWorker


EVIDENCE = EvidenceRef(
    "capability-evidence",
    EvidenceKind.TEST,
    "sha256:capability-evidence",
    "pytest",
    1.0,
)


class ContinuationModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools):
        del messages, tools
        self.calls += 1
        if self.calls == 1:
            return ModelSegment(
                text="page one",
                tool_calls=(
                    {
                        "id": "read-page-1",
                        "name": "Read",
                        "arguments": {"path": "large.txt", "limit": 1},
                    },
                ),
            )
        if self.calls == 2:
            return ModelSegment(
                text="continue",
                tool_calls=(
                    {
                        "id": "read-page-2",
                        "name": "ContinueToolResult",
                        "arguments": {
                            "observation_id": str(
                                uuid5(
                                    NAMESPACE_URL,
                                    "cc-harness:observation:read-page-1:1",
                                )
                            ),
                            "next_cursor": "2",
                        },
                    },
                ),
            )
        return ModelSegment(
            text="done",
            completion_candidate={
                "acceptance_criteria": ["done"],
                "evidence": [EVIDENCE.to_dict()],
            },
        )


@pytest.mark.asyncio
async def test_durable_runtime_continues_native_read_result(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "large.txt").write_text("one\ntwo\n", encoding="utf-8")
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        coordinator = RunCoordinator(store)
        handle = await coordinator.submit(RunRequest("read", ("done",)))
        client = DurableRuntimeClient(store, coordinator)
        specs, contracts, handlers, deps = client._build_tool_runtime()
        model = ContinuationModel()
        worker = RunWorker(
            store,
            ReActKernel(model),
            worker_id="capability-worker",
            action_executor=lambda request: client._execute_tool(
                request,
                run_id=handle.run_id,
                handlers=handlers,
                handler_deps=deps,
                contracts=contracts,
                l5=None,
            ),
            contracts=contracts,
            available_tools=specs,
        )
        await worker.execute(await worker.claim(handle.run_id))

        assert model.calls == 3
        events = (await store.read(handle.run_id)).events
        observations = [
            event
            for event in events
            if event.event_type == "ToolObservationCommitted"
        ]
        assert len(observations) == 2
        second = json.loads(store.artifacts.read_text(observations[1].payload["observation_artifact"]))
        assert second["metadata"]["continued_from_observation"] == observations[0].payload[
            "observation_id"
        ]
        assert json.loads(
            store.artifacts.read_text(observations[0].payload["observation_artifact"])
        )["complete"] is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_run_command_observation_contains_workspace_change_set(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        coordinator = RunCoordinator(store)
        handle = await coordinator.submit(RunRequest("command", ("done",)))
        client = DurableRuntimeClient(store, coordinator)

        async def command(args, *, cwd):
            Path(cwd, "created.txt").write_text(args["command"], encoding="utf-8")
            return ToolResult.success("stdout", metadata={"exit_code": 0})

        specs, contracts, handlers, deps = client._build_tool_runtime()
        handlers["run_command"] = command
        result = await client._execute_tool(
            ActionRequest("command-1", "run_command", {"command": "created"}),
            run_id=handle.run_id,
            handlers=handlers,
            handler_deps=deps,
            contracts=contracts,
            l5=None,
        )

        assert result.status is ActionStatus.SUCCEEDED
        metadata = result.observation.metadata
        change_set = metadata["workspace_change_set"]
        assert "created.txt" in change_set["changed_paths"]
        assert metadata["workspace_baseline"]["file_count"] == 0
        assert change_set["digest"].startswith("sha256:")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_existing_offload_service_reduces_model_visible_result_to_pointer(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        coordinator = RunCoordinator(store)
        handle = await coordinator.submit(RunRequest("offload", ("done",)))
        client = DurableRuntimeClient(store, coordinator)
        _specs, contracts, handlers, handler_deps = client._build_tool_runtime()

        async def large_read(args, *, cwd):
            del args, cwd
            return ToolResult.success("large-line " * 4_000)

        handlers["Read"] = large_read
        _extras, context_deps = build_context_offload(
            project / ".cc-harness",
            session_id=handle.run_id,
            llm=None,
            memory_config=MemoryConfig(offload_enabled=True, offload_threshold=1),
            context_config=ContextConfig(context_window=10_000),
        )
        context_deps["token_counter"] = TokenCounter()
        result = await client._execute_tool(
            ActionRequest("large-read", "Read", {"path": "large.txt"}),
            run_id=handle.run_id,
            context_deps=context_deps,
            handlers=handlers,
            handler_deps=handler_deps,
            contracts=contracts,
            l5=None,
        )

        assert result.observation.metadata.get("offload"), result.observation.metadata
        assert result.observation.metadata["offload"]["node_id"]
        assert result.observation.text.startswith("[offloaded node=")
        assert "large-line" in client.store.artifacts.read_text(result.result_artifact)
    finally:
        await store.close()
