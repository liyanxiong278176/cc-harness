"""Client and local execution service for the rebuilt durable runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .action_contracts import ToolContractRegistry, ToolRecoveryContract
from .activation import ActivationManifest, CapabilityProfile
from .capability_runtime import AgentCapabilityRuntime
from .capability_services import SharedCapabilityServices
from .config import load_layered_config
from .coordinator import RunCoordinator, RunRequest
from .credential_broker import ActionScopedCapabilityBroker, CredentialBrokerError
from .interaction_history import materialize_interaction_messages
from .llm import LLMClient
from .mcp_client import MCPClient, ToolResult
from .native_tools import NATIVE_FILE_TOOLS
from .policy import Action
from .run_kernel import ActionRequest, ModelAdapter, ModelSegment, ReActKernel
from .run_model import ActionStatus, CompletionCandidate, EffectClass
from .run_store import RunNotFound, RunStore, RunStoreError
from .supervisor import LocalSupervisor
from .tools import RUN_COMMAND_SPEC, init_session_executor, run_command, shutdown_session_executor
from .l5 import sanitize
from .tool_observation import CONTINUE_TOOL_RESULT_SPEC, ToolObservation, make_observation
from .worker import ActionExecutionResult, RunWorker


_COMPLETION_BLOCK = re.compile(
    r"<cc-harness-complete>\s*(\{.*?\})\s*</cc-harness-complete>",
    re.DOTALL,
)

_WORKSPACE_IGNORED_DIRS = {
    ".git",
    ".cc-harness",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
}

_SENSITIVE_METADATA_KEY = re.compile(
    r"(?:api.?key|access.?token|authorization|cookie|credential|password|passwd|private.?key|secret|token)",
    re.IGNORECASE,
)


def _sanitize_value(value: Any, l5: Any | None, *, key: str = "") -> Any:
    """Sanitize nested structured tool data before it becomes an artifact."""

    if key and _SENSITIVE_METADATA_KEY.search(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(item_key): _sanitize_value(item_value, l5, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item, l5, key=key) for item in value]
    if isinstance(value, str):
        return sanitize(value, l5)
    return value

RECALL_RUN_CONTEXT_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "RecallRunContext",
        "description": (
            "Recall authorized committed facts from this run or its explicit "
            "predecessor/parent handoff. Returned data is advisory and untrusted."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source_run_id": {"type": "string"},
                "event_types": {"type": "array", "items": {"type": "string"}},
                "after_sequence": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": [],
        },
    },
}


def _workspace_snapshot(root: Path) -> dict[str, str]:
    """Return a bounded-to-scope content digest map for command evidence."""

    snapshot: dict[str, str] = {}
    root = root.resolve()
    if not root.is_dir():
        return snapshot
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(
            name
            for name in directories
            if name not in _WORKSPACE_IGNORED_DIRS
            and not (Path(current) / name).is_symlink()
        )
        for name in sorted(files):
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            snapshot[relative] = f"sha256:{digest}"
    return snapshot


def _workspace_change_set(
    before: Mapping[str, str], after: Mapping[str, str]
) -> dict[str, Any]:
    created = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(path for path in set(before) & set(after) if before[path] != after[path])
    changes = {
        "created": [{"path": path, "after": after[path]} for path in created],
        "modified": [
            {"path": path, "before": before[path], "after": after[path]}
            for path in modified
        ],
        "deleted": [{"path": path, "before": before[path]} for path in deleted],
    }
    encoded = json.dumps(changes, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {
        **changes,
        "changed_paths": sorted((*created, *modified, *deleted)),
        "digest": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
    }


class DurableModelAdapter(ModelAdapter):
    """Translate the existing streaming LLM seam into one model segment."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    @staticmethod
    def _provider_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Drop durable bookkeeping keys before crossing the provider boundary."""

        allowed = {
            "role",
            "content",
            "name",
            "tool_call_id",
            "tool_calls",
            "reasoning_content",
            "refusal",
        }
        return [
            {key: value for key, value in dict(message).items() if key in allowed}
            for message in messages
        ]

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> ModelSegment:
        content: list[str] = []
        pending = []
        finish_reason = "model_stop"
        usage = None
        reasoning_content = ""
        stream = self.llm.chat(
            self._provider_messages(messages),
            [dict(item) for item in tools],
        )
        try:
            async for event in stream:
                if event.kind == "content":
                    content.append(event.text)
                elif event.kind == "done":
                    pending = event.pending
                    finish_reason = event.finish_reason or finish_reason
                    if event.content:
                        content = [event.content]
                    reasoning_content = event.reasoning_content
                    usage = event.usage
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
                # A model can emit a JSON-looking completion marker whose
                # nested evidence is not an EvidenceRef object (for example,
                # ``["verified"]``).  Do not let that provider protocol error
                # crash the worker while also never treating it as a valid
                # completion.  Keep malformed content visible to the next
                # turn so the model can repair its response and preserve the
                # audit trail in the assistant message.
                try:
                    CompletionCandidate.from_dict(raw)
                except (KeyError, TypeError, ValueError):
                    raw = None
                if raw is not None:
                    candidate = raw
                    text = _COMPLETION_BLOCK.sub("", text).strip()
        return ModelSegment(
            text=text,
            tool_calls=tuple(calls),
            completion_candidate=candidate,
            stop_reason=finish_reason,
            usage=(
                {
                    "input_tokens": usage.prompt_tokens,
                    "uncached_input_tokens": usage.uncached_prompt_tokens,
                    "cache_creation_input_tokens": usage.cache_creation_prompt_tokens,
                    "cache_read_input_tokens": usage.cache_read_prompt_tokens,
                    "output_tokens": usage.completion_tokens,
                    "model_calls": 1,
                }
                if usage is not None
                else {"model_calls": 1}
            ),
            reasoning_content=reasoning_content,
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
        self._capabilities: AgentCapabilityRuntime | None = None
        self._services: SharedCapabilityServices | None = None
        self._policy = None
        self._capability_broker = ActionScopedCapabilityBroker()
        self._security_capability_metadata: dict[str, dict[str, Any]] = {}
        self._activation_manifest: ActivationManifest | None = None
        self._workspace_command_lock = asyncio.Lock()
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
        # Terminal-Bench supplies a frozen official task statement inside an
        # isolated Harbor container.  Its instructions can describe external
        # operations (for example ``git push``) without being a live user's
        # request.  Keep the provenance explicit and auditable; never infer it
        # from the objective text.
        goal_provenance = (
            "official_benchmark"
            if os.getenv("CC_HARNESS_TRUSTED_BENCHMARK_TASK", "") == "1"
            and os.getenv("CC_HARNESS_TERMINAL_BENCH", "") == "1"
            else "user"
        )
        return (
            await self.coordinator.submit(
                RunRequest(
                    objective,
                    acceptance_criteria,
                    goal_provenance=goal_provenance,
                )
            )
        ).run_id

    async def start_supervisor(
        self,
        *,
        worker_id: str | None = None,
        max_workers: int = 3,
        reasoning_effort: str | None = None,
        capability_profile: str = "standard",
        host_execution: bool = False,
    ) -> LocalSupervisor:
        if self.supervisor is not None:
            return self.supervisor
        config = load_layered_config(self.cwd)
        activation_path = self.cwd / ".cc-harness" / "activation" / "durable-runtime.json"
        activation_path.parent.mkdir(parents=True, exist_ok=True)
        self._activation_manifest = ActivationManifest(
            activation_path,
            session_id="durable-runtime",
            project_root=self.cwd,
            profile=CapabilityProfile.named(capability_profile),
            requested_model=config.openai_model,
        )
        self._activation_manifest.initialize("runtime", entrypoint="DurableRuntimeClient")
        self._activation_manifest.add_artifact("runtime", activation_path)
        self._llm = LLMClient(
            api_key=config.openai_api_key,
            model=config.openai_model,
            base_url=config.openai_base_url,
            reasoning_effort=reasoning_effort,
        )
        self._activation_manifest.set_resolved_model(config.openai_model)
        self._mcp = MCPClient(config.mcp_servers)
        await self._mcp.start()
        self._activation_manifest.initialize(
            "mcp",
            configured_servers=len(config.mcp_servers),
            connected_servers=len(self._mcp._sessions),
            tool_count=len(self._mcp._tools),
        )
        self._services = SharedCapabilityServices.load(
            self.cwd,
            config,
            profile=CapabilityProfile.named(capability_profile),
            host_execution=host_execution,
        )
        self._policy = self._services.policy
        executor_config = self._services.executor_config
        init_session_executor(executor_config, str(self.cwd))
        self._activation_manifest.initialize(
            "safety",
            policy_path=str(self.cwd / "policy.yaml"),
            **self._services.activation_details(),
        )
        self._capabilities = await AgentCapabilityRuntime.create(
            self.store,
            self.cwd,
            llm=self._llm,
            config=config,
            shared_services=self._services,
        )
        self._activation_manifest.initialize(
            "context",
            context_window=self._capabilities.context_config.context_window,
            offload_enabled=self._capabilities.memory_config.offload_enabled,
        )
        if self._capabilities.memory_deps:
            self._activation_manifest.initialize(
                "memory",
                configured_enabled=bool(self._capabilities.memory_config.enabled),
                pipeline_enabled=bool(self._capabilities.memory_config.pipeline_enabled),
            )
        else:
            self._activation_manifest.initialize("memory", configured_enabled=False)
        self._activation_manifest.initialize(
            "background_services",
            enabled_services=sorted(self._capabilities.background_services),
        )
        self._activation_manifest.initialize(
            "agent_loop",
            control_plane="durable-segment",
            segment_boundary="plan-node-or-terminal",
        )
        self._activation_manifest.initialize(
            "tools",
            native_tools=True,
            mcp_tool_count=len(self._mcp._tools),
        )
        model = DurableModelAdapter(self._llm)
        kernel = ReActKernel(model)
        base_worker_id = worker_id or f"supervisor-{os.getpid()}"
        counter = 0

        def worker_factory(_run_id: str) -> RunWorker:
            nonlocal counter
            counter += 1
            run_extras = self._capabilities.tool_extras_for_run(_run_id) if self._capabilities else ()
            run_context_deps = (
                self._capabilities.context_deps_for_run(_run_id)
                if self._capabilities
                else {}
            )
            run_tool_specs, run_contracts, run_handlers, run_handler_deps = self._build_tool_runtime(
                self._capabilities,
                extras=run_extras,
            )
            return RunWorker(
                self.store,
                kernel,
                worker_id=f"{base_worker_id}-{counter}",
                action_executor=lambda request: self._execute_tool(
                    request,
                    run_id=_run_id,
                    context_deps=run_context_deps,
                    handlers=run_handlers,
                    handler_deps=run_handler_deps,
                    contracts=run_contracts,
                    l5=self._services.l5 if self._services is not None else None,
                ),
                contracts=run_contracts,
                available_tools=run_tool_specs,
                continue_segments=True,
                capability_runtime=self._capabilities,
                activation_manifest=self._activation_manifest,
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

    def issue_action_capability(
        self,
        *,
        run_id: str,
        action_id: str,
        scope: tuple[str, ...],
        secret: str,
        ttl_seconds: float = 300.0,
    ):
        """Issue a non-secret, action-scoped handle for an explicit tool sink."""

        return self._capability_broker.issue(
            run_id=run_id,
            action_id=action_id,
            scope=scope,
            secret=secret,
            ttl_seconds=ttl_seconds,
        )

    def _build_tool_runtime(
        self,
        capabilities: AgentCapabilityRuntime | None = None,
        *,
        extras: Sequence[Mapping[str, Any]] | None = None,
    ):
        contracts = ToolContractRegistry.first_party()
        handlers: dict[str, Any] = {
            name: entry["handler"] for name, entry in NATIVE_FILE_TOOLS.items()
        }
        handler_deps: dict[str, Mapping[str, Any]] = {}
        handlers["run_command"] = run_command
        specs: list[dict[str, Any]] = []
        for name, entry in (("run_command", {"spec": RUN_COMMAND_SPEC}), *NATIVE_FILE_TOOLS.items()):
            spec = json.loads(json.dumps(entry["spec"], ensure_ascii=False))
            function = spec.setdefault("function", {})
            contract = contracts.get(name)
            if contract.effect_class is not EffectClass.READ_ONLY:
                # Durable execution has no implicit interactive prompt.  Any
                # mutation/external/unknown action therefore pauses through the
                # event-sourced approval gate before dispatch.
                contract = replace(contract, requires_approval=True)
                contracts.register(contract)
            security_effect = {
                EffectClass.READ_ONLY.value: "read",
                EffectClass.WORKSPACE_MUTATION.value: "write",
                EffectClass.EXTERNAL_SIDE_EFFECT.value: "external_write",
                EffectClass.UNKNOWN.value: "unknown",
            }.get(
                contract.effect_class.value
                if isinstance(contract.effect_class, EffectClass)
                else str(contract.effect_class),
                "unknown",
            )
            self._security_capability_metadata[name] = {
                "effect": security_effect,
                "requires_user_intent": contract.requires_approval,
                "source": "first_party_native_contract",
            }
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
        for spec, contract in (
            (
                CONTINUE_TOOL_RESULT_SPEC,
                ToolRecoveryContract(
                    "ContinueToolResult",
                    EffectClass.READ_ONLY,
                    retryable=True,
                    max_retries=2,
                    idempotent=True,
                    parallelizable=False,
                    requires_approval=False,
                    child_allowed=True,
                    metadata={"source": "durable-observation-continuation"},
                ),
            ),
            (
                RECALL_RUN_CONTEXT_SPEC,
                ToolRecoveryContract(
                    "RecallRunContext",
                    EffectClass.READ_ONLY,
                    retryable=True,
                    max_retries=2,
                    idempotent=True,
                    parallelizable=False,
                    requires_approval=False,
                    child_allowed=True,
                    metadata={"source": "durable-authorized-recall"},
                ),
            ),
        ):
            contracts.register(contract)
            copied = json.loads(json.dumps(spec, ensure_ascii=False))
            copied.setdefault("function", {})["x-cc-harness-capability"] = {
                "effect": EffectClass.READ_ONLY.value,
                "requires_user_intent": False,
                "source": contract.metadata["source"],
            }
            specs.append(copied)
        if capabilities is not None:
            for extra in extras if extras is not None else capabilities.tool_extras:
                spec = json.loads(json.dumps(extra["spec"], ensure_ascii=False))
                function = spec.setdefault("function", {})
                name = str(function.get("name") or "")
                if not name:
                    continue
                handlers[name] = extra["handler"]
                handler_deps[name] = dict(extra.get("deps") or {})
                self._security_capability_metadata[name] = dict(
                    function.get("x-cc-harness-capability") or {
                        "effect": "read" if name in {"read_ref", "search_ref", "inspect_node"} else "unknown",
                        "source": "durable-extra-capability",
                    }
                )
                if name in {"read_ref", "search_ref", "inspect_node"}:
                    contracts.register(
                        ToolRecoveryContract(
                            name,
                            EffectClass.READ_ONLY,
                            retryable=True,
                            max_retries=2,
                            idempotent=True,
                            parallelizable=True,
                            requires_approval=False,
                            child_allowed=True,
                            metadata={"source": "durable-context-capability"},
                        )
                    )
                else:
                    contracts.register(contracts.get(name))
                specs.append(spec)
        if self._mcp is not None:
            for spec in self._mcp._tools:
                copied = json.loads(json.dumps(spec, ensure_ascii=False))
                function = copied.get("function") or {}
                name = str(function.get("name") or "")
                metadata = dict(function.get("x-cc-harness-capability") or {})
                contracts.from_mcp_metadata(name, metadata)
                self._security_capability_metadata[name] = metadata
                specs.append(copied)
        return tuple(specs), contracts, handlers, handler_deps

    @staticmethod
    def _structured_result(text: str) -> dict[str, Any] | None:
        """Decode native JSON results, including read_ref's JSON header."""

        candidates = [text]
        if "\n" in text:
            candidates.append(text.splitlines()[0])
        for candidate in candidates:
            try:
                value = json.loads(candidate)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                return value
        return None

    async def _read_all_events(self, run_id: str):
        events = []
        after = 0
        while True:
            page = await self.store.read(run_id, after=after, limit=1000)
            events.extend(page.events)
            if page.next_cursor is None:
                return tuple(events)
            after = page.next_cursor

    async def _continue_tool_result(
        self,
        run_id: str,
        request: ActionRequest,
        *,
        handlers: Mapping[str, Any],
        handler_deps: Mapping[str, Mapping[str, Any]],
    ) -> ToolResult:
        observation_id = str(request.arguments.get("observation_id") or "")
        cursor = str(request.arguments.get("next_cursor") or "")
        if not observation_id or not cursor:
            return ToolResult.error(
                "observation_id and next_cursor are required",
                "[Tool Error] observation_id and next_cursor are required",
            )
        observation: ToolObservation | None = None
        for event in await self._read_all_events(run_id):
            if event.event_type != "ToolObservationCommitted":
                continue
            if event.payload.get("observation_id") != observation_id:
                continue
            artifact = event.payload.get("observation_artifact")
            if not artifact:
                continue
            try:
                observation = ToolObservation.from_dict(
                    json.loads(self.store.artifacts.read_text(str(artifact)))
                )
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
            break
        if observation is None:
            return ToolResult.error(
                "observation not found",
                "[Tool Error] observation is not available in this run",
            )
        if observation.complete or observation.effect_class != EffectClass.READ_ONLY.value:
            return ToolResult.error(
                "observation is not continuable",
                "[Tool Error] only incomplete read-only observations can be continued",
            )
        original_tool = str(observation.metadata.get("tool_name") or observation.tool_name)
        original_args = observation.metadata.get("request_arguments")
        if not isinstance(original_args, Mapping) or original_tool not in handlers:
            return ToolResult.error(
                "continuation source is unavailable",
                "[Tool Error] the original read-only tool cannot be resumed",
            )
        args = dict(original_args)
        cursor_kind = str(observation.metadata.get("cursor_kind") or "cursor")
        try:
            numeric_cursor = int(cursor)
        except ValueError:
            numeric_cursor = None
        if numeric_cursor is None:
            return ToolResult.error(
                "next_cursor must be numeric for this tool",
                "[Tool Error] next_cursor is not valid for the original tool",
            )
        if cursor_kind == "character_offset":
            args["character_offset"] = numeric_cursor
        elif cursor_kind == "offset":
            args["offset"] = numeric_cursor
        else:
            args["cursor"] = numeric_cursor
        result = await handlers[original_tool](
            args,
            cwd=str(self.cwd),
            **dict(handler_deps.get(original_tool) or {}),
        )
        if not isinstance(result, ToolResult):
            result = ToolResult.success(str(result))
        metadata = dict(result.metadata)
        metadata.update(
            {
                "continued_from_observation": observation_id,
                "continued_tool_name": original_tool,
                "continued_request_arguments": args,
            }
        )
        return ToolResult(
            is_error=result.is_error,
            display_text=result.display_text,
            llm_text=result.llm_text,
            source=result.source,
            trusted=result.trusted,
            capability=result.capability,
            metadata=metadata,
        )

    async def _recall_run_context(self, run_id: str, arguments: Mapping[str, Any]) -> ToolResult:
        try:
            projection = await self.store.load_projection(run_id)
            record = await self.store.load_run_record(run_id)
        except (RunNotFound, RunStoreError, AttributeError, TypeError, ValueError) as exc:
            return ToolResult.error(
                "durable run context is unavailable",
                "[Tool Error] RecallRunContext context is unavailable; resume from the last checkpoint",
                source="durable_recall",
                capability="authorized_run_context",
                metadata={"error_type": type(exc).__name__},
            )
        authorized = {run_id}
        for value in (record.parent_run_id, record.predecessor_run_id):
            if value:
                authorized.add(value)
        authorized.update(
            item.child_run_id
            for item in getattr(projection, "children", ())
            if getattr(item, "child_run_id", None)
        )
        source_run_id = str(arguments.get("source_run_id") or run_id)
        if source_run_id not in authorized:
            return ToolResult.error(
                "source run is outside the authorized recall scope",
                "[Tool Error] RecallRunContext is limited to this run and explicit handoffs",
            )
        try:
            after = max(0, int(arguments.get("after_sequence", 0)))
            limit = min(100, max(1, int(arguments.get("limit", 50))))
        except (TypeError, ValueError):
            return ToolResult.error(
                "invalid recall range",
                "[Tool Error] after_sequence and limit must be integers",
            )
        requested_types = {
            str(item)
            for item in (arguments.get("event_types") or ())
            if str(item).strip()
        }
        events = await self._read_all_events(source_run_id)
        selected = [
            event
            for event in events
            if event.sequence > after
            and (not requested_types or event.event_type in requested_types)
        ]
        complete = len(selected) <= limit
        selected = selected[:limit]
        payload = {
            "source_run_id": source_run_id,
            "after_sequence": after,
            "complete": complete,
            "next_cursor": None if complete or not selected else str(selected[-1].sequence),
            "events": [
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "payload": dict(event.payload),
                    "artifact_refs": list(event.artifact_refs),
                }
                for event in selected
            ],
        }
        return ToolResult.success(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            source="durable_recall",
            capability="authorized_run_context",
            metadata={
                "complete": complete,
                "next_cursor": payload["next_cursor"],
            },
        )

    async def _execute_tool(
        self,
        request: ActionRequest,
        *,
        run_id: str,
        context_deps: Mapping[str, Any] | None = None,
        handlers: Mapping[str, Any],
        handler_deps: Mapping[str, Mapping[str, Any]],
        contracts: ToolContractRegistry,
        l5,
    ) -> ActionExecutionResult:
        result: ToolResult | None = None
        if self._policy is not None:
            projection = await self.store.load_projection(run_id)
            history = await materialize_interaction_messages(self.store, projection)
            tool_results = [
                str(message.get("content") or "")
                for message in history
                if message.get("role") == "tool"
            ]
            policy_arguments = {
                key: value for key, value in request.arguments.items() if key != "capability_id"
            }
            decision = self._policy.evaluate(
                request.tool_name,
                policy_arguments,
                {
                    "project_root": self.cwd,
                    "provenance_mode": self._services.provenance_mode
                    if self._services is not None
                    else False,
                    "messages": history,
                    "tool_results": tool_results,
                    "tool_result_records": (),
                    "capability_metadata": self._security_capability_metadata.get(
                        request.tool_name
                    ),
                },
            )
            if decision.action is Action.DENY:
                result = ToolResult.error(
                    "security policy denied the action",
                    f"[Security Error] {decision.reason}",
                    source="security_policy",
                    capability="policy_denied",
                    metadata={
                        "security_decision": decision.rule_id,
                        "security_evidence": decision.evidence,
                        "safety_applied": True,
                    },
                )
        command_lock = request.tool_name == "run_command"
        if command_lock:
            await self._workspace_command_lock.acquire()
        workspace_before = _workspace_snapshot(self.cwd) if command_lock else {}
        try:
            execution_request = request
            capability_id = request.arguments.get("capability_id")
            if capability_id:
                secret = self._capability_broker.resolve(
                    str(capability_id),
                    run_id=run_id,
                    action_id=request.action_id,
                    scope=request.tool_name,
                )
                handler = handlers.get(request.tool_name)
                annotations = getattr(handler, "__annotations__", {}) if handler else {}
                if handler is None or "capability_secret" not in annotations:
                    raise CredentialBrokerError(
                        "the selected tool does not declare a capability secret sink"
                    )
                execution_arguments = dict(request.arguments)
                execution_arguments.pop("capability_id", None)
                execution_arguments["capability_secret"] = secret
                execution_request = replace(request, arguments=execution_arguments)
            if result is not None:
                pass
            elif request.tool_name == "ContinueToolResult":
                result = await self._continue_tool_result(
                    run_id,
                    execution_request,
                    handlers=handlers,
                    handler_deps=handler_deps,
                )
            elif request.tool_name == "RecallRunContext":
                result = await self._recall_run_context(run_id, execution_request.arguments)
            elif request.tool_name in handlers:
                result = await handlers[request.tool_name](
                    execution_request.arguments,
                    cwd=str(self.cwd),
                    **dict(handler_deps.get(request.tool_name) or {}),
                )
            elif self._mcp is not None:
                result = await self._mcp.call_tool(
                    request.tool_name, dict(execution_request.arguments)
                )
            else:
                result = ToolResult.error(
                    display="MCP runtime is not connected",
                    llm="[Tool Error] MCP runtime is not connected",
                )
            if not isinstance(result, ToolResult):
                result = ToolResult.success(str(result))
            if command_lock:
                workspace_after = _workspace_snapshot(self.cwd)
                change_set = _workspace_change_set(workspace_before, workspace_after)
                baseline_encoded = json.dumps(
                    workspace_before,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                metadata = dict(result.metadata)
                metadata.update(
                    {
                        "workspace_baseline": {
                            "digest": f"sha256:{hashlib.sha256(baseline_encoded).hexdigest()}",
                            "file_count": len(workspace_before),
                        },
                        "workspace_change_set": change_set,
                    }
                )
                result = ToolResult(
                    is_error=result.is_error,
                    display_text=result.display_text,
                    llm_text=result.llm_text,
                    source=result.source,
                    trusted=result.trusted,
                    capability=result.capability,
                    metadata=metadata,
                )
        except BaseException as exc:
            return ActionExecutionResult(
                ActionStatus.OUTCOME_UNKNOWN,
                error_kind=type(exc).__name__,
            )
        finally:
            if command_lock:
                self._workspace_command_lock.release()

        text = sanitize(result.llm_text, l5)
        structured = self._structured_result(result.llm_text)
        structured_for_observation = None
        if structured is not None:
            structured_for_observation = _sanitize_value(structured, l5)
            for key in ("content", "stdout", "stderr"):
                value = structured_for_observation.get(key)
                if isinstance(value, str) and len(value) > 4_096:
                    structured_for_observation[key] = (
                        f"<stored in result artifact; {len(value)} characters>"
                    )
        metadata = _sanitize_value(dict(result.metadata), l5)
        metadata.setdefault("sanitized_by_l5", l5 is not None)
        metadata.setdefault("safety_applied", self._policy is not None)
        model_text = text
        offload = (context_deps or {}).get("offload")
        if (
            not result.is_error
            and (context_deps or {}).get("enabled")
            and callable(offload)
        ):
            try:
                offloaded = await offload(
                    text,
                    request.tool_name,
                    dict(request.arguments),
                    threshold=int((context_deps or {}).get("threshold", 0)),
                    token_counter=(context_deps or {}).get("token_counter"),
                )
            except Exception as exc:  # noqa: BLE001 - offload is fail-soft
                metadata["offload_error"] = f"{type(exc).__name__}: {exc}"
                model_text = (
                    "[Large tool result withheld because durable offload failed; "
                    "the committed result artifact remains authoritative]"
                )
            else:
                if offloaded is not None:
                    model_text = offloaded.pointer_msg
                    metadata["offload"] = {
                        "node_id": offloaded.node_id,
                        "summary": offloaded.summary,
                        "refs_path": offloaded.refs_path,
                        "pointer_msg": offloaded.pointer_msg,
                        "content_digest": offloaded.content_digest,
                        "size_bytes": offloaded.size_bytes,
                    }
        if structured is not None:
            metadata.setdefault("structured_preview", {
                key: value
                for key, value in structured.items()
                if key not in {"content", "stdout", "stderr"}
            })
        explicit_complete = metadata.get("complete")
        if explicit_complete is None and structured is not None and "complete" in structured:
            explicit_complete = structured["complete"]
        if explicit_complete is None and structured is not None and "truncated" in structured:
            explicit_complete = not bool(structured["truncated"])
        complete = bool(explicit_complete) if explicit_complete is not None else True
        next_cursor = (
            metadata.get("next_cursor")
            or metadata.get("next_offset")
            or metadata.get("next_character_offset")
        )
        cursor_kind = "cursor"
        if next_cursor is None and structured is not None:
            if structured.get("next_cursor") is not None:
                next_cursor = structured["next_cursor"]
            elif structured.get("next_offset") is not None:
                next_cursor = structured["next_offset"]
                cursor_kind = "offset"
            elif structured.get("next_character_offset") is not None:
                next_cursor = structured["next_character_offset"]
                cursor_kind = "character_offset"
        if metadata.get("next_character_offset") is not None:
            cursor_kind = "character_offset"
        elif metadata.get("next_offset") is not None:
            cursor_kind = "offset"
        if next_cursor is not None:
            next_cursor = str(next_cursor)
            complete = False if explicit_complete is None else complete
        effect_contract = contracts.get(request.tool_name)
        effect = (
            effect_contract.effect_class.value
            if isinstance(effect_contract.effect_class, EffectClass)
            else str(effect_contract.effect_class)
        )
        original_tool = str(metadata.get("continued_tool_name") or request.tool_name)
        path = request.arguments.get("path")
        if not isinstance(path, str) and isinstance(metadata.get("continued_request_arguments"), Mapping):
            path = metadata["continued_request_arguments"].get("path")
        paths = (str(path),) if isinstance(path, str) and path else ()
        modified = paths if original_tool in {"Edit", "Write"} else ()
        if request.tool_name == "run_command":
            modified = tuple(
                str(item)
                for item in (metadata.get("workspace_change_set") or {}).get("changed_paths", ())
            )
        read = paths if original_tool in {"Read", "Glob", "Grep", "Edit", "Write"} else ()
        if original_tool in {"Read", "Glob", "Grep", "read_ref", "search_ref", "inspect_node"}:
            metadata.setdefault("tool_name", original_tool)
            continued_args = metadata.get("continued_request_arguments")
            if isinstance(continued_args, Mapping):
                metadata["request_arguments"] = dict(continued_args)
            else:
                metadata.setdefault("request_arguments", dict(request.arguments))
            metadata.setdefault("cursor_kind", cursor_kind)
        stale = bool(
            result.is_error
            and original_tool in {"Edit", "Write"}
            and any(
                marker in text.casefold()
                for marker in ("stale content hash", "file changed before commit", "exact old_text")
            )
        )
        if stale:
            metadata["mutation_conflict"] = {
                "kind": "stale_content",
                "requires_reread": True,
                "path": path,
            }
        artifact_payload = {
            "tool_name": request.tool_name,
            "is_error": bool(result.is_error),
            "text": text[:256_000],
            "model_text": model_text[:256_000],
            "source": result.source,
            "capability": result.capability,
            "metadata": metadata,
        }
        artifact = self.store.artifacts.put_text(
            json.dumps(artifact_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            media_type="application/json; purpose=tool-result",
        )
        if result.is_error:
            command_outcome_uncertain = bool(
                request.tool_name == "run_command"
                and (
                    metadata.get("timed_out")
                    or metadata.get("exit_code") is None
                )
            )
            status = (
                ActionStatus.OUTCOME_UNKNOWN
                if effect_contract.effect_class is EffectClass.EXTERNAL_SIDE_EFFECT
                or command_outcome_uncertain
                else ActionStatus.FAILED
            )
            error_kind = "stale_content" if stale else "tool_error"
            recovery = (
                "reread"
                if stale
                else "reconcile"
                if status is ActionStatus.OUTCOME_UNKNOWN
                else "none"
            )
            observation = make_observation(
                action_id=request.action_id,
                attempt=1,
                tool_name=request.tool_name,
                status="unknown" if status is ActionStatus.OUTCOME_UNKNOWN else "failed",
                effect_class=effect,
                text=model_text,
                result_data=structured_for_observation,
                result_artifact=artifact.digest,
                complete=complete,
                next_cursor=next_cursor,
                read_paths=read,
                modified_paths=modified,
                error_kind=error_kind,
                recovery=recovery,
                provenance=(result.source, result.capability),
                metadata=metadata,
            )
            return ActionExecutionResult(
                status,
                result_artifact=artifact.digest,
                observation=observation,
                error_kind=error_kind,
                modified_paths=modified,
                read_paths=read,
                complete=complete,
                next_cursor=next_cursor,
            )
        observation = make_observation(
            action_id=request.action_id,
            attempt=1,
            tool_name=request.tool_name,
            status="succeeded",
            effect_class=effect,
            text=model_text,
            result_data=structured_for_observation,
            result_artifact=artifact.digest,
            complete=complete,
            next_cursor=next_cursor,
            read_paths=read,
            modified_paths=modified,
            provenance=(result.source, result.capability),
            metadata=metadata,
        )
        return ActionExecutionResult(
            ActionStatus.SUCCEEDED,
            result_artifact=artifact.digest,
            observation=observation,
            modified_paths=modified,
            read_paths=read,
            complete=complete,
            next_cursor=next_cursor,
        )

    async def close(self) -> None:
        if self.supervisor is not None:
            await self.supervisor.stop(drain=False)
            self.supervisor = None
        if self._mcp is not None:
            await self._mcp.shutdown()
            self._mcp = None
        if self._capabilities is not None:
            await self._capabilities.close()
            self._capabilities = None
        for record in await self.store.list_runs():
            self._capability_broker.revoke_run(record.run_id)
        if self._services is not None and self._services.l2_client is not None:
            await self._services.l2_client.close()
            self._services.l2_client = None
        self._services = None
        if self._llm is not None:
            await self._llm.aclose()
            self._llm = None
        if self._execution_started:
            await shutdown_session_executor()
            self._execution_started = False
        await self.store.close()


__all__ = ["DurableModelAdapter", "DurableRuntimeClient"]
