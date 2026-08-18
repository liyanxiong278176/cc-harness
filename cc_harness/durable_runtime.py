"""Client and local execution service for the rebuilt durable runtime."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .action_contracts import ToolContractRegistry
from .config import load_executor_config, load_l5_config, load_layered_config
from .coordinator import RunCoordinator, RunRequest
from .llm import LLMClient
from .mcp_client import MCPClient, ToolResult
from .native_tools import NATIVE_FILE_TOOLS
from .run_kernel import ActionRequest, ModelAdapter, ModelSegment, ReActKernel
from .run_model import ActionStatus, EffectClass
from .run_store import RunStore
from .supervisor import LocalSupervisor
from .tools import RUN_COMMAND_SPEC, init_session_executor, run_command, shutdown_session_executor
from .l5 import build_l5_engine, sanitize
from .worker import ActionExecutionResult, RunWorker


_COMPLETION_BLOCK = re.compile(
    r"<cc-harness-complete>\s*(\{.*?\})\s*</cc-harness-complete>",
    re.DOTALL,
)


class DurableModelAdapter(ModelAdapter):
    """Translate the existing streaming LLM seam into one model segment."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> ModelSegment:
        content: list[str] = []
        pending = []
        finish_reason = "model_stop"
        stream = self.llm.chat([dict(item) for item in messages], [dict(item) for item in tools])
        try:
            async for event in stream:
                if event.kind == "content":
                    content.append(event.text)
                elif event.kind == "done":
                    pending = event.pending
                    finish_reason = event.finish_reason or finish_reason
                    if event.content:
                        content = [event.content]
        finally:
            close_stream = getattr(stream, "aclose", None)
            if close_stream is not None:
                await close_stream()

        calls: list[dict[str, Any]] = []
        for index, item in enumerate(pending):
            if not item.name:
                continue
            try:
                arguments = json.loads(item.arguments_json or "{}")
            except json.JSONDecodeError:
                arguments = item.arguments_json or "{}"
            calls.append(
                {
                    "id": item.id or f"durable-call-{index}",
                    "name": item.name,
                    "arguments": arguments,
                }
            )

        text = "".join(content)
        candidate = None
        match = _COMPLETION_BLOCK.search(text)
        if match is not None:
            try:
                raw = json.loads(match.group(1))
            except json.JSONDecodeError:
                raw = None
            if isinstance(raw, dict):
                candidate = raw
            text = _COMPLETION_BLOCK.sub("", text).strip()
        return ModelSegment(
            text=text,
            tool_calls=tuple(calls),
            completion_candidate=candidate,
            stop_reason=finish_reason,
        )


