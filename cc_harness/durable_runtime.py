"""Client and local execution service for the rebuilt durable runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .action_contracts import ToolContractRegistry, ToolRecoveryContract
from .activation import ActivationManifest, CapabilityProfile
from .capability_runtime import AgentCapabilityRuntime
from .capability_services import SharedCapabilityServices
from .config import ExecutorBackend, load_layered_config
from .context import _repair_tool_result_pairing
from .coordinator import RunCoordinator, RunRequest
from .credential_broker import ActionScopedCapabilityBroker, CredentialBrokerError
from .interaction_history import materialize_interaction_messages
from .project_instructions import load_project_instructions
from .tool_bundles import parse_tool_bundles, select_tool_specs
from .llm import LLMClient, ProviderProtocolError, normalize_thinking_mode
from .mcp_client import MCPClient, ToolResult
from .native_tools import NATIVE_FILE_TOOLS
from .policy import Action
from .run_kernel import ActionRequest, ModelAdapter, ModelSegment, ReActKernel
from .run_events import EventActor
from .run_model import ActionStatus, CompletionCandidate, EffectClass, RunStatus
from .run_store import RunNotFound, RunStore, RunStoreError
from .run_telemetry import aggregate_model_usage
from .supervisor import LocalSupervisor
from .tools import (
    RUN_COMMAND_SPEC,
    init_session_executor,
    prewarm_session_executor,
    run_command,
    shutdown_session_executor,
)
from .l5 import sanitize
from .tool_observation import CONTINUE_TOOL_RESULT_SPEC, ToolObservation, make_observation
from .worker import ActionExecutionResult, RunWorker
from .durable_subagents import (
    ACCEPT_CHILD_CANDIDATE_SPEC,
    DISPATCH_SUBAGENT_SPEC,
    REJECT_CHILD_CANDIDATE_SPEC,
    durable_accept_child_candidate_handler,
    durable_dispatch_subagent_handler,
    durable_reject_child_candidate_handler,
)
from .worktrees import WorktreeManager


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

    def __init__(self, llm: LLMClient, *, tool_bundles=None) -> None:
        self.llm = llm
        self.tool_bundles = tool_bundles

    @staticmethod
    def _provider_messages(
        messages: Sequence[Mapping[str, Any]],
        *,
        thinking_mode: str = "auto",
        reasoning_content_required: bool = False,
    ) -> list[dict[str, Any]]:
        """Build provider messages without changing replay semantics.

        Durable event metadata is deliberately removed at this boundary, but
        provider-owned fields are copied verbatim.  Thinking-mode providers
        require the original ``reasoning_content`` next to an assistant tool
        call; when that field is required, a missing value is a protocol error
        rather than an opportunity to synthesize an empty string.
        """

        allowed = {
            "role",
            "content",
            "name",
            "tool_call_id",
            "tool_calls",
            "reasoning_content",
            "refusal",
        }
        mode = normalize_thinking_mode(thinking_mode)
        strict_reasoning = mode == "enabled" or (
            mode == "auto" and reasoning_content_required
        )
        if strict_reasoning:
            # Validate the authoritative assistant artifacts before repairing
            # malformed tool turns.  A missing provider-owned reasoning field
            # is a deterministic replay error and must remain visible rather
            # than being hidden by dropping the incomplete tool call.
            for index, message in enumerate(messages):
                if message.get("role") != "assistant" or not message.get("tool_calls"):
                    continue
                if "reasoning_content" not in message:
                    raise ProviderProtocolError(
                        "assistant tool-call replay is missing reasoning_content",
                        field="reasoning_content",
                        message_index=index,
                    )
                if not isinstance(message["reasoning_content"], str):
                    raise ProviderProtocolError(
                        "assistant tool-call reasoning_content must be a string",
                        field="reasoning_content",
                        message_index=index,
                    )

        result: list[dict[str, Any]] = []
        # This is a final provider-boundary guard.  Durable projections already
        # normalize turns, but custom message providers and restored legacy
        # contexts can bypass that path.  Never fabricate a missing result;
        # retain only complete, declaration-ordered tool-call pairs.
        normalized_messages = _repair_tool_result_pairing(
            [dict(message) for message in messages]
        )
        for message in normalized_messages:
            copied = {key: value for key, value in dict(message).items() if key in allowed}
            if mode == "disabled":
                copied.pop("reasoning_content", None)
            result.append(copied)
        return result

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
        thinking_mode = getattr(self.llm, "thinking_mode", "auto")
        reasoning_content_required = bool(
            getattr(self.llm, "reasoning_content_required", False)
        )
        stream = self.llm.chat(
            self._provider_messages(
                messages,
                thinking_mode=thinking_mode,
                reasoning_content_required=reasoning_content_required,
            ),
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
        usage_payload: dict[str, Any] = (
            {
                "input_tokens": usage.prompt_tokens,
                "uncached_input_tokens": usage.uncached_prompt_tokens,
                "cache_creation_input_tokens": usage.cache_creation_prompt_tokens,
                "cache_read_input_tokens": usage.cache_read_prompt_tokens,
                "output_tokens": usage.completion_tokens,
                "model_calls": 1,
            }
            if usage is not None else {"model_calls": 1}
        )
        if usage is not None:
            usage_payload["reported_cost"] = usage.reported_cost
            usage_payload["reported_cost_currency"] = usage.reported_cost_currency
        # Provider/model identity is stored with the usage event, not inferred
        # later from mutable process configuration.  This is especially
        # important when a resumed run changes the requested model.
        usage_payload["model"] = str(getattr(self.llm, "resolved_model", None) or self.llm.model)
        usage_payload["thinking_mode"] = str(getattr(self.llm, "thinking_mode", "auto"))
        usage_payload["thinking_fallback_used"] = bool(
            getattr(self.llm, "thinking_fallback_used", False)
        )
        base_url = str(getattr(self.llm, "base_url", None) or "")
        usage_payload["provider"] = (
            base_url.split("//", 1)[-1].split("/", 1)[0]
            if base_url
            else "openai-compatible"
        )
        from .prompt_rules import production_rule_metadata
        from .tool_bundles import bundle_digest
        system_content = next(
            (
                message.get("content")
                for message in messages
                if message.get("role") == "system" and isinstance(message.get("content"), str)
            ),
            "",
        )
        prompt_meta = {
            "version": "core-v2",
            "digest": hashlib.sha256(system_content.encode("utf-8")).hexdigest(),
            "rules_digest": production_rule_metadata()["digest"],
            "tool_bundle_digest": bundle_digest(list(tools), self.tool_bundles),
            "tool_bundle_count": len(tools),
        }
        prompt_meta["cache_epoch"] = hashlib.sha256(
            "|".join(
                (
                    str(prompt_meta["version"]),
                    str(prompt_meta["rules_digest"]),
                    str(prompt_meta["tool_bundle_digest"]),
                )
            ).encode("utf-8")
        ).hexdigest()[:20]
        usage_payload["prompt_metadata"] = prompt_meta
        return ModelSegment(
            text=text,
            tool_calls=tuple(calls),
            completion_candidate=candidate,
            stop_reason=finish_reason,
            usage=usage_payload,
            reasoning_content=reasoning_content,
        )


class DurableRuntimeClient:
    """Own the durable store/coordinator and optionally a local supervisor.

    Creating a client is intentionally configuration-light: status/list and
    migration commands must work even when model credentials are unavailable.
    Interactive control uses ``start_detached_supervisor`` so closing the TUI
    does not cancel work; direct/headless execution may opt into
    ``start_supervisor`` or the CLI ``--command supervisor``.
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
        self.project_instructions = None
        self.tool_bundles = None

    @classmethod
    async def create(
        cls,
        cwd: Path,
        *,
        data_root: Path | None = None,
    ) -> "DurableRuntimeClient":
        store = RunStore(Path(cwd), data_root=data_root)
        await store.open()
        worktrees = WorktreeManager(Path(cwd), state_root=store.state_dir, artifacts=store.artifacts)
        return cls(store, RunCoordinator(store, worktrees=worktrees))

    async def submit(
        self,
        objective: str,
        acceptance_criteria: tuple[str, ...] = ("request addressed",),
        *,
        confirm_high_risk: bool = False,
    ) -> str:
        # Terminal-Bench supplies a frozen official task statement inside an
        # isolated Harbor container.  Its instructions can describe external
        # operations (for example ``git push``) without being a live user's
        # request.  Keep the provenance explicit and auditable; never infer it
        # from the objective text.
        if (
            os.getenv("CC_HARNESS_TRUSTED_BENCHMARK_TASK", "") == "1"
            and os.getenv("CC_HARNESS_TERMINAL_BENCH", "") == "1"
        ):
            goal_provenance = "official_benchmark"
        elif confirm_high_risk:
            goal_provenance = "user_confirmed"
        else:
            goal_provenance = "user"
        return (
            await self.coordinator.submit(
                RunRequest(
                    objective,
                    acceptance_criteria,
                    goal_provenance=goal_provenance,
                )
            )
        ).run_id

    async def _resolve_run_root(self, run_id: str) -> Path:
        """Resolve a child execution root from the parent's persisted DAG."""
        try:
            record = await self.store.load_run_record(run_id)
            if record.parent_run_id:
                parent = await self.store.load_projection(record.parent_run_id)
                child = next((item for item in parent.children if item.child_run_id == run_id), None)
                node = next((item for item in parent.plan.nodes if child and item.node_id == child.node_id), None)
                if node and node.worktree_id:
                    path = Path(node.worktree_id).resolve()
                    if path.is_dir():
                        return path
        except Exception:  # noqa: BLE001 - recovery falls back to the project root
            pass
        return self.cwd

    async def _on_child_completed(self, child_run_id: str, candidate: CompletionCandidate) -> None:
        try:
            record = await self.store.load_run_record(child_run_id)
            if not record.parent_run_id:
                return
            child_projection = await self.store.load_projection(child_run_id)
            parent = await self.store.load_projection(record.parent_run_id)
            child = next((item for item in parent.children if item.child_run_id == child_run_id), None)
            if child is None or child.status in {"completed", "accepted", "candidate_submitted"}:
                return
            node = next((item for item in parent.plan.nodes if item.node_id == child.node_id), None)
            if node is None or node.effect_class == EffectClass.READ_ONLY.value:
                await self.coordinator._append(
                    record.parent_run_id,
                    "ChildRunCompleted",
                    {"child_run_id": child_run_id},
                    EventActor("coordinator", "local-coordinator"),
                )
                return
            if self.coordinator.worktrees is None or not node.worktree_id or not node.worktree_base_commit:
                raise RuntimeError("mutating child has no isolated worktree metadata")
            worktree = self.coordinator.worktrees.create_child(
                record.parent_run_id,
                child_run_id,
                base_commit=node.worktree_base_commit,
                depth=node.depth,
                write=True,
            )
            change_set = self.coordinator.worktrees.commit_candidate(
                worktree,
                message=(child_projection.goal.objective if child_projection.goal else f"child {child_run_id}")[:200],
                owned_paths=node.owned_paths,
                verification_evidence=candidate.evidence,
            )
            await self.coordinator.submit_child_candidate(record.parent_run_id, change_set)
        except Exception as exc:  # noqa: BLE001 - preserve a durable conflict instead of failing silently
            try:
                record = await self.store.load_run_record(child_run_id)
                if record.parent_run_id:
                    await self.coordinator._append(
                        record.parent_run_id,
                        "IntegrationConflictRaised",
                        {"child_run_id": child_run_id, "paths": [], "reason": str(exc)},
                        EventActor("coordinator", "local-coordinator"),
                    )
            except Exception:
                pass

    async def _on_child_cancelled(self, child_run_id: str, reason: str) -> None:
        try:
            record = await self.store.load_run_record(child_run_id)
            if not record.parent_run_id:
                return
            parent = await self.store.load_projection(record.parent_run_id)
            child = next((item for item in parent.children if item.child_run_id == child_run_id), None)
            if child and child.status not in {"cancelled", "completed", "accepted", "candidate_submitted"}:
                await self.coordinator._append(record.parent_run_id, "ChildRunCancelled", {"child_run_id": child_run_id, "reason": reason}, EventActor("coordinator", "local-coordinator"))
        except Exception:
            pass

    async def _on_child_failed(self, child_run_id: str, reason: str) -> None:
        try:
            record = await self.store.load_run_record(child_run_id)
            if not record.parent_run_id:
                return
            parent = await self.store.load_projection(record.parent_run_id)
            child = next((item for item in parent.children if item.child_run_id == child_run_id), None)
            if child and child.status not in {"failed", "cancelled", "completed", "accepted", "candidate_submitted"}:
                await self.coordinator._append(
                    record.parent_run_id,
                    "ChildRunFailed",
                    {"child_run_id": child_run_id, "reason": reason},
                    EventActor("coordinator", "local-coordinator"),
                )
        except Exception:
            pass

    async def run_tree(self, root_run_id: str) -> tuple[str, ...]:
        """Return a root Run plus all persisted descendants in stable order."""
        records = await self.store.list_runs()
        children: dict[str, list[str]] = {}
        for record in records:
            if record.parent_run_id:
                children.setdefault(record.parent_run_id, []).append(record.run_id)
        result: list[str] = []
        stack = [root_run_id]
        while stack:
            current = stack.pop(0)
            if current in result:
                continue
            result.append(current)
            stack.extend(sorted(children.get(current, ())))
        return tuple(result)

    async def usage_for_run(self, root_run_id: str) -> dict[str, Any]:
        """Aggregate provider usage for a root Run and its durable descendants."""

        run_ids = await self.run_tree(root_run_id)
        events = []
        for run_id in run_ids:
            page = await self.store.read(run_id, limit=100_000)
            events.extend(page.events)
        summary = aggregate_model_usage(events)
        summary.update({"run_id": root_run_id, "run_count": len(run_ids)})
        return summary

    async def terminate_run_tree(
        self,
        root_run_id: str,
        *,
        reason: str = "explicit Ctrl+C termination",
        grace_seconds: float = 1.0,
    ) -> None:
        """Stop scheduling and durably request cancellation for the whole tree."""
        run_ids = await self.run_tree(root_run_id)
        for run_id in reversed(run_ids):
            try:
                await self.coordinator.interrupt(run_id, reason)
            except Exception:
                continue
        if grace_seconds > 0:
            await asyncio.sleep(grace_seconds)
        for run_id in reversed(run_ids):
            try:
                receipt = await self.coordinator.cancel(run_id, reason)
                # A queued child has no worker to emit the parent-side child
                # cancellation event.  Mirror only an already-finalized
                # cancellation; a live worker remains CANCEL_REQUESTED until
                # its safe boundary records outcome_unknown/RunCancelled.
                if receipt.status is RunStatus.CANCELLED:
                    await self._on_child_cancelled(run_id, reason)
            except Exception:
                continue
        if self.supervisor is not None:
            await self.supervisor.stop(drain=False)
            self.supervisor = None

    async def continuation_candidates(self) -> tuple[Any, ...]:
        """Find recoverable root Runs in this project only."""
        records = {record.run_id: record for record in await self.store.list_runs()}
        views = await self.coordinator.list()
        eligible = {
            RunStatus.CANCELLED,
            RunStatus.STALLED,
            RunStatus.FAILED_RECOVERABLE,
            RunStatus.WAITING_ON_PREDECESSOR,
        }
        return tuple(
            view for view in views
            if records.get(view.run_id) is not None
            and records[view.run_id].parent_run_id is None
            and view.status in eligible
        )

    async def continue_run(self, run_id: str, *, reason: str = "natural-language continuation") -> str:
        root_view = await self.coordinator.inspect(run_id)
        await self.coordinator.resume(run_id, reason)
        # Ctrl+C/cancellation is recorded independently for every Run in the
        # tree.  Once the parent is resumed, re-queue only recoverable
        # descendants; terminal failures and already completed children stay
        # untouched.  This keeps continuation checkpoint-based and avoids
        # creating duplicate child Runs.
        recoverable = {
            RunStatus.CANCELLED,
            RunStatus.STALLED,
            RunStatus.FAILED_RECOVERABLE,
            RunStatus.WAITING_ON_PREDECESSOR,
        }
        if root_view.status in recoverable:
            for descendant_id in (await self.run_tree(run_id))[1:]:
                descendant = await self.coordinator.inspect(descendant_id)
                if descendant.status in recoverable:
                    await self.coordinator.resume(
                        descendant_id,
                        f"{reason}; recover descendant checkpoint",
                    )
        if self.supervisor is None:
            await self.start_supervisor()
        return run_id

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
        self.tool_bundles = parse_tool_bundles(
            config.runtime_environment.get("CC_HARNESS_TOOL_BUNDLES")
        )
        self.project_instructions = load_project_instructions(self.cwd)
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
            thinking_mode=config.runtime_environment.get("CC_HARNESS_THINKING_MODE"),
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
        # Mark the session executor as owned before any eager preflight.  If
        # Docker/OpenSandbox is unavailable, client.close() must still clean
        # masks, an owned server process, and the executor singleton.
        self._execution_started = True
        if executor_config.backend is ExecutorBackend.SANDBOX:
            try:
                server_state = await prewarm_session_executor()
            except Exception as exc:
                self._activation_manifest.degrade(
                    "runtime",
                    f"sandbox_server_unavailable:{type(exc).__name__}:{exc}",
                )
                raise
            self._activation_manifest.initialize(
                "runtime",
                sandbox_server_ready=True,
                sandbox_server_owned=bool(getattr(server_state, "owned", False)),
                sandbox_server_endpoint=(
                    f"{executor_config.sandbox.server_host}:"
                    f"{executor_config.sandbox.server_port}"
                ),
                sandbox_server_version=getattr(server_state, "server_version", None),
                sandbox_server_config_digest=getattr(server_state, "config_digest", None),
                sandbox_server_egress_mode=getattr(server_state, "egress_mode", None),
            )
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
            context_window_source=self._capabilities.context_config.context_window_source,
            context_window_verified=self._capabilities.context_config.context_window_verified,
            thresholds=[
                self._capabilities.context_config.tier1_threshold,
                self._capabilities.context_config.tier2_threshold,
                self._capabilities.context_config.tier3_threshold,
            ],
            output_reserve_tokens=self._capabilities.context_config.output_reserve_tokens,
            tool_schema_reserve_tokens=self._capabilities.context_config.tool_schema_reserve_tokens,
            fail_closed=self._capabilities.context_config.fail_closed,
            offload_enabled=self._capabilities.memory_config.offload_enabled,
        )
        if self._capabilities.memory_deps:
            self._activation_manifest.initialize(
                "memory",
                configured_enabled=bool(self._capabilities.memory_config.enabled),
                pipeline_enabled=bool(self._capabilities.memory_config.pipeline_enabled),
                layered_inject=bool(self._capabilities.memory_config.layered_inject),
                capture_enabled=bool(self._capabilities.memory_config.capture_enabled),
                background_services=sorted(self._capabilities.background_services),
            )
        else:
            self._activation_manifest.initialize(
                "memory",
                configured_enabled=bool(self._capabilities.memory_config.enabled),
                pipeline_enabled=False,
                layered_inject=False,
                capture_enabled=False,
                unavailable=True,
            )
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
        model = DurableModelAdapter(self._llm, tool_bundles=self.tool_bundles)
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
            worker_ref: dict[str, RunWorker] = {}
            worker = RunWorker(
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
                    working_root=worker_ref.get("worker", None).working_directory if worker_ref.get("worker") else self.cwd,
                ),
                contracts=run_contracts,
                available_tools=run_tool_specs,
                continue_segments=True,
                capability_runtime=self._capabilities,
                activation_manifest=self._activation_manifest,
                project_instructions=(
                    self.project_instructions.text
                    if self.project_instructions is not None else None
                ),
                child_completion_callback=self._on_child_completed,
                child_cancellation_callback=self._on_child_cancelled,
                child_failure_callback=self._on_child_failed,
                working_directory_resolver=self._resolve_run_root,
            )
            worker_ref["worker"] = worker
            return worker

        self.supervisor = LocalSupervisor(
            self.store,
            worker_factory,
            max_workers=max_workers,
        )
        await self.supervisor.start()
        return self.supervisor

    def start_detached_supervisor(
        self,
        *,
        reasoning_effort: str | None = None,
        host_execution: bool = False,
    ) -> int:
        """Ensure a supervisor process owns execution independently of the TUI.

        The interactive client is only a control plane.  A PID marker under the
        durable project state prevents duplicate supervisors; stale markers are
        harmless and are replaced.  Closing the terminal therefore closes only
        the UI/store client, while the detached worker keeps consuming queued
        Runs from the same SQLite event log.
        """

        if self.supervisor is not None:
            raise RuntimeError("a local supervisor is already attached to this client")
        pid_path = self.store.state_dir / "supervisor.pid"
        try:
            existing_pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            existing_pid = None
        if existing_pid is not None:
            try:
                os.kill(existing_pid, 0)
            except PermissionError:
                return existing_pid
            except (OSError, ProcessLookupError):
                existing_pid = None
            else:
                return existing_pid

        log_path = self.store.state_dir / "supervisor.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "cc_harness.entrypoint",
            "--command",
            "supervisor",
            "--cwd",
            str(self.cwd),
            "--data-root",
            str(self.store.data_root.resolve()),
        ]
        if reasoning_effort:
            command.extend(("--effort", reasoning_effort))
        if host_execution:
            command.append("--host-execution")
        log_file = log_path.open("a", encoding="utf-8")
        popen_kwargs: dict[str, Any] = {
            "cwd": str(self.cwd),
            "stdin": subprocess.DEVNULL,
            "stdout": log_file,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
        if os.name == "nt":
            detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            popen_kwargs["creationflags"] = detached | new_group
        else:
            popen_kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **popen_kwargs)
        except Exception:
            log_file.close()
            raise
        finally:
            # The child has its own inherited descriptor; the TUI must not keep
            # the log file open after spawning it.
            if not log_file.closed:
                log_file.close()
        pid_path.write_text(str(process.pid), encoding="utf-8")
        return process.pid

    async def run_supervisor_forever(
        self,
        *,
        auto_approve: bool = False,
        **kwargs: Any,
    ) -> None:
        """Keep the detached supervisor alive until the caller stops it.

        Non-interactive callers may explicitly select ``bypass-prompts``.  In
        that mode the supervisor must also drive the durable approval queue;
        otherwise a worker reaches ``awaiting_approval`` and remains parked
        forever because there is no REPL control loop to grant the request.
        The default remains approval-gated and sleeps as before.
        """

        await self.start_supervisor(**kwargs)
        while True:
            # A defensive self-heal for an unexpected supervisor-loop task
            # failure.  The detached process remains the single owner, but a
            # transient tick exception must not leave queued durable runs
            # invisible until the process is manually restarted.
            if self.supervisor is not None:
                await self.supervisor.start()
            if auto_approve:
                for view in await self.coordinator.list():
                    if view.status is not RunStatus.AWAITING_APPROVAL:
                        continue
                    pending = [
                        item
                        for item in view.projection.approvals
                        if item.status.value == "requested"
                    ]
                    for approval in pending:
                        await self.coordinator.approve(
                            run_id=view.run_id,
                            approval_id=approval.approval_id,
                            action_args_digest=approval.action_args_digest,
                        )
                await asyncio.sleep(0.25)
            else:
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
        for spec, name, handler in (
            (DISPATCH_SUBAGENT_SPEC, "dispatch_subagent", durable_dispatch_subagent_handler),
            (ACCEPT_CHILD_CANDIDATE_SPEC, "accept_child_candidate", durable_accept_child_candidate_handler),
            (REJECT_CHILD_CANDIDATE_SPEC, "reject_child_candidate", durable_reject_child_candidate_handler),
        ):
            copied = json.loads(json.dumps(spec, ensure_ascii=False))
            copied.setdefault("function", {})["x-cc-harness-capability"] = {
                "effect": EffectClass.READ_ONLY.value,
                "requires_user_intent": False,
                "source": "durable-child-coordinator",
            }
            contracts.register(
                ToolRecoveryContract(
                    name,
                    EffectClass.READ_ONLY,
                    retryable=False,
                    max_retries=0,
                    idempotent=False,
                    parallelizable=False,
                    requires_approval=False,
                    child_allowed=True,
                    metadata={"source": "durable-child-coordinator"},
                )
            )
            handlers[name] = handler
            handler_deps[name] = {"coordinator": self.coordinator}
            self._security_capability_metadata[name] = dict(copied["function"]["x-cc-harness-capability"])
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
        if self.tool_bundles is not None:
            native_names = {
                "run_command",
                "ContinueToolResult",
                "RecallRunContext",
                "dispatch_subagent",
                "accept_child_candidate",
                "reject_child_candidate",
                *NATIVE_FILE_TOOLS,
            }
            native_names.update(
                str((extra.get("spec") or {}).get("function", {}).get("name") or "")
                for extra in (extras or ())
            )
            specs = select_tool_specs(specs, self.tool_bundles, native_names=native_names)
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
        working_root: Path | None = None,
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
            cwd=str(working_root or self.cwd),
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
        working_root: Path | None = None,
    ) -> ActionExecutionResult:
        execution_root = Path(working_root or self.cwd).resolve()
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
                    "project_root": execution_root,
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
        workspace_before = _workspace_snapshot(execution_root) if command_lock else {}
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
                    working_root=execution_root,
                )
            elif request.tool_name == "RecallRunContext":
                result = await self._recall_run_context(run_id, execution_request.arguments)
            elif request.tool_name in handlers:
                dependencies = dict(handler_deps.get(request.tool_name) or {})
                if request.tool_name in {"dispatch_subagent", "accept_child_candidate", "reject_child_candidate"}:
                    dependencies["run_id"] = run_id
                result = await handlers[request.tool_name](
                    execution_request.arguments,
                    cwd=str(execution_root),
                    **dependencies,
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
                workspace_after = _workspace_snapshot(execution_root)
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
        # Persist a redacted argument snapshot with every observation.  The
        # runtime completion gate and recovery path need to know whether a
        # successful command was a verification/readiness probe; keeping this
        # in the observation artifact avoids guessing from model prose.
        metadata.setdefault(
            "request_arguments",
            _sanitize_value(dict(execution_request.arguments), l5),
        )
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
        pid_path = self.store.state_dir / "supervisor.pid"
        try:
            if int(pid_path.read_text(encoding="utf-8").strip()) == os.getpid():
                pid_path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
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