class DurableRuntimeClient:
    """Own the durable store/coordinator and optionally a local supervisor.

    Creating a client is intentionally configuration-light: status/list and
    migration commands must work even when model credentials are unavailable.
    The execution service is opt-in through ``start_supervisor`` or the CLI
    ``--command supervisor``.
    """

    def __init__(self, store: RunStore, coordinator: RunCoordinator) -> None:
        self.store = store
        self.coordinator = coordinator
        self.cwd = store.project_root
        self.supervisor: LocalSupervisor | None = None
        self._llm: LLMClient | None = None
        self._mcp: MCPClient | None = None
        self._execution_started = False

    @classmethod
    async def create(
        cls,
        cwd: Path,
        *,
        data_root: Path | None = None,
    ) -> "DurableRuntimeClient":
        store = RunStore(Path(cwd), data_root=data_root)
        await store.open()
        return cls(store, RunCoordinator(store))

    async def submit(
        self,
        objective: str,
        acceptance_criteria: tuple[str, ...] = ("request addressed",),
    ) -> str:
        return (
            await self.coordinator.submit(RunRequest(objective, acceptance_criteria))
        ).run_id

    async def start_supervisor(
        self,
        *,
        worker_id: str | None = None,
        max_workers: int = 3,
        reasoning_effort: str | None = None,
    ) -> LocalSupervisor:
        if self.supervisor is not None:
            return self.supervisor
        config = load_layered_config(self.cwd)
        self._llm = LLMClient(
            api_key=config.openai_api_key,
            model=config.openai_model,
            base_url=config.openai_base_url,
            reasoning_effort=reasoning_effort,
        )
        self._mcp = MCPClient(config.mcp_servers)
        await self._mcp.start()
        init_session_executor(load_executor_config(self.cwd / "policy.yaml"), str(self.cwd))
        l5 = build_l5_engine(load_l5_config(self.cwd / "policy.yaml"))
        tool_specs, contracts, handlers = self._build_tool_runtime()
        model = DurableModelAdapter(self._llm)
        kernel = ReActKernel(model)
        base_worker_id = worker_id or f"supervisor-{os.getpid()}"
        counter = 0

        def worker_factory(_run_id: str) -> RunWorker:
            nonlocal counter
            counter += 1
            return RunWorker(
                self.store,
                kernel,
                worker_id=f"{base_worker_id}-{counter}",
                action_executor=lambda request: self._execute_tool(
                    request,
                    handlers=handlers,
                    contracts=contracts,
                    l5=l5,
                ),
                contracts=contracts,
                available_tools=tool_specs,
                continue_segments=True,
            )

        self.supervisor = LocalSupervisor(
            self.store,
            worker_factory,
            max_workers=max_workers,
        )
        await self.supervisor.start()
        self._execution_started = True
        return self.supervisor

    async def run_supervisor_forever(self, **kwargs: Any) -> None:
        await self.start_supervisor(**kwargs)
        while True:
            await asyncio.sleep(3600)

    def _build_tool_runtime(self):
        contracts = ToolContractRegistry.first_party()
        handlers: dict[str, Any] = {
            name: entry["handler"] for name, entry in NATIVE_FILE_TOOLS.items()
        }
        handlers["run_command"] = run_command
        specs: list[dict[str, Any]] = []
        for name, entry in (("run_command", {"spec": RUN_COMMAND_SPEC}), *NATIVE_FILE_TOOLS.items()):
            spec = json.loads(json.dumps(entry["spec"], ensure_ascii=False))
            function = spec.setdefault("function", {})
            contract = contracts.get(name)
            function["x-cc-harness-capability"] = {
                "effect": (
                    contract.effect_class.value
                    if isinstance(contract.effect_class, EffectClass)
                    else contract.effect_class
                ),
                "requires_user_intent": contract.requires_approval,
                "source": "first_party_native_contract",
            }
            specs.append(spec)
        if self._mcp is not None:
            for spec in self._mcp._tools:
                copied = json.loads(json.dumps(spec, ensure_ascii=False))
                function = copied.get("function") or {}
                name = str(function.get("name") or "")
                metadata = dict(function.get("x-cc-harness-capability") or {})
                contracts.from_mcp_metadata(name, metadata)
                specs.append(copied)
        return tuple(specs), contracts, handlers

    async def _execute_tool(
        self,
        request: ActionRequest,
        *,
        handlers: Mapping[str, Any],
        contracts: ToolContractRegistry,
        l5,
    ) -> ActionExecutionResult:
        try:
            if request.tool_name in handlers:
                result = await handlers[request.tool_name](request.arguments, cwd=str(self.cwd))
            elif self._mcp is not None:
                result = await self._mcp.call_tool(request.tool_name, dict(request.arguments))
            else:
                result = ToolResult.error(
                    display="MCP runtime is not connected",
                    llm="[Tool Error] MCP runtime is not connected",
                )
        except BaseException as exc:
            return ActionExecutionResult(ActionStatus.OUTCOME_UNKNOWN, error_kind=type(exc).__name__)
        if not isinstance(result, ToolResult):
            result = ToolResult.success(str(result))
        text = sanitize(result.llm_text, l5)
        artifact_payload = {
            "tool_name": request.tool_name,
            "is_error": bool(result.is_error),
            "text": text[:256_000],
            "source": result.source,
            "capability": result.capability,
        }
        artifact = self.store.artifacts.put_text(
            json.dumps(artifact_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            media_type="application/json; purpose=tool-result",
        )
        contract = contracts.get(request.tool_name)
        if result.is_error:
            status = (
                ActionStatus.OUTCOME_UNKNOWN
                if contract.effect_class is EffectClass.EXTERNAL_SIDE_EFFECT
                else ActionStatus.FAILED
            )
            return ActionExecutionResult(status, artifact.digest, "tool_error")
        path = request.arguments.get("path")
        paths = (str(path),) if isinstance(path, str) and path else ()
        modified = paths if request.tool_name in {"Edit", "Write"} else ()
        read = paths if request.tool_name in {"Read", "Glob", "Grep"} else ()
        return ActionExecutionResult(
            ActionStatus.SUCCEEDED,
            result_artifact=artifact.digest,
            modified_paths=modified,
            read_paths=read,
        )

    async def close(self) -> None:
        if self.supervisor is not None:
            await self.supervisor.stop(drain=False)
            self.supervisor = None
        if self._mcp is not None:
            await self._mcp.shutdown()
            self._mcp = None
        if self._llm is not None:
            await self._llm.aclose()
            self._llm = None
        if self._execution_started:
            await shutdown_session_executor()
            self._execution_started = False
        await self.store.close()


__all__ = ["DurableModelAdapter", "DurableRuntimeClient"]
