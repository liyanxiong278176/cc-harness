"""Worker execution of one durable agent segment.

The worker owns only a lease-scoped segment. Every lifecycle, action,
approval, and yield transition is appended through :class:`RunStore`.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import re
import traceback
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .action_contracts import ToolContractRegistry
from .activation import ActivationManifest
from .capability_runtime import AgentCapabilityRuntime
from .interaction_history import assistant_message, materialize_interaction_messages, objective_messages
from .lease import LeaseManager, ResourceLeaseManager
from .loop_control import (
    TaskContract,
    artifact_validation_issues,
    completion_contract_from_instruction,
    _normalise_command,
    _text_digest,
)
from .llm import ProviderProtocolError
from .stream_errors import StreamErrorDetails, classify_stream_error
from .run_events import EventActor, RunEvent
from .local_verification import python_assertion_verifier
from .run_kernel import ActionRequest, AgentKernel, SegmentContext
from .execution_control import BudgetExhausted, ExecutionControl
from .kernel_middleware import KernelMiddleware
from .run_model import (
    ActionStatus,
    CompletionCandidate,
    CompletionGate,
    EffectClass,
    EvidenceKind,
    EvidenceRef,
    Lease,
    RunStatus,
)
from .run_projection import RunProjection
from .run_store import LeaseFenceError, RunStore, RunStoreError, SequenceConflict
from .run_outcomes import outcome_for_event
from .permissions import normalize_permission_mode, requires_approval_for_mode
from .tool_observation import ToolObservation, make_observation


@dataclass(frozen=True)
class ActionExecutionResult:
    status: ActionStatus
    result_artifact: str | None = None
    observation: ToolObservation | None = None
    observation_artifact: str | None = None
    error_kind: str | None = None
    modified_paths: tuple[str, ...] = ()
    read_paths: tuple[str, ...] = ()
    complete: bool = True
    next_cursor: str | None = None


ActionExecutor = Callable[[ActionRequest], Awaitable[ActionExecutionResult]]
CompletionVerifier = Callable[[CompletionCandidate], bool]
MessageProvider = Callable[[RunProjection], Sequence[Mapping[str, Any]]]
ChildCompletionCallback = Callable[[str, CompletionCandidate], Awaitable[None]]
ChildCancellationCallback = Callable[[str, str], Awaitable[None]]
ChildFailureCallback = Callable[[str, str], Awaitable[None]]
WorkingDirectoryResolver = Callable[[str], Awaitable[Path]]
StreamEmitter = Callable[[Mapping[str, Any]], Awaitable[None]]


class ModelInvocationTimeout(RuntimeError):
    """The provider did not finish one model segment before its watchdog."""


def _worker_failure_contract(
    exc: BaseException,
    *,
    fallback_code: str,
    fallback_phase: str,
    fallback_message: str,
) -> tuple[StreamErrorDetails, dict[str, Any]]:
    """Build a durable failure summary without letting classification block it."""

    try:
        candidate = getattr(exc, "stream_error", None)
        details = (
            candidate
            if isinstance(candidate, StreamErrorDetails)
            else classify_stream_error(exc)
        )
        payload = details.to_dict()
        if isinstance(payload, dict):
            return details, payload
    except Exception:
        # Durable RunFailed is more important than the optional diagnostic
        # taxonomy. Fall through to a small, locally constructed contract.
        pass

    fallback = StreamErrorDetails(
        code=fallback_code,
        phase=fallback_phase,
        retryable=False,
        next_action="检查 Runtime 错误详情后从最近检查点继续",
        message=fallback_message,
    )
    return fallback, {
        "code": fallback.code,
        "phase": fallback.phase,
        "retryable": fallback.retryable,
        "partial_output": fallback.partial_output,
        "attempt": fallback.attempt,
        "retry_after": fallback.retry_after,
        "next_action": fallback.next_action,
        "message": fallback.message,
    }


def _bounded_timeout(value: float | int | None, *, default: float, minimum: float = 0.1) -> float:
    """Normalize a watchdog duration without allowing an accidental infinity."""

    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return 0.0
    return max(minimum, min(parsed, 3600.0))


async def _cancel_task_bounded(task: asyncio.Task, *, grace_seconds: float = 0.2) -> bool:
    """Request cancellation without waiting forever for a non-cooperative task.

    ``asyncio.wait_for(task, timeout=...)`` first cancels *and then waits for
    the cancellation to finish*.  A provider that catches ``CancelledError``
    can therefore defeat the timeout and keep the Worker stuck in its own
    cleanup path.  ``asyncio.wait`` has a real wall-clock bound: unfinished
    tasks are deliberately retained as orphans and their durable action stays
    ``outcome_unknown`` until reconciliation.
    """

    if task.done():
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()
        return True
    task.cancel()
    try:
        done, _pending = await asyncio.wait(
            {task}, timeout=max(0.0, float(grace_seconds))
        )
    except asyncio.CancelledError:
        # The owning Worker is itself being cancelled.  Do not turn cleanup
        # into another unbounded await; the event loop will finish the orphan
        # when it can, while the Worker persists the safe unknown outcome.
        return False
    if task in done:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()
        return True
    # Keep the eventual exception from becoming an un-retrieved-task warning.
    # The orphan is intentionally not awaited here: a provider that ignores
    # cancellation must not hold the Worker hostage.  Its durable action is
    # already ``outcome_unknown`` and remains eligible for reconciliation.
    task.add_done_callback(_consume_orphan_task_result)
    return False


def _consume_orphan_task_result(task: asyncio.Task) -> None:
    """Drain a detached provider/action task once it eventually finishes."""

    with contextlib.suppress(BaseException):
        task.result()


_SENSITIVE_KEY = re.compile(
    r"(?:api.?key|access.?token|authorization|cookie|credential|password|passwd|private.?key|secret|token)",
    re.IGNORECASE,
)

# Completion evidence is derived from durable tool observations, never from a
# model's final prose.  Keep this vocabulary provider/task agnostic while
# avoiding the old substring-only ``test`` check (which treated ``latest`` or
# ``test-data.txt`` as a verification command).
_VERIFICATION_COMMAND_RE = re.compile(
    r"(?:\b(?:pytest|unittest|mypy|ruff|eslint|pyright|pylint|bandit)\b|"
    r"\b(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b|"
    r"\b(?:cargo|go|dotnet|mvnw?|gradlew?)\s+test\b|"
    r"\b(?:make|just|task)\s+(?:test|check|verify|lint|validate)\b|"
    r"\bctest(?:\s|$)|"
    r"(?:^|[;&|\s])(?:\./)?(?:tests?/[A-Za-z0-9_.-]+|test(?:s)?\.(?:sh|py|js|ts)|run_tests?)(?:\s|$)|"
    r"\b(?:python(?:3)?|bash|sh|node|ruby|perl)\b[^;&|\n]*(?:\btests?\b|(?:test|verify|validate|check)[_./-])|"
    r"\b(?:verify|validate|lint|typecheck|compile|build|smoke)\b|"
    r"\bgit\s+diff\s+--check\b|"
    r"\b(?:curl|wget|grpcurl)\b|\b(?:nc|netcat)\s+-z\b|"
    r"\b(?:systemctl\s+is-active|service\s+\S+\s+status)\b|"
    r"\bcmp\b|"
    r"(?:^|\s)--(?:check|verify|validate)\b)",
    re.IGNORECASE,
)


def _is_verification_command(tool_name: str, command: str) -> bool:
    """Return whether a successful command is decisive completion evidence."""

    if tool_name != "run_command":
        return False
    if "|" in command and "pipefail" not in command:
        return False  # a downstream success can mask the verifier's failure
    return bool(_VERIFICATION_COMMAND_RE.search(command or ""))


def _local_verifier_invocations(command: str) -> set[str]:
    """Identify the same local verifier across display/redirect wrappers."""
    invocations = set()
    for match in re.finditer(r"\b(?:python(?:3)?|pytest|ruff|mypy|node|npm|pnpm|yarn)\s+[^;|\n]+", command):
        core = re.split(r"\s+\d*[<>]", match.group())[0].strip()
        if _VERIFICATION_COMMAND_RE.search(core) and not re.search(r"\b(?:deploy|publish|push|upload)\b", core):
            invocations.add(" ".join(core.split()))
    return invocations


def _observation_verifies(observation: ToolObservation, command: str) -> bool:
    annotation = observation.metadata.get("verification")
    checked_script = (observation.tool_name == "run_command" and isinstance(annotation, Mapping)
                      and annotation.get("kind") == "local_python_assertions"
                      and isinstance(annotation.get("assertions"), int) and annotation["assertions"] > 0
                      and re.fullmatch(r"sha256:[0-9a-f]{64}", str(annotation.get("script_digest", "")))
                      and observation.metadata.get("exit_code") == 0)
    if observation.status != "succeeded" or not observation.complete or not (_is_verification_command(observation.tool_name, command) or checked_script):
        return False
    text = "\n".join(str(block.text or "") for block in observation.content)
    return not re.search(r"(?im)^\s*(?:[\w]*exit(?:_code)?|returncode)\s*[=:]\s*-?[1-9]\d*\s*$", text)


def _observation_is_decisive(
    observation: ToolObservation,
    *,
    command: str = "",
) -> bool:
    """Return whether an observation proves progress beyond a read-only lookup.

    Verification commands are decisive by definition.  A successful mutation
    is also a meaningful completion boundary for the official benchmark when
    the task's external verifier is the final oracle.  The latter is guarded
    by durable effect/changed-path metadata, never by model prose, so a plain
    ``Read``/``Glob``/``Grep`` result cannot close a run accidentally.
    """

    if _is_verification_command(observation.tool_name, command):
        return True
    if observation.modified_paths:
        return True
    effect = str(observation.effect_class or "").casefold()
    if effect in {
        EffectClass.WORKSPACE_MUTATION.value,
        EffectClass.EXTERNAL_SIDE_EFFECT.value,
    }:
        return True
    change_set = observation.metadata.get("workspace_change_set")
    return isinstance(change_set, Mapping) and bool(
        change_set.get("changed_paths") or change_set.get("created_paths")
    )


def _is_benchmark_task() -> bool:
    """Identify the frozen official benchmark path without trusting task text."""

    return (
        os.getenv("CC_HARNESS_TERMINAL_BENCH", "") == "1"
        and os.getenv("CC_HARNESS_TRUSTED_BENCHMARK_TASK", "") == "1"
    )


def _goal_public_text(goal) -> str:
    """Render the durable public goal for deterministic contract extraction.

    Completion obligations must be derived from the objective, acceptance
    criteria, and constraints together.  Reusing only ``goal.objective``
    silently drops output paths or verification requirements that were stored
    in the structured contract during run creation.
    """

    if goal is None:
        return ""
    sections = [str(goal.objective or "")]
    if goal.acceptance_criteria:
        sections.append(
            "Acceptance criteria:\n" + "\n".join(f"- {item}" for item in goal.acceptance_criteria)
        )
    if goal.constraints:
        sections.append(
            "Constraints:\n" + "\n".join(f"- {item}" for item in goal.constraints)
        )
    return "\n\n".join(section for section in sections if section.strip())


def _redact_argument_values(value: Any, *, key: str = "") -> Any:
    """Keep approval context useful without persisting credential material."""

    if key and _SENSITIVE_KEY.search(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_argument_values(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_argument_values(item) for item in value]
    return value


class RunWorker:
    def __init__(
        self,
        store: RunStore,
        kernel: AgentKernel,
        *,
        worker_id: str,
        lease_manager: LeaseManager | None = None,
        resource_manager: ResourceLeaseManager | None = None,
        action_executor: ActionExecutor | None = None,
        contracts: ToolContractRegistry | None = None,
        completion_verifier: CompletionVerifier | None = None,
        message_provider: MessageProvider | None = None,
        available_tools: Sequence[Mapping[str, Any]] = (),
        continue_segments: bool = True,
        capability_runtime: AgentCapabilityRuntime | None = None,
        persist_interactions: bool = True,
        activation_manifest: ActivationManifest | None = None,
        project_instructions: str | None = None,
        project_instruction_sources: Sequence[Mapping[str, Any]] = (),
        child_completion_callback: ChildCompletionCallback | None = None,
        child_cancellation_callback: ChildCancellationCallback | None = None,
        child_failure_callback: ChildFailureCallback | None = None,
        working_directory_resolver: WorkingDirectoryResolver | None = None,
        permission_mode: str = "default",
        action_timeout_seconds: float | None = None,
        model_timeout_seconds: float | None = None,
        stream_emitter: StreamEmitter | None = None,
    ) -> None:
        self.store = store
        self.execution = ExecutionControl(store)
        self.middleware = KernelMiddleware()
        self._action_reservations: dict[str, str] = {}
        self.kernel = kernel
        self.worker_id = worker_id
        self._uses_default_lease_manager = lease_manager is None
        self.lease_manager = lease_manager or LeaseManager(store)
        # The supervisor injects one project-backed manager shared by its
        # workers.  Keeping this optional preserves direct worker/test usage
        # while making durable action conflicts visible across processes.
        self.resource_manager = resource_manager
        self.action_executor = action_executor
        self.contracts = contracts or ToolContractRegistry.first_party()
        self.completion_verifier = completion_verifier
        # Completion is a runtime-owned protocol boundary.  Keep the optional
        # legacy verifier seam, but always run the structural gate first so a
        # model's prose can never transition a run to completed by itself.
        self.completion_gate = CompletionGate()
        self._uses_default_messages = message_provider is None
        self.message_provider = message_provider or self._default_messages
        self.available_tools = tuple(dict(item) for item in available_tools)
        self.continue_segments = continue_segments
        self.capability_runtime = capability_runtime
        self.persist_interactions = persist_interactions
        self.activation_manifest = activation_manifest
        self.project_instructions = project_instructions
        self.project_instruction_sources = tuple(project_instruction_sources)
        self.child_completion_callback = child_completion_callback
        self.child_cancellation_callback = child_cancellation_callback
        self.child_failure_callback = child_failure_callback
        self.working_directory_resolver = working_directory_resolver
        self.permission_mode = normalize_permission_mode(permission_mode)
        self.action_timeout_seconds = _bounded_timeout(
            action_timeout_seconds
            if action_timeout_seconds is not None
            else os.getenv("CC_HARNESS_ACTION_TIMEOUT_SECONDS"),
            default=300.0,
        )
        self.model_timeout_seconds = _bounded_timeout(
            model_timeout_seconds
            if model_timeout_seconds is not None
            else os.getenv("CC_HARNESS_MODEL_TIMEOUT_SECONDS"),
            default=300.0,
        )
        self.stream_emitter = stream_emitter
        self.working_directory: Path = self.store.project_root
        self._active_node_id: str | None = None
        self._event_lock = store.event_append_lock
        self._assessed_verification_sequence = 0
        self._execution_task: asyncio.Task | None = None
        self._cancellation_watchdog_task: asyncio.Task | None = None

    def _requires_approval(self, request: ActionRequest) -> bool:
        """Apply the selected mode to a model action and its durable contract.

        The mode is evaluated in the worker as well as when contracts are
        built. This second check matters for provider adapters that include an
        explicit ``requires_approval`` flag in a tool call: bypass/auto-edit
        must not accidentally fall back to a stale model-side flag, while the
        conservative default still honors it.
        """

        contract = self.contracts.get(request.tool_name)
        return requires_approval_for_mode(
            contract.effect_class,
            self.permission_mode,
            declared=request.requires_approval or contract.requires_approval,
        )

    async def claim(self, run_id: str) -> Lease:
        return await self.lease_manager.claim(run_id, self.worker_id)

    async def execute(self, lease: Lease) -> None:
        if lease.is_expired():
            raise RuntimeError("worker lease expired before execution")
        heartbeat_task: asyncio.Task[None] | None = None
        current_lease = lease
        self.execution.worker_lease = lease
        self._execution_task = asyncio.current_task()
        self._cancellation_watchdog_task = asyncio.create_task(
            self._cancellation_watchdog(lease.run_id, self._execution_task),
            name=f"cc-harness-cancel-watchdog-{lease.run_id}",
        )
        try:
            projection = await self.store.load_projection(lease.run_id)
            if await self.execution.pause_boundary(lease.run_id):
                return
            await self.execution.apply_guidance(lease.run_id)
            projection = await self.store.load_projection(lease.run_id)
            if self.working_directory_resolver is not None:
                self.working_directory = await self.working_directory_resolver(lease.run_id)
            segment = self._next_segment(projection)
            await self._append(current_lease, "RunSegmentStarted", {"segment": segment})
            self._active_node_id = await self._ensure_plan_node_started(current_lease, projection)
            if projection.plan.nodes and self._active_node_id is None:
                page = await self.store.read(lease.run_id, limit=100_000)
                completed = self._completed_plan_node_ids(projection, page.events)
                # A model may legitimately finish the checklist inside a tool
                # call (for example task_plan marks the last step done) and
                # still need one more segment to produce the final answer.
                # Blocking here turned that normal flow into
                # "no dependency-ready plan node"; only a genuinely stuck
                # plan (some nodes still unfinished) is a scheduling block.
                if any(node.node_id not in completed for node in projection.plan.nodes):
                    await self._append(
                        current_lease,
                        "RunBlocked",
                        {"reason": "no dependency-ready plan node"},
                    )
                    return
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(current_lease),
                name=f"cc-harness-heartbeat-{lease.run_id}",
            )

            projection = await self.store.load_projection(lease.run_id)
            if projection.status is RunStatus.CANCEL_REQUESTED:
                await self._acknowledge_cancellation(current_lease, "cancel requested")
                return

            # Approval is a durable pause. Once granted, resume the exact
            # planned action from its redacted argument artifact.
            had_action = False
            had_progress = False
            approved = await self._execute_granted_actions(current_lease, projection)
            had_action = approved
            # A rejected approval is itself a terminal action outcome.  Do
            # not execute the planned action during generic recovery; write a
            # cancelled observation first so the next model invocation has a
            # provider-valid tool result and can continue with another plan.
            await self._record_rejected_actions(
                current_lease,
                await self.store.load_projection(lease.run_id),
            )
            recovered_action, recovery_blocked = await self._recover_inflight_actions(
                current_lease,
                await self.store.load_projection(lease.run_id),
            )
            had_action = had_action or recovered_action
            if recovery_blocked:
                # A recovered child must not disappear behind a blocked
                # projection.  Surface the durable failure to the parent so
                # the parent can re-plan or replace that child instead of
                # waiting forever on a child that can no longer progress.
                if self.child_failure_callback is not None:
                    with contextlib.suppress(Exception):
                        await self.child_failure_callback(
                            lease.run_id,
                            "in-flight action recovery blocked; reconciliation required",
                        )
                return

            # A Segment is a recoverable interaction interval, not one model
            # call.  It ends at a plan-node/approval/cancellation/completion
            # boundary or when the model returns no verifiable next action.
            verification_repairs = 0
            while True:
                projection = await self.store.load_projection(lease.run_id)
                if await self.execution.pause_boundary(lease.run_id):
                    return
                await self.execution.apply_guidance(lease.run_id)
                projection = await self.store.load_projection(lease.run_id)
                if self._active_node_id not in projection.plan.by_id:
                    self._active_node_id = await self._ensure_plan_node_started(current_lease, projection)
                if (projection.task.plan_approval_required
                        and projection.discovery_status != "awaiting"
                        and projection.task.approved_plan_digest != projection.plan.digest):
                    await self.execution.wait(lease.run_id, kind="waiting_approval",
                                              reason="plan_requires_approval")
                    return
                await self._kernel_phase(lease.run_id, "prepare", "before")
                if projection.status is RunStatus.CANCEL_REQUESTED:
                    await self._acknowledge_cancellation(current_lease, "cancel requested")
                    return
                if self.capability_runtime is not None:
                    validate_goal = getattr(self.capability_runtime, "validate_goal", None)
                    if validate_goal is not None:
                        goal_allowed, goal_reason = await validate_goal(projection)
                        self._trigger_activation(
                            "safety",
                            run_id=lease.run_id,
                            stage="goal_validation",
                            allowed=bool(goal_allowed),
                            reason=str(goal_reason),
                        )
                        if not goal_allowed:
                            await self._append(
                                current_lease,
                                "RunBlocked",
                                {
                                    "reason": "l2_input_security_block",
                                    "detail": goal_reason,
                                },
                            )
                            return
                context_manifest_artifact: str | None = None
                context_build = None
                if projection.skill_snapshot is None and self.capability_runtime is not None:
                    local_skills = getattr(self.capability_runtime, "local_skills", None)
                    if callable(local_skills):
                        snapshot, info = await asyncio.to_thread(local_skills().snapshot, lease.run_id)
                        await self._append(current_lease, "SkillSnapshotPinned", {
                            "snapshot": snapshot, "revision": info["revision"], "reason": "initial"}, artifact_refs=(snapshot,))
                        projection = await self.store.load_projection(lease.run_id)
                messages = await self._messages_for_projection(
                    projection,
                    include_interactions=self.capability_runtime is None,
                )
                invocation_tools = self.available_tools
                completion_assessment = False
                ws = projection.working_state
                if (self.capability_runtime is not None and projection.goal is not None
                    and projection.goal.interaction_mode == "coding" and not _is_benchmark_task()
                    and ws.last_verification_ok is True and ws.last_verification_sequence >= ws.last_mutation_sequence
                    and ws.last_verification_sequence > self._assessed_verification_sequence
                    and not ws.unresolved_errors and not projection.task.pause_requested):
                    contract = completion_contract_from_instruction(_goal_public_text(projection.goal))
                    if contract.required_artifacts and await self._durable_contract_satisfied(lease.run_id, projection, contract):
                        proposal = await self._synthesize_completion_candidate(current_lease, projection)
                        if proposal is not None:
                            # The model needs a bounded request template, not
                            # the entire receipt ledger. The final gate always
                            # reconstructs the full proof from durable events.
                            tests = tuple(e for e in proposal.evidence if e.kind == EvidenceKind.TEST)
                            proposal = replace(proposal, evidence=(tests[-1:] or proposal.evidence[-1:]))
                            completion_assessment = True
                            self._assessed_verification_sequence = ws.last_verification_sequence
                            invocation_tools = ()
                            messages = (*messages, {"role": "user", "_context_mandatory": True, "_cc_harness_untrusted": True,
                                "content": "Runtime completion review: required local outputs exist and current verification receipts are available. "
                                "Compare ALL current goal criteria against the committed facts. No tools are available in this review. "
                                "If satisfied, give a concise final summary and emit the following completion proposal unchanged. "
                                "If any criterion is missing, describe that concrete gap instead; do not claim completion or restart exploration. "
                                "This request grants no permissions.\n<cc-harness-complete>" + json.dumps(proposal.to_dict(), ensure_ascii=False) + "</cc-harness-complete>"})
                if self.capability_runtime is not None:
                    context_build = await self.capability_runtime.build_context(
                        projection,
                        messages,
                        invocation_tools,
                        # Automatic memory injection is an L3 persona snapshot;
                        # explicit memory_recall progressively searches L3→L0.
                        query="",
                    )
                    messages = context_build.messages
                    context_manifest_artifact = context_build.manifest_artifact
                    await self._append(
                        current_lease,
                        "ContextProjectionBuilt",
                        {
                            "projection_id": f"context-{lease.run_id}-{segment}",
                            "source_message_count": context_build.source_message_count,
                            "projected_message_count": context_build.projected_message_count,
                            "source_digest": context_build.source_digest,
                            "projection_digest": context_build.projection_digest,
                            "segment": segment,
                            "round": len(projection.actions),
                            "compaction_tier": int(context_build.compaction.tier),
                            "compaction": {
                                "tier": context_build.compaction.tier.name.lower(),
                                "before_tokens": context_build.compaction.before_tokens,
                                "after_tokens": context_build.compaction.after_tokens,
                                "ratio_before": context_build.compaction.ratio_before,
                                "ratio_after": context_build.compaction.ratio_after,
                                "summarized": context_build.compaction.summarized,
                                "applied": context_build.compaction.applied,
                                "error": context_build.compaction.error,
                            },
                            "effective_context_window": context_build.effective_context_window,
                            "provider_safety_factor": context_build.provider_safety_factor,
                            "manifest_artifact": context_build.manifest_artifact,
                            "coverage": dict(context_build.coverage),
                        },
                        artifact_refs=(
                            (context_build.manifest_artifact,)
                            if context_build.manifest_artifact
                            else ()
                        ),
                    )
                    self._trigger_activation(
                        "context",
                        run_id=lease.run_id,
                        projection_digest=context_build.projection_digest,
                    )
                    # Memory is a projection capability, so it may be used
                    # even when the current lookup is empty or a persisted
                    # snapshot is reused.  Record the attempt at the same
                    # durable boundary as context activation; this keeps the
                    # UI/activation manifest honest instead of reporting
                    # ``triggered=false`` until a later save checkpoint.
                    context_coverage = dict(context_build.coverage)
                    if context_coverage.get("memory_enabled"):
                        self._trigger_activation(
                            "memory",
                            run_id=lease.run_id,
                            stage="context_projection",
                            injected=bool(context_build.recalled),
                            snapshot_reused=bool(
                                context_coverage.get("memory_snapshot_reused")
                            ),
                            injection_mode=context_coverage.get(
                                "memory_injection_mode", "disabled"
                            ),
                        )
                    context_overflow = bool(
                        self.capability_runtime.context_config.enabled
                        and (
                            context_build.compaction.ratio_after > 1.0
                            or (
                                context_build.effective_context_window is not None
                                and context_build.compaction.after_tokens
                                > context_build.effective_context_window
                            )
                        )
                    )
                    compaction_error = context_build.compaction.error
                    if context_coverage.get("integrity_errors"):
                        await self.execution.wait(lease.run_id, kind="waiting_input",
                                                  reason="context_integrity: " + "; ".join(context_coverage["integrity_errors"]))
                        return
                    if context_overflow and not compaction_error:
                        compaction_error = "mandatory context overflow after compaction"
                    if context_overflow:
                        await self.execution.wait(lease.run_id, kind="waiting_input",
                                                  reason="context budget still exceeded after bounded rebuild")
                        return
                    if (
                        context_build.compaction.applied
                        or compaction_error
                    ):
                        await self._append(
                            current_lease,
                            "ContextCompacted",
                            {
                                "projection_id": f"context-{lease.run_id}-{segment}",
                                "tier": context_build.compaction.tier.name.lower(),
                                "source_digest": context_build.source_digest,
                                "error": compaction_error,
                                "before_tokens": context_build.compaction.before_tokens,
                                "after_tokens": context_build.compaction.after_tokens,
                                "ratio_before": context_build.compaction.ratio_before,
                                "ratio_after": context_build.compaction.ratio_after,
                                "applied": context_build.compaction.applied,
                                "effective_context_window": context_build.effective_context_window,
                                "provider_safety_factor": context_build.provider_safety_factor,
                            },
                            artifact_refs=(
                                (context_build.compaction_artifact,)
                                if context_build.compaction_artifact
                                else ()
                            ),
                        )
                    if compaction_error and self.capability_runtime.context_config.fail_closed:
                        await self._append(
                            current_lease,
                            "RunBlocked",
                            {
                                "reason": "context projection failed",
                                "error": compaction_error,
                            },
                        )
                        return
                generation_reader = getattr(self.capability_runtime, "memory_generation", None)
                if callable(generation_reader) and context_build is not None:
                    if context_build.coverage.get("memory_generation") != await generation_reader():
                        continue  # rebuild at the next model boundary after a concurrent forget
                round_index = len(projection.actions)
                await self._kernel_phase(lease.run_id, "prepare", "after")
                await self._kernel_phase(lease.run_id, "model", "before")
                # ``segment``/``round`` describe the logical position but are
                # not unique across a durable resume: a stalled no-action
                # turn can be resumed with the same pair.  A fresh nonce is
                # therefore part of every invocation identity so usage
                # aggregation, WebUI latest-call telemetry, and audit joins
                # never collapse two real provider requests into one.
                invocation_id = f"model-{lease.run_id}-{uuid.uuid4().hex}"
                model_amounts = await self._model_budget(projection, messages)
                model_reservation = await self.execution.reserve(lease.run_id, model_amounts, key=invocation_id)
                await self._append(
                    current_lease,
                    "ModelInvocationStarted",
                    {
                        "invocation_id": invocation_id,
                        "segment": segment,
                        "round": round_index,
                        "context_manifest_artifact": context_manifest_artifact,
                        "purpose": "completion_review" if completion_assessment else "execution",
                    },
                    artifact_refs=(
                        (context_manifest_artifact,)
                        if context_manifest_artifact
                        else ()
                    ),
                )
                self._trigger_activation(
                    "agent_loop",
                    run_id=lease.run_id,
                    segment=segment,
                    round=round_index,
                )
                context = SegmentContext(
                    run_id=lease.run_id,
                    projection=projection,
                    messages=tuple(dict(item) for item in messages),
                    available_tools=invocation_tools,
                    working_state=projection.working_state.to_dict(),
                    lease_epoch=current_lease.epoch,
                    worker_id=self.worker_id,
                    cancellation_requested=projection.status is RunStatus.CANCEL_REQUESTED,
                )
                invocation_started = time.monotonic()
                invocation_status = "failed"
                invocation_error: str | None = None
                invocation_error_details: dict[str, Any] | None = None
                outcome = None
                from .llm import task_output_token_limit
                output_limit_token = task_output_token_limit.set(
                    getattr(getattr(self.kernel, "model", None), "budget_output_tokens", None)
                    if "tokens" in model_amounts or "cost" in model_amounts else None
                )
                try:
                    outcome = await self._execute_model_segment(
                        context,
                        segment=segment,
                        invocation_id=invocation_id,
                    )
                    invocation_status = "succeeded"
                except asyncio.CancelledError:
                    invocation_status = "cancelled"
                    invocation_error = "模型调用已取消"
                    raise
                except Exception as exc:
                    details, invocation_error_details = _worker_failure_contract(
                        exc,
                        fallback_code="runtime_error",
                        fallback_phase="model",
                        fallback_message="Runtime 未能完成本轮响应",
                    )
                    # Keep a concrete, bounded local diagnostic in the durable
                    # usage fact. The browser projection exposes only the
                    # separately stored structured error contract.
                    invocation_error = f"{type(exc).__name__}: {details.code}"[:240]
                    raise
                finally:
                    task_output_token_limit.reset(output_limit_token)
                    usage = dict(getattr(outcome, "usage", {}) or {})
                    token_count = usage.get("total_tokens")
                    if token_count is None:
                        token_count = (usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0) + (
                            usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
                        )
                    actual = {"steps": 1, "model_calls": 1,
                              "active_seconds": max(0, time.monotonic() - invocation_started)}
                    if token_count:
                        actual["tokens"] = token_count
                    elif "tokens" in model_amounts:
                        # Missing provider usage is not zero. Retain the reservation
                        # conservatively rather than allowing unlimited unmetered calls.
                        actual["tokens"] = model_amounts["tokens"]
                    if usage.get("reported_cost") is not None:
                        actual["cost"] = usage["reported_cost"]
                    elif "cost" in model_amounts:
                        actual["cost"] = model_amounts["cost"]
                    estimated = [key for key in ("tokens", "cost") if key in actual and (
                        (key == "tokens" and not token_count) or (key == "cost" and usage.get("reported_cost") is None)
                    )]
                    await asyncio.shield(self.execution.settle(
                        lease.run_id, model_reservation, actual, estimated=estimated,
                    ))
                    if self.capability_runtime is not None and context_build is not None:
                        record_usage = getattr(
                            self.capability_runtime, "record_provider_usage", None
                        )
                        if record_usage is not None:
                            with contextlib.suppress(Exception):
                                record_usage(
                                    getattr(outcome, "usage", None),
                                    local_input_tokens=context_build.compaction.after_tokens,
                                )
                    # Persist a terminal invocation fact even when the
                    # provider stream fails before an assistant message can
                    # be committed.  Usage reports consume these facts.
                    finished_payload: dict[str, Any] = {
                        "invocation_id": invocation_id,
                        "status": invocation_status,
                        "duration_ms": round((time.monotonic() - invocation_started) * 1000, 3),
                        "usage": dict(getattr(outcome, "usage", {}) or {}),
                    }
                    if outcome is not None:
                        finished_payload["stop_reason"] = str(
                            getattr(outcome, "stop_reason", "") or "model_stop"
                        )
                        finished_payload["provider_metadata"] = dict(
                            getattr(outcome, "provider_metadata", None)
                            or (getattr(outcome, "usage", {}) or {}).get("provider_metadata")
                            or {}
                        )
                    if invocation_error:
                        finished_payload["error"] = invocation_error
                    if invocation_error_details is not None:
                        finished_payload["error_details"] = invocation_error_details
                    with contextlib.suppress(Exception):
                        await self._append_runtime_event(
                            lease.run_id,
                            "ModelInvocationFinished",
                            finished_payload,
                        )
                after_model = await self.store.load_projection(lease.run_id)
                await self._kernel_phase(lease.run_id, "model", "after")
                if await self.execution.pause_boundary(lease.run_id):
                    return
                if after_model.task.goal_revision != projection.task.goal_revision:
                    # These calls were never dispatched. Discard the stale proposal;
                    # the next Prepare rebuilds from the new authoritative goal.
                    continue
                if after_model.status in {RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLED}:
                    await self._append(
                        current_lease,
                        "AssistantMessageInterrupted",
                        {
                            "segment": segment,
                            "round": round_index,
                            "reason": "cancel requested during model segment",
                        },
                    )
                    await self._acknowledge_cancellation(
                        current_lease,
                        "cancel requested during model segment",
                    )
                    return
                protect_output = getattr(self.capability_runtime, "protect_model_output", None)
                if protect_output is not None:
                    safe_text, _security_details = protect_output(outcome.model_text, messages)
                    outcome = replace(outcome, model_text=safe_text)
                refreshed_requests = self._refresh_completed_reads(projection, outcome.action_requests)
                if refreshed_requests != outcome.action_requests:
                    outcome = replace(outcome, action_requests=refreshed_requests)
                message_requests = outcome.action_requests
                for request_index, request in enumerate(outcome.action_requests):
                    if self._requires_approval(request):
                        # Provider protocols require one tool result for every
                        # persisted call. Calls behind this approval boundary
                        # are replanned after the approved result is committed.
                        message_requests = (outcome.action_requests[request_index],)
                        break
                if outcome.action_requests and not any(self._requires_approval(r) for r in outcome.action_requests):
                    # Reserve the entire proposal before persisting provider tool calls.
                    # An exhausted budget must not leave unmatched assistant calls.
                    for request in outcome.action_requests:
                        effect = self.contracts.get(request.tool_name).effect_class
                        amounts = {"tool_calls": 1}
                        if effect in {EffectClass.UNKNOWN, EffectClass.EXTERNAL_SIDE_EFFECT}:
                            amounts["risk_actions"] = 1
                        self._action_reservations[request.action_id] = await self.execution.reserve(
                            lease.run_id, amounts,
                        )
                await self._commit_assistant_message(
                    current_lease,
                    segment,
                    round_index,
                    outcome,
                    tool_requests=message_requests,
                )

                if completion_assessment and (outcome.completion_candidate is None or outcome.action_requests):
                    for request in outcome.action_requests:
                        reservation = self._action_reservations.pop(request.action_id, None)
                        if reservation is not None:
                            await self.execution.settle(lease.run_id, reservation, {})
                    await self.execution.append(lease.run_id, "TaskVerificationFinished", {"passed": False,
                        "issues": [outcome.model_text[:2000] or "Completion review did not submit a valid proposal; continue with the concrete missing acceptance checks."]})
                    continue

                if not outcome.action_requests:
                    if outcome.progress is not None:
                        await self._append(
                            current_lease,
                            "ProgressRecorded",
                            {"progress": outcome.progress.to_dict()},
                        )
                        had_progress = True
                    if await self._try_accept_completion(current_lease, outcome.completion_candidate):
                        await self._commit_memory_checkpoint(
                            current_lease,
                            segment=segment,
                            committed_progress=True,
                        )
                        return
                    if await self._active_node_completed(lease.run_id):
                        had_progress = True
                    if ((outcome.completion_candidate is not None or (self.capability_runtime is not None and not _is_benchmark_task() and outcome.model_text.strip())) and verification_repairs < 2
                            and not await self._active_node_completed(lease.run_id)):
                        verification_repairs += 1
                        await self.execution.append(lease.run_id, "TaskVerificationFinished", {
                            "passed": False, "issues": await self._completion_repair_issues(lease.run_id),
                            "repair_attempt": verification_repairs,
                        })
                        continue
                    if outcome.progress is not None:
                        await self._commit_memory_checkpoint(
                            current_lease,
                            segment=segment,
                            committed_progress=True,
                        )
                    break

                requests, duplicate_inflight = self._deduplicate_action_requests(
                    projection, outcome.action_requests
                )
                if not requests:
                    if duplicate_inflight:
                        await self._append(
                            current_lease,
                            "RunBlocked",
                            {
                                "reason": "duplicate action requires durable reconciliation",
                                "action_ids": [request.action_id for request in outcome.action_requests],
                            },
                        )
                        return
                    if outcome.progress is not None:
                        await self._append(
                            current_lease,
                            "ProgressRecorded",
                            {"progress": outcome.progress.to_dict()},
                        )
                        had_progress = True
                    if await self._try_accept_completion(current_lease, outcome.completion_candidate):
                        await self._commit_memory_checkpoint(
                            current_lease,
                            segment=segment,
                            committed_progress=True,
                        )
                        return
                    if await self._active_node_completed(lease.run_id):
                        had_progress = True
                    if outcome.progress is not None:
                        await self._commit_memory_checkpoint(
                            current_lease,
                            segment=segment,
                            committed_progress=True,
                        )
                    if self.capability_runtime is not None and verification_repairs < 2 and not _is_benchmark_task():
                        verification_repairs += 1
                        await self.execution.append(lease.run_id, "TaskVerificationFinished", {
                            "passed": False, "repair_attempt": verification_repairs,
                            "issues": ["The requested identical actions already have durable terminal receipts and were not executed again. "
                                       "Reuse successful results; repair inputs for failed actions. Do not repeat the same proposal without a changed input. "
                                       "Continue toward the missing delivery/verification checks."] + await self._completion_repair_issues(lease.run_id)})
                        continue
                    break
                for request in requests:
                    if self._requires_approval(request):
                        contract = self.contracts.get(request.tool_name)
                        await self._plan_action(current_lease, request, contract.effect_class, contract.digest)
                        await self._append(
                            current_lease,
                            "ApprovalRequested",
                            {
                                "approval_id": f"approval-{request.action_id}",
                                "action_id": request.action_id,
                                "action_args_digest": request.normalized_args_digest,
                                "scope": [request.tool_name],
                            },
                        )
                        return
                had_action = True
                statuses = await self._execute_request_batch(current_lease, requests)
                await self._kernel_phase(lease.run_id, "observe", "after")
                if await self.execution.pause_boundary(lease.run_id):
                    return
                latest_task = await self.store.load_projection(lease.run_id)
                if latest_task.task.wait.get("requested"):
                    wait = latest_task.task.wait
                    await self.execution.wait(lease.run_id, kind=wait["kind"], reason=wait["reason"],
                                              wait_id=wait["wait_id"], timeout_at=wait.get("timeout_at"))
                    return
                if latest_task.task.goal_revision != projection.task.goal_revision:
                    continue
                cancellation_state = await self.store.load_projection(lease.run_id)
                if cancellation_state.status is RunStatus.CANCEL_REQUESTED:
                    await self._acknowledge_cancellation(current_lease, "cancel requested after action boundary")
                    return
                if cancellation_state.status is RunStatus.CANCELLED:
                    return
                for request, result_status in zip(requests, statuses, strict=True):
                    if result_status is ActionStatus.OUTCOME_UNKNOWN:
                        await self._append(
                            current_lease,
                            "RunBlocked",
                            {"reason": "action outcome is unknown", "action_id": request.action_id},
                        )
                        return

                if outcome.progress is not None:
                    await self._append(
                        current_lease,
                        "ProgressRecorded",
                        {"progress": outcome.progress.to_dict()},
                    )
                    had_progress = True
                if outcome.completion_candidate is not None and await self._try_accept_completion(current_lease, outcome.completion_candidate):
                    await self._commit_memory_checkpoint(
                        current_lease,
                        segment=segment,
                        committed_progress=True,
                    )
                    return
                await self._commit_memory_checkpoint(
                    current_lease,
                    segment=segment,
                    committed_progress=True,
                )
                if self._active_node_id:
                    current = await self.store.load_projection(lease.run_id)
                    if any(
                        todo.todo_id == self._active_node_id
                        and todo.status in {"done", "completed"}
                        for todo in current.todos
                    ):
                        break

            await self._finish_segment(
                current_lease,
                segment,
                had_action=had_action,
                had_progress=had_progress,
            )
        except BudgetExhausted:
            for key in tuple(self._action_reservations.values()):
                await self.execution.settle(lease.run_id, key, {})
            self._action_reservations.clear()
            await self.execution.pause_boundary(lease.run_id)
            return
        except asyncio.CancelledError:
            # Ctrl+C/cancel is delivered by the watchdog as soon as the client
            # appends InterruptRequested.  Before acknowledging it, close every
            # started action conservatively: a started effect without a durable
            # observation is *unknown*, never silently successful.
            with contextlib.suppress(Exception):
                projection = await self.store.load_projection(lease.run_id)
                if projection.task.pause_requested:
                    await self._finalize_interruption(current_lease, projection.task.reason, pause=True)
                    return
                if projection.status in {RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLED}:
                    await self._finalize_interruption(current_lease, "cancel requested")
                    return
            raise
        except LeaseFenceError:
            # A supervisor may reclaim an expired lease while a provider or
            # subprocess is still unwinding.  The old worker is intentionally
            # fenced and must exit quietly; emitting RunFailed here would race
            # the replacement worker and turn a recoverable hand-off into a
            # false Runtime/Harbor infrastructure failure.
            return
        except ProviderProtocolError as exc:
            # Protocol failures are deterministic and must be visible in the
            # durable stream.  Never retry by fabricating provider fields or
            # spending another model call on the same malformed replay.
            details, error_payload = _worker_failure_contract(
                exc,
                fallback_code="provider_protocol_error",
                fallback_phase="model",
                fallback_message="模型返回的数据格式无法安全重放",
            )
            failure_payload = {
                "reason": f"{exc.code}: {str(exc)[:600]}",
                "error": error_payload,
                "target_status": RunStatus.FAILED_RECOVERABLE.value,
            }
            with contextlib.suppress(Exception):
                await self._append(
                    current_lease,
                    "RunFailed",
                    failure_payload,
                )
            if self.child_failure_callback is not None:
                with contextlib.suppress(Exception):
                    await self.child_failure_callback(
                        current_lease.run_id,
                        f"{details.code}: {details.message}",
                    )
            raise
        except Exception as exc:
            # A worker exception must become durable state before its lease is
            # released.  Otherwise the supervisor has neither an active task
            # nor an expired lease to reclaim and the run appears to hang in
            # RUNNING forever.
            details, error_payload = _worker_failure_contract(
                exc,
                fallback_code="runtime_error",
                fallback_phase="model",
                fallback_message="Runtime 未能完成本轮响应",
            )
            failure_payload = {
                "reason": (
                    f"unhandled worker exception: {type(exc).__name__}: "
                    f"{str(exc)[:300]} | "
                    f"{traceback.format_exc(limit=4).replace(chr(10), ' ')[:900]}"
                ),
                "error": error_payload,
                "target_status": RunStatus.FAILED_RECOVERABLE.value,
            }
            with contextlib.suppress(Exception):
                await self._append(
                    current_lease,
                    "RunFailed",
                    failure_payload,
                )
            if self.child_failure_callback is not None:
                with contextlib.suppress(Exception):
                    await self.child_failure_callback(
                        lease.run_id,
                        f"{details.code}: {details.message}",
                    )
            raise
        finally:
            for key in tuple(self._action_reservations.values()):
                with contextlib.suppress(Exception):
                    await self.execution.settle(lease.run_id, key, {})
            self._action_reservations.clear()
            if self._cancellation_watchdog_task is not None:
                self._cancellation_watchdog_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._cancellation_watchdog_task
                self._cancellation_watchdog_task = None
            self._execution_task = None
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            # Runtime shutdown can race with a provider cancellation.  The
            # supervisor normally awaits workers before closing the store, but
            # a process-level shutdown may still reach this finally block
            # after the store has been closed.  Lease release is cleanup, not
            # a reason to surface a second unhandled failure.
            with contextlib.suppress(RunStoreError, ValueError):
                await self.lease_manager.release(current_lease)

    async def heartbeat(self, lease: Lease) -> Lease:
        async with self._event_lock:
            return await self.lease_manager.heartbeat(lease)

    async def release(self, lease: Lease) -> bool:
        return await self.lease_manager.release(lease)

    async def _heartbeat_loop(self, lease: Lease) -> None:
        current = lease
        interval = max(0.25, self.lease_manager.ttl_seconds / 3.0)
        while True:
            await asyncio.sleep(interval)
            try:
                current = await self.heartbeat(current)
            except Exception:  # noqa: BLE001 - the owning segment will be fenced on its next write
                return

    async def _cancellation_watchdog(
        self, run_id: str, execution_task: asyncio.Task | None
    ) -> None:
        """Turn a durable cancellation request into prompt task cancellation.

        The coordinator intentionally writes InterruptRequested first so an
        in-flight effect remains auditable.  Polling the authoritative
        projection avoids waiting for a lease TTL and works for provider calls,
        subprocesses, and custom action executors alike.
        """

        if execution_task is None:
            return
        while not execution_task.done():
            await asyncio.sleep(0.05)
            try:
                projection = await self.store.load_projection(run_id)
            except Exception:
                continue
            if projection.status in {RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLED}:
                if not execution_task.done():
                    execution_task.cancel()
                return

    async def _execute_model_segment(
        self,
        context: SegmentContext,
        *,
        segment: int | None = None,
        invocation_id: str | None = None,
    ):
        """Run one provider segment behind a bounded, cancellation-safe task."""

        chunk = 0
        terminal_emitted = False
        stream_started = False
        pending_content: list[str] = []
        pending_chars = 0
        emit_lock = asyncio.Lock()
        flush_task: asyncio.Task[None] | None = None
        # A small server-side coalescing window keeps token-heavy providers
        # from turning the browser into a render loop while preserving strict
        # ordering around tool/error/done boundaries.
        stream_flush_seconds = 0.04
        stream_max_chars = 4096

        async def publish(payload: Mapping[str, Any]) -> None:
            """Publish one already-ordered envelope to the best-effort sink."""

            nonlocal chunk
            if self.stream_emitter is None:
                return
            chunk += 1
            envelope = {
                "type": "stream_delta",
                "run_id": context.run_id,
                "segment": segment if segment is not None else 0,
                "chunk": chunk,
                "invocation_id": invocation_id,
                **dict(payload),
            }
            # A disconnected browser or a full in-memory queue must never
            # turn a successful provider call into a failed Durable segment.
            try:
                await self.stream_emitter(envelope)
            except Exception:
                return

        async def flush_content_locked() -> None:
            nonlocal pending_chars
            if not pending_content:
                return
            text = "".join(pending_content)
            pending_content.clear()
            pending_chars = 0
            await publish({"kind": "content", "text": text})

        async def delayed_flush() -> None:
            try:
                await asyncio.sleep(stream_flush_seconds)
                async with emit_lock:
                    await flush_content_locked()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The stream sink is presentation-only.  A timer failure must
                # never affect the provider/worker result.
                return

        async def stop_flush_task() -> None:
            """Cancel the coalescing timer once a segment reaches a boundary."""

            nonlocal flush_task
            task = flush_task
            flush_task = None
            if task is None or task.done() or task is asyncio.current_task():
                return
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        async def emit_stream(payload: Mapping[str, Any]) -> None:
            """Best-effort bridge from a provider chunk to the WebUI hub."""

            nonlocal terminal_emitted, stream_started, flush_task, pending_chars
            if self.stream_emitter is None or terminal_emitted:
                return
            kind = str(payload.get("kind") or "")
            content_text = str(payload.get("text") or "") if kind == "content" else ""
            if content_text:
                stream_started = True
                async with emit_lock:
                    pending_content.append(content_text)
                    pending_chars += len(content_text)
                    if pending_chars >= stream_max_chars:
                        await flush_content_locked()
                    elif flush_task is None or flush_task.done():
                        flush_task = asyncio.create_task(
                            delayed_flush(),
                            name=f"cc-harness-stream-flush-{context.run_id}",
                        )
                return

            # Tool deltas and terminal markers are hard ordering boundaries:
            # flush any buffered text before publishing them.
            async with emit_lock:
                await flush_content_locked()
                if kind in {"reasoning", "tool_call_delta"}:
                    stream_started = True
                if kind == "done":
                    terminal_emitted = True
                await publish(payload)

        async def emit_terminal(
            reason: str,
            *,
            error_details: StreamErrorDetails | None = None,
        ) -> None:
            """Emit a terminal presentation marker without affecting the worker."""

            nonlocal terminal_emitted
            if terminal_emitted:
                return
            with contextlib.suppress(BaseException):
                if error_details is not None:
                    terminal_emitted = True
                    async with emit_lock:
                        await flush_content_locked()
                        await publish(
                            {
                                "type": "stream_error",
                                "kind": "error",
                                "finish_reason": reason,
                                "error": error_details.to_dict(),
                            }
                        )
                    return
                payload: dict[str, Any] = {"kind": "done", "finish_reason": reason}
                await emit_stream(payload)

        async def execute_kernel() -> Any:
            execute = self.kernel.execute_segment
            supports_callback = False
            try:
                signature = inspect.signature(execute)
                supports_callback = (
                    "stream_callback" in signature.parameters
                    or any(
                        parameter.kind is inspect.Parameter.VAR_KEYWORD
                        for parameter in signature.parameters.values()
                    )
                )
            except (TypeError, ValueError):
                supports_callback = False
            if self.stream_emitter is not None and supports_callback:
                return await execute(context, stream_callback=emit_stream)
            return await execute(context)

        model_timeout = self.model_timeout_seconds
        remaining_time = (await self.execution.remaining_time(context.run_id)
                          if getattr(self.execution, "worker_lease", None) is not None else None)
        if remaining_time is not None:
            model_timeout = max(0.001, min(model_timeout or remaining_time, remaining_time))
        if model_timeout == 0:
            try:
                result = await execute_kernel()
            except asyncio.CancelledError:
                await emit_terminal("cancelled")
                await stop_flush_task()
                raise
            except Exception as exc:
                details = classify_stream_error(exc, partial_output=stream_started)
                with contextlib.suppress(Exception):
                    setattr(exc, "stream_error", details)
                await emit_terminal("error", error_details=details)
                await stop_flush_task()
                raise
            await emit_terminal(str(getattr(result, "stop_reason", "model_stop")))
            await stop_flush_task()
            return result
        task = asyncio.create_task(
            execute_kernel(),
            name=f"cc-harness-model-{context.run_id}-{context.lease_epoch}",
        )
        try:
            result = await asyncio.wait_for(asyncio.shield(task), model_timeout)
            await emit_terminal(str(getattr(result, "stop_reason", "model_stop")))
            await stop_flush_task()
            return result
        except asyncio.TimeoutError as exc:
            # Give cooperative adapters a short opportunity to close their HTTP
            # stream.  A non-cooperative provider is intentionally left as an
            # orphan task: the durable worker is failed/recoverable and will
            # never replay its model result as an action without a new segment.
            timeout_error = ModelInvocationTimeout(
                f"model segment timed out after {model_timeout:.3f}s"
            )
            details = classify_stream_error(timeout_error, partial_output=stream_started)
            with contextlib.suppress(Exception):
                setattr(timeout_error, "stream_error", details)
            # Publish the structured timeout before cancelling the kernel: a
            # cooperative provider may emit its own cancellation terminal
            # while unwinding, which would otherwise preempt this diagnosis.
            await emit_terminal("timeout", error_details=details)
            await _cancel_task_bounded(task)
            await stop_flush_task()
            if remaining_time is not None and (
                self.model_timeout_seconds == 0 or remaining_time <= self.model_timeout_seconds
            ):
                await self.execution.pause(context.run_id, "budget_exhausted: active_seconds")
                raise BudgetExhausted("active_seconds") from exc
            raise timeout_error from exc
        except asyncio.CancelledError:
            await _cancel_task_bounded(task)
            await emit_terminal("cancelled")
            await stop_flush_task()
            raise
        except Exception as exc:
            details = classify_stream_error(exc, partial_output=stream_started)
            with contextlib.suppress(Exception):
                setattr(exc, "stream_error", details)
            await emit_terminal("error", error_details=details)
            await stop_flush_task()
            raise

    async def _finalize_interruption(self, lease: Lease, reason: str, *, pause: bool = False) -> None:
        """Persist safe cancellation outcomes before ``RunCancelled``."""

        projection = await self.store.load_projection(lease.run_id)
        for action in projection.actions:
            if action.status in {ActionStatus.PLANNED, ActionStatus.PREPARED}:
                await self._append(
                    lease,
                    "ActionCancelled",
                    {
                        "action_id": action.action_id,
                        "attempt": action.attempt,
                        "cancellation_reason": reason,
                    },
                )
                continue
            if action.status is not ActionStatus.STARTED:
                continue
            observation, observation_artifact = await self._find_action_observation(
                lease.run_id, action.action_id, action.attempt
            )
            if observation is None:
                effect = action.effect_class.value if isinstance(action.effect_class, EffectClass) else str(action.effect_class)
                contract = self.contracts.get(action.tool_name)
                safe_read = contract.effect_class == EffectClass.READ_ONLY and contract.idempotent
                observation = make_observation(
                    action_id=action.action_id,
                    attempt=action.attempt,
                    tool_name=action.tool_name,
                    status="cancelled" if safe_read else "unknown",
                    effect_class=effect,
                    text="read-only action interrupted; safe to read again" if safe_read else "action interrupted before its external outcome was durable",
                    error_kind="interrupted_before_observation",
                    recovery="retry_safe_read" if safe_read else "reconcile",
                    provenance=("durable-cancellation", "watchdog"),
                    metadata={"requires_reconciliation": not safe_read, "interrupted": True},
                )
                observation_ref = self.store.artifacts.put_text(
                    json.dumps(
                        observation.to_dict(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    media_type="application/json; purpose=tool-observation-cancellation",
                )
                observation_artifact = observation_ref.digest
                await self._append_observation_event(lease, observation, observation_artifact)
            await self._append_recovered_terminal(
                lease, action, observation, observation_artifact
            )
        if pause:
            await self.execution.pause_boundary(lease.run_id)
        else:
            await self._acknowledge_cancellation(lease, reason)

    async def _execute_granted_actions(self, lease: Lease, projection: RunProjection) -> bool:
        granted = {item.action_id for item in projection.approvals if item.status.value == "granted"}
        actions = [
            item
            for item in projection.actions
            if item.action_id in granted and item.status is ActionStatus.PLANNED
        ]
        for action in actions:
            if not action.arguments_artifact:
                await self._append(
                    lease,
                    "ActionOutcomeUnknown",
                    {
                        "action_id": action.action_id,
                        "attempt": action.attempt,
                        "reason": "approved action arguments are unavailable",
                    },
                )
                await self._append(
                    lease,
                    "RunBlocked",
                    {"reason": "approved action cannot be reconstructed"},
                )
                return True
            try:
                arguments = json.loads(self.store.artifacts.read_text(action.arguments_artifact))
            except (OSError, ValueError, TypeError, KeyError):
                arguments = None
            if not isinstance(arguments, dict):
                await self._append(
                    lease,
                    "ActionOutcomeUnknown",
                    {
                        "action_id": action.action_id,
                        "attempt": action.attempt,
                        "reason": "approved action argument artifact is invalid",
                    },
                )
                await self._append(
                    lease,
                    "RunBlocked",
                    {"reason": "approved action arguments are invalid"},
                )
                return True
            request = ActionRequest(
                action_id=action.action_id,
                tool_name=action.tool_name,
                arguments=arguments,
                effect_class=action.effect_class,
                requires_approval=False,
                idempotency_key=action.idempotency_key,
            )
            status = await self._execute_action(
                lease,
                request,
                planned=True,
                attempt=action.attempt,
            )
            if status is ActionStatus.OUTCOME_UNKNOWN:
                await self._append(
                    lease,
                    "RunBlocked",
                    {"reason": "approved action outcome is unknown"},
                )
                return True
        return bool(actions)

    async def _record_rejected_actions(
        self,
        lease: Lease,
        projection: RunProjection,
    ) -> bool:
        """Close rejected/expired approval calls without invoking executor.

        ``ApprovalRejected`` moves a run back to the durable queue.  The
        original ``ActionPlanned`` record must then be closed explicitly;
        otherwise ``_recover_inflight_actions`` would treat it as work that
        never crossed the execution boundary and run it on the next claim.
        A ``RunCancelled`` boundary expires an unanswered approval for the
        same reason: if the user later resumes the Run, the planned action is
        closed as not executed rather than being replayed without consent.
        Persisting a normal provider-neutral tool observation also preserves
        the assistant/tool message pairing required by providers on resume.
        """

        rejected = {
            approval.action_id: approval
            for approval in projection.approvals
            if approval.status.value in {"rejected", "expired"}
        }
        if not rejected:
            return False

        actions = [
            action
            for action in projection.actions
            if action.action_id in rejected
            and action.status in {ActionStatus.PLANNED, ActionStatus.PREPARED}
        ]
        for action in actions:
            approval = rejected[action.action_id]
            expired = approval.status.value == "expired"
            cancellation_reason = "approval_expired" if expired else "user_rejected"
            decision_label = "expired" if expired else "rejected"
            observation_text = (
                "运行在该工具调用获得批准前已停止，因此该动作未执行。"
                "不要自动重试同一调用；请继续处理任务中其他可执行步骤。"
                if expired
                else "用户拒绝了本次工具调用，因此该动作未执行。"
                "不要自动重试同一调用；请继续处理任务中其他可执行步骤。"
            )
            # The observation and ActionCancelled events are deliberately
            # separate durable boundaries.  If the process dies between
            # them, reuse the already-committed observation instead of
            # writing a duplicate (the projection keys observations by
            # action_id/attempt and rejects a second record).
            observation, observation_artifact = await self._find_action_observation(
                lease.run_id,
                action.action_id,
                action.attempt,
            )
            if observation is None:
                effect = (
                    action.effect_class.value
                    if isinstance(action.effect_class, EffectClass)
                    else str(action.effect_class)
                )
                observation = make_observation(
                    action_id=action.action_id,
                    attempt=action.attempt,
                    tool_name=action.tool_name,
                    status="cancelled",
                    effect_class=effect,
                    text=observation_text,
                    error_kind="user_rejected",
                    recovery="policy",
                    provenance=("durable-worker", "approval"),
                    metadata={
                        "approval_id": approval.approval_id,
                        "decision": decision_label,
                        "continue": True,
                    },
                )
                observation_ref = self.store.artifacts.put_text(
                    json.dumps(
                        observation.to_dict(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    media_type="application/json; purpose=tool-observation-approval",
                )
                observation_artifact = observation_ref.digest
                await self._append_observation_event(
                    lease,
                    observation,
                    observation_artifact,
                )
            elif (
                observation.status != "cancelled"
                or observation.error_kind != "user_rejected"
                or not observation_artifact
            ):
                # A same-attempt observation with a different terminal
                # meaning is inconsistent with a rejected, never-started
                # action.  Fail closed; do not guess whether an effect ran.
                raise RunStoreError(
                    "rejected approval has an incompatible durable observation"
                )
            await self._append(
                lease,
                "ActionCancelled",
                {
                    "action_id": action.action_id,
                    "attempt": action.attempt,
                    "observation_artifact": observation_artifact,
                    "cancellation_reason": cancellation_reason,
                },
                artifact_refs=((observation_artifact,) if observation_artifact else ()),
            )
        return bool(actions)

    async def _recover_inflight_actions(
        self,
        lease: Lease,
        projection: RunProjection,
    ) -> tuple[bool, bool]:
        """Recover action boundaries without replaying an uncertain effect.

        Planned/prepared actions have not crossed the durable ``ActionStarted``
        boundary and can be resumed.  Once ``ActionStarted`` exists, a
        committed observation closes the action; an idempotent read-only action
        may be retried as a new attempt, while every other uncertain effect is
        explicitly blocked for reconciliation.
        """

        pending_approval_actions = {
            approval.action_id
            for approval in projection.approvals
            if approval.status.value == "requested"
        }
        recovered = False
        # Pause/crash recovery may already have marked a started effect as
        # unknown. Explicit continuation does not reconcile that effect, so
        # keep the same barrier used for outcomes discovered below.
        unknown = next((action for action in projection.actions
                        if action.status is ActionStatus.OUTCOME_UNKNOWN), None)
        if unknown is not None:
            await self._append(lease, "RunBlocked", {
                "reason": "action outcome is unknown after worker recovery",
                "action_id": unknown.action_id,
            })
            return True, True
        for action in projection.actions:
            if action.action_id in pending_approval_actions:
                continue
            if action.status in {ActionStatus.PLANNED, ActionStatus.PREPARED}:
                arguments = self._read_action_arguments(action.arguments_artifact)
                if arguments is None:
                    await self._block_unreconstructable_action(
                        lease,
                        action.action_id,
                        action.attempt,
                        action.status,
                    )
                    return recovered, True
                request = ActionRequest(
                    action_id=action.action_id,
                    tool_name=action.tool_name,
                    arguments=arguments,
                    effect_class=action.effect_class,
                    requires_approval=False,
                    idempotency_key=action.idempotency_key,
                )
                status = await self._execute_action(
                    lease,
                    request,
                    planned=True,
                    attempt=action.attempt,
                )
                recovered = True
                if status is ActionStatus.OUTCOME_UNKNOWN:
                    await self._append(
                        lease,
                        "RunBlocked",
                        {"reason": "recovered action outcome is unknown", "action_id": action.action_id},
                    )
                    return recovered, True
                continue
            if action.status is not ActionStatus.STARTED:
                continue
            recovered = True
            observation, observation_artifact = await self._find_action_observation(
                lease.run_id,
                action.action_id,
                action.attempt,
            )
            if observation is None:
                contract = self.contracts.get(action.tool_name)
                safe_retry = (
                    contract.effect_class is EffectClass.READ_ONLY
                    and contract.retryable
                    and contract.idempotent
                    and action.attempt < contract.max_retries
                )
                retry_arguments = self._read_action_arguments(action.arguments_artifact)
                if safe_retry and retry_arguments is not None:
                    observation = make_observation(
                        action_id=action.action_id,
                        attempt=action.attempt,
                        tool_name=action.tool_name,
                        status="cancelled",
                        effect_class=EffectClass.READ_ONLY.value,
                        text="read-only action was retried after worker lease expiry",
                        error_kind="worker_lease_expired",
                        recovery="retry_safe_read",
                        provenance=("durable-recovery", "lease-expiry"),
                        metadata={"recovered": True, "safe_retry": True},
                    )
                    observation_ref = self.store.artifacts.put_text(
                        json.dumps(
                            observation.to_dict(),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        media_type="application/json; purpose=tool-observation-recovery",
                    )
                    observation_artifact = observation_ref.digest
                    await self._append_observation_event(lease, observation, observation_artifact)
                    await self._append_recovered_terminal(
                        lease,
                        action,
                        observation,
                        observation_artifact,
                    )
                    retry_request = ActionRequest(
                        action_id=action.action_id,
                        tool_name=action.tool_name,
                        arguments=retry_arguments,
                        effect_class=action.effect_class,
                        requires_approval=False,
                        idempotency_key=action.idempotency_key,
                    )
                    retry_status = await self._execute_action(
                        lease,
                        retry_request,
                        attempt=action.attempt + 1,
                    )
                    if retry_status is ActionStatus.OUTCOME_UNKNOWN:
                        await self._append(
                            lease,
                            "RunBlocked",
                            {
                                "reason": "safe read retry outcome is unknown",
                                "action_id": action.action_id,
                            },
                        )
                        return recovered, True
                    continue
                observation = make_observation(
                    action_id=action.action_id,
                    attempt=action.attempt,
                    tool_name=action.tool_name,
                    status="unknown",
                    effect_class=(
                        action.effect_class.value
                        if isinstance(action.effect_class, EffectClass)
                        else str(action.effect_class)
                    ),
                    text="worker lease expired before the action outcome was committed",
                    error_kind="worker_lease_expired",
                    recovery="reconcile",
                    provenance=("durable-recovery", "lease-expiry"),
                    metadata={"recovered": True, "requires_reconciliation": True},
                )
                observation_ref = self.store.artifacts.put_text(
                    json.dumps(
                        observation.to_dict(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    media_type="application/json; purpose=tool-observation-recovery",
                )
                observation_artifact = observation_ref.digest
                await self._append_observation_event(lease, observation, observation_artifact)
            else:
                self._trigger_activation(
                    "tools",
                    run_id=lease.run_id,
                    tool_name=observation.tool_name,
                    observation_id=observation.observation_id,
                    recovered=True,
                )
            await self._append_recovered_terminal(lease, action, observation, observation_artifact)
            if observation.status not in {"succeeded", "failed", "cancelled"}:
                await self._append(
                    lease,
                    "RunBlocked",
                    {
                        "reason": "action outcome is unknown after worker recovery",
                        "action_id": action.action_id,
                    },
                )
                return recovered, True
        return recovered, False

    def _read_action_arguments(self, artifact: str | None) -> dict[str, Any] | None:
        if not artifact:
            return None
        try:
            value = json.loads(self.store.artifacts.read_text(artifact))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return dict(value) if isinstance(value, Mapping) else None

    async def _block_unreconstructable_action(
        self,
        lease: Lease,
        action_id: str,
        attempt: int,
        status: ActionStatus,
    ) -> None:
        await self._append(
            lease,
            "ActionCancelled" if status in {ActionStatus.PLANNED, ActionStatus.PREPARED} else "ActionOutcomeUnknown",
            {
                "action_id": action_id,
                "attempt": attempt,
                (
                    "reason"
                    if status not in {ActionStatus.PLANNED, ActionStatus.PREPARED}
                    else "cancellation_reason"
                ): "action arguments are unavailable after worker recovery",
            },
        )
        await self._append(
            lease,
            "RunBlocked",
            {"reason": "recovered action arguments are unavailable", "action_id": action_id},
        )

    async def _find_action_observation(
        self,
        run_id: str,
        action_id: str,
        attempt: int,
    ) -> tuple[ToolObservation | None, str | None]:
        after = 0
        while True:
            page = await self.store.read(run_id, after=after, limit=1000)
            for event in page.events:
                if event.event_type != "ToolObservationCommitted":
                    continue
                if event.payload.get("action_id") != action_id:
                    continue
                if int(event.payload.get("attempt", 0)) != attempt:
                    continue
                artifact = event.payload.get("observation_artifact")
                if not artifact:
                    continue
                try:
                    value = json.loads(self.store.artifacts.read_text(str(artifact)))
                    return ToolObservation.from_dict(value), str(artifact)
                except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
                    continue
            if page.next_cursor is None:
                return None, None
            after = page.next_cursor

    async def _append_observation_event(
        self,
        lease: Lease,
        observation: ToolObservation,
        observation_artifact: str,
    ) -> None:
        await self._append(
            lease,
            "ToolObservationCommitted",
            {
                "observation_id": observation.observation_id,
                "action_id": observation.action_id,
                "attempt": observation.attempt,
                "tool_name": observation.tool_name,
                "observation_artifact": observation_artifact,
                "status": observation.status,
                "complete": observation.complete,
                "next_cursor": observation.next_cursor,
                "recovery": observation.recovery,
                "provenance": list(observation.provenance),
                "safety_applied": bool(
                    observation.metadata.get("sanitized_by_l5")
                    or observation.metadata.get("safety_applied")
                ),
                "offload_applied": bool(observation.metadata.get("offload")),
            },
            artifact_refs=(observation_artifact,),
        )
        self._trigger_activation(
            "tools",
            run_id=lease.run_id,
            tool_name=observation.tool_name,
            observation_id=observation.observation_id,
            recovered=True,
        )

    async def _append_recovered_terminal(
        self,
        lease: Lease,
        action,
        observation: ToolObservation,
        observation_artifact: str | None,
    ) -> None:
        payload: dict[str, Any] = {
            "action_id": action.action_id,
            "attempt": action.attempt,
            "observation_artifact": observation_artifact,
            "modified_paths": list(observation.modified_paths),
            "read_paths": list(observation.read_paths),
        }
        result_artifact = next(
            (block.artifact_ref for block in observation.content if block.artifact_ref),
            None,
        )
        if result_artifact:
            payload["result_artifact"] = result_artifact
        if observation.status == "succeeded":
            event_type = "ActionSucceeded"
        elif observation.status == "failed":
            event_type = "ActionFailed"
            payload["error_kind"] = observation.error_kind or "execution"
        elif observation.status == "cancelled":
            event_type = "ActionCancelled"
        else:
            event_type = "ActionOutcomeUnknown"
            payload["reason"] = observation.error_kind or "unknown"
        await self._append(lease, event_type, payload)

    @staticmethod
    def _next_segment(projection: RunProjection) -> int:
        return len(projection.progress) + len(projection.actions) + 1

    @staticmethod
    def _deduplicate_action_requests(
        projection: RunProjection,
        requests: Sequence[ActionRequest],
    ) -> tuple[tuple[ActionRequest, ...], bool]:
        """Select one executable request per semantic action key.

        The durable projection is loaded before a model batch is parsed.  A
        provider can nevertheless return duplicate calls in that same batch,
        so a local key set closes the intra-response race before any executor
        is invoked.  ``True`` means at least one request was already in flight
        and requires reconciliation/blocking rather than a blind replay.
        """

        duplicate_inflight = False
        request_buffer: list[ActionRequest] = []
        seen_idempotency_keys: set[str] = set()
        for request in requests:
            if request.idempotency_key in seen_idempotency_keys:
                duplicate_inflight = True
                continue
            seen_idempotency_keys.add(request.idempotency_key)
            duplicate_status = RunWorker._action_deduplication_status(projection, request)
            if duplicate_status in {
                ActionStatus.SUCCEEDED,
                ActionStatus.FAILED,
                ActionStatus.CANCELLED,
            }:
                continue
            if duplicate_status in {
                ActionStatus.PLANNED,
                ActionStatus.PREPARED,
                ActionStatus.STARTED,
                ActionStatus.OUTCOME_UNKNOWN,
            }:
                duplicate_inflight = True
                continue
            request_buffer.append(request)
        return tuple(request_buffer), duplicate_inflight

    def _refresh_completed_reads(
        self, projection: RunProjection, requests: Sequence[ActionRequest]
    ) -> tuple[ActionRequest, ...]:
        """Permit a new read after a terminal receipt, keeping effect dedupe.

        The trusted tool registry, not model-declared effects, decides which
        operations can be reread. The same provider call ID remains a replay;
        equivalent reads in one new batch retain one shared deduplication key.
        """
        fresh_keys: dict[str, str] = {}
        result = []
        for request in requests:
            uncertain = next((a for a in reversed(projection.actions)
                if a.tool_name == request.tool_name and a.normalized_args_digest == request.normalized_args_digest
                and a.status in {ActionStatus.PLANNED, ActionStatus.PREPARED, ActionStatus.STARTED, ActionStatus.OUTCOME_UNKNOWN}), None)
            if uncertain is not None:
                result.append(replace(request, idempotency_key=uncertain.idempotency_key))
                continue
            if request.tool_name == "run_command" and (_is_verification_command(request.tool_name, str(request.arguments.get("command", "")))
                or python_assertion_verifier(self.working_directory, str(request.arguments.get("command", ""))) is not None):
                # Local verification is a new operation after inputs change.
                # Never re-key externally effectful commands or provider IDs
                # already committed; uncertain outcomes still require recovery.
                command = str(request.arguments.get("command", ""))
                if (re.match(r"\s*(?:python(?:3)?|pytest|ruff|mypy|node|npm|pnpm|yarn)\b", command)
                    and not re.search(r"\b(?:push|publish|deploy|upload|curl|wget)\b", command)
                    and not request.arguments.get("idempotency_key") and not request.arguments.get("_cc_idempotency_key")
                    and not any(a.action_id == request.action_id for a in projection.actions)):
                    request = replace(request, idempotency_key=f"verify:{projection.working_state.last_mutation_sequence}:{request.idempotency_key}")
            terminal = self._action_deduplication_status(projection, request)
            if (
                self.contracts.get(request.tool_name).effect_class is EffectClass.READ_ONLY
                and terminal in {ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.CANCELLED}
                and not any(action.action_id == request.action_id for action in projection.actions)
            ):
                key = fresh_keys.setdefault(
                    request.idempotency_key,
                    f"read:{request.action_id}:{request.idempotency_key}",
                )
                request = replace(request, idempotency_key=key)
            result.append(request)
        return tuple(result)

    async def _resolve_replaced_failures(self, lease, request, observation, observation_artifact):
        projection = await self.store.load_projection(lease.run_id)
        unresolved = {e.get("action_id") for e in projection.working_state.unresolved_errors}
        pinned_resource = None
        if request.tool_name == "read_skill":
            for block in observation.content:
                try:
                    resource = json.loads(str(block.text or ""))
                except (ValueError, TypeError):
                    continue
                if isinstance(resource, dict) and resource.get("snapshot") == projection.skill_snapshot:
                    pinned_resource = resource.get("resource_path")
        for action in projection.actions:
            if action.action_id not in unresolved or action.status is not ActionStatus.FAILED:
                continue
            args = json.loads(self.store.artifacts.read_text(action.arguments_artifact))
            discarded_precondition = False
            if action.tool_name in {"Write", "Edit", "run_command"} and _observation_verifies(observation, str(request.arguments.get("command", ""))):
                receipt = next((o for o in projection.observations if o.action_id == action.action_id and o.status == "failed"), None)
                if receipt is not None:
                    failed_observation = json.loads(self.store.artifacts.read_text(receipt.observation_artifact))
                    failed_text = " ".join(str(b.get("text", "")) for b in failed_observation.get("content", [])).casefold()
                    if action.tool_name in {"Write", "Edit"}:
                        discarded_precondition = any(marker in failed_text for marker in (
                            "requires expected_hash", "stale content hash", "file changed before commit", "exact old_text must match once", "expected_hash mismatch"))
                    else:
                        # PowerShell parses the entire statement before any
                        # command runs. A host-reported parser rejection has
                        # no command effect to reconcile; keep its failure in
                        # history but allow independently verified work to end.
                        stderr = str(failed_observation.get("metadata", {}).get("stderr", "")).lstrip()
                        discarded_precondition = stderr.startswith("ParserError:")
            if action.tool_name != request.tool_name and not (action.tool_name == "Read" and pinned_resource) and not discarded_precondition:
                continue
            same_command = (request.tool_name == "run_command" and _observation_verifies(observation, str(request.arguments.get("command", "")))
                            and (args == dict(request.arguments) or bool(_local_verifier_invocations(str(args.get("command", "")))
                                     & _local_verifier_invocations(str(request.arguments.get("command", ""))))))
            same_file = (request.tool_name in {"Write", "Edit", "Read"} and args.get("path")
                         and self._workspace_path(str(args["path"])) is not None
                         and self._workspace_path(str(args["path"])) == self._workspace_path(str(request.arguments.get("path", ""))))
            pinned_read = (action.tool_name == "Read" and pinned_resource and args.get("path")
                           and self._workspace_path(str(args["path"])) is not None
                           and self._workspace_path(str(args["path"])) == self._workspace_path(str(pinned_resource)))
            if same_command or same_file or pinned_read or discarded_precondition:
                await self._append(lease, "FailureResolved", {"failed_action_id": action.action_id,
                    "resolving_action_id": request.action_id, "observation_artifact": observation_artifact,
                    "reason": "rejected pre-execution proposal superseded by verified current workspace" if discarded_precondition else "successful corresponding replacement"},
                    artifact_refs=(observation_artifact,))

    async def _completion_repair_issues(self, run_id):
        p = await self.store.load_projection(run_id)
        contract = completion_contract_from_instruction(_goal_public_text(p.goal) if p.goal else "")
        issues = list(artifact_validation_issues(self.working_directory, contract.required_artifacts))
        unresolved = {e.get("action_id") for e in p.working_state.unresolved_errors}
        for a in p.actions:
            if a.action_id in unresolved:
                args = self.store.artifacts.read_text(a.arguments_artifact)
                issues.append(f"Unresolved failed {a.tool_name}: {args[:600]}. Repair its cause and run the corresponding action/verifier successfully.")
        issues.append("Completion is not verified. Run a goal-relevant verifier after the last edit, without output pipelines that mask its exit code. Check required outputs. A command succeeding or git status alone is not goal acceptance.")
        if any(a.action_id in unresolved and a.tool_name == "Read"
               and "skill-runs" in self.store.artifacts.read_text(a.arguments_artifact) for a in p.actions):
            issues.append("For the failed pinned Skill resource read, use read_skill with the enabled skill_id and resource instead of direct Read of the internal path.")
        return issues

    @staticmethod
    def _action_deduplication_status(
        projection: RunProjection, request: ActionRequest
    ) -> ActionStatus | None:
        """Find the durable receipt for an action or semantic duplicate."""

        candidates = [
            action
            for action in projection.actions
            if action.action_id == request.action_id
            or (
                action.idempotency_key
                and action.idempotency_key == request.idempotency_key
            )
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item.attempt, item.action_id)).status

    def _action_already_committed(self, projection: RunProjection, request: ActionRequest) -> bool:
        return self._action_deduplication_status(projection, request) in {
            ActionStatus.SUCCEEDED,
            ActionStatus.FAILED,
            ActionStatus.CANCELLED,
            ActionStatus.OUTCOME_UNKNOWN,
        }

    async def _execute_request_batch(
        self,
        lease: Lease,
        requests: Sequence[ActionRequest],
    ) -> tuple[ActionStatus, ...]:
        """Run independent read-only calls together; keep effects serial."""

        if len(requests) > 1 and all(
            request.tool_name in {"Read", "Glob", "Grep"}
            and self.contracts.get(request.tool_name).effect_class is EffectClass.READ_ONLY
            for request in requests
        ):
            return tuple(await asyncio.gather(*(self._execute_action(lease, request) for request in requests)))
        statuses: list[ActionStatus] = []
        for request in requests:
            statuses.append(await self._execute_action(lease, request))
        return tuple(statuses)

    def _trigger_activation(self, name: str, **details: Any) -> None:
        if self.activation_manifest is None:
            return
        try:
            self.activation_manifest.trigger(name, **details)
        except (KeyError, OSError, TypeError, ValueError):
            # Activation evidence is observability; it must not interrupt a
            # lease-owned run whose durable events are already authoritative.
            return

    async def _commit_memory_checkpoint(
        self,
        lease: Lease,
        *,
        segment: int,
        committed_progress: bool,
    ) -> None:
        checkpoint = await self._checkpoint_memory(
            lease.run_id,
            segment=segment,
            committed_progress=committed_progress,
        )
        if checkpoint is None:
            return
        await self._append(
            lease,
            "MemoryCandidateRecorded",
            {
                "candidate_id": checkpoint.checkpoint_id,
                "source_digest": checkpoint.source_digest,
                "status": "captured",
                "captured_count": checkpoint.captured_count,
            },
            artifact_refs=((checkpoint.artifact,) if checkpoint.artifact else ()),
        )
        await self._append(
            lease,
            "MemoryCheckpointCommitted",
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "source_digest": checkpoint.source_digest,
                "captured_count": checkpoint.captured_count,
                "pipeline_enqueued": checkpoint.pipeline_enqueued,
            },
            artifact_refs=((checkpoint.artifact,) if checkpoint.artifact else ()),
        )
        self._trigger_activation(
            "memory",
            run_id=lease.run_id,
            segment=segment,
            captured_count=checkpoint.captured_count,
        )

    async def _commit_assistant_message(
        self,
        lease: Lease,
        segment: int,
        round_index: int,
        outcome,
        *,
        tool_requests: Sequence[ActionRequest] | None = None,
    ) -> None:
        if not self.persist_interactions:
            return
        calls = tuple(
            {
                "id": request.action_id,
                "name": request.tool_name,
                "arguments": dict(request.arguments),
            }
            for request in (
                outcome.action_requests if tool_requests is None else tool_requests
            )
        )
        message = assistant_message(
            outcome.model_text,
            calls,
            refusal=(
                str(getattr(outcome, "refusal"))
                if getattr(outcome, "refusal", None) is not None
                else None
            ),
            stop_reason=str(getattr(outcome, "stop_reason", "") or "model_stop"),
            provider_metadata=(
                getattr(outcome, "provider_metadata", None)
                or (getattr(outcome, "usage", {}) or {}).get("provider_metadata")
                or None
            ),
        )
        artifact = self.store.artifacts.put_text(
            json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            media_type="application/json; purpose=assistant-message",
        )
        await self._append(
            lease,
            "AssistantMessageCommitted",
            {
                "message_id": f"assistant-{lease.run_id}-{segment}-{round_index}",
                "message_artifact": artifact.digest,
                "segment": segment,
                "round": round_index,
                "content_digest": artifact.digest,
                "stop_reason": outcome.stop_reason,
                "provider_metadata": dict(
                    getattr(outcome, "provider_metadata", None)
                    or (getattr(outcome, "usage", {}) or {}).get("provider_metadata")
                    or {}
                ),
                "tool_call_ids": [
                    request.action_id
                    for request in (
                        outcome.action_requests if tool_requests is None else tool_requests
                    )
                ],
                "usage": dict(outcome.usage),
            },
            artifact_refs=(artifact.digest,),
        )

    async def _try_accept_completion(
        self,
        lease: Lease,
        candidate: CompletionCandidate | None,
    ) -> bool:
        projection = await self.store.load_projection(lease.run_id)
        if projection.task.pause_requested or any(not x["applied"] for x in projection.task.guidance):
            return False
        if projection.task.guidance and projection.goal and projection.goal.interaction_mode == "coding":
            latest_guidance = max(item["sequence"] for item in projection.task.guidance)
            if projection.working_state.last_verification_sequence <= latest_guidance:
                return False  # old checks cannot certify a revised goal
        # Completion is a state transition, not a cosmetic model response.
        # Refuse it while the durable projection still contains an unresolved
        # error, a requested approval, or an action that has not reached a
        # terminal successful boundary.  This also covers provider-supplied
        # candidates: canonical evidence alone must not hide a later failed or
        # outcome-unknown action in the same turn.
        if projection.working_state.unresolved_errors:
            return False
        if any(item.status.value == "requested" for item in projection.approvals):
            return False
        latest_actions: dict[str, Any] = {}
        for action in projection.actions:
            previous = latest_actions.get(action.action_id)
            if previous is None or action.attempt > previous.attempt:
                latest_actions[action.action_id] = action
        if any(
            action.status
            in {
                ActionStatus.PLANNED,
                ActionStatus.PREPARED,
                ActionStatus.STARTED,
                ActionStatus.OUTCOME_UNKNOWN,
            }
            for action in latest_actions.values()
        ):
            return False
        source = "model"
        if self.capability_runtime is not None and self.completion_verifier is None and not _is_benchmark_task():
            # The product path never trusts model-assigned evidence kinds,
            # short fixture digests or claimed test outcomes. A completion
            # declaration only requests reconstruction from actual receipts.
            candidate = await self._synthesize_completion_candidate(lease, projection)
            if candidate is None:
                return False
            source = "runtime"
        # A model candidate is only a request to complete.  On the official
        # benchmark path, canonicalize every evidence reference against the
        # append-only observation ledger before it can reach CompletionAccepted.
        # This prevents fabricated digests, stale evidence, and read-only
        # observations from being mistaken for proof.  User runs retain the
        # existing custom-verifier seam for backwards compatibility.
        if candidate is not None and _is_benchmark_task():
            candidate = await self._canonicalize_benchmark_candidate(lease.run_id, candidate)
            if candidate is None:
                return False
        if (
            candidate is not None
            and self.completion_verifier is None
            and not await self._candidate_evidence_is_durable(
                lease.run_id,
                candidate,
                allow_assistant_response=(
                    projection.goal is not None
                    and projection.goal.interaction_mode == "conversation"
                ),
            )
        ):
            await self._append(
                lease,
                "StallDiagnosisRecorded",
                {
                    "diagnosis": "completion evidence is not bound to a durable tool observation",
                },
            )
            if self.capability_runtime is None:
                return False
            # Product models often describe a test with an invalid reference.
            # Discard that claim and reconstruct from actual committed receipts;
            # never let model-supplied evidence override the local verifier.
            candidate = None
        if candidate is None:
            candidate = await self._synthesize_completion_candidate(lease, projection)
            if candidate is None:
                return False
            source = "runtime"
        task_completion = completion_contract_from_instruction(
            _goal_public_text(projection.goal) if projection.goal is not None else "",
            trusted_public_instruction=_is_benchmark_task(),
        )
        if not await self._durable_contract_satisfied(
            lease.run_id,
            projection,
            task_completion,
        ):
            return False
        required_pending = tuple(
            item.child_run_id
            for item in projection.children
            if item.required and item.status not in {"completed", "accepted"}
        )
        if required_pending:
            candidate = replace(
                candidate,
                unaccepted_children=tuple(
                    sorted(set(candidate.unaccepted_children).union(required_pending))
                ),
            )
        task_contract = (
            TaskContract.from_goal(
                projection.goal,
                completion=completion_contract_from_instruction(
                    _goal_public_text(projection.goal),
                    trusted_public_instruction=_is_benchmark_task(),
                ),
            )
            if projection.goal is not None
            else None
        )
        gate = self.completion_gate.evaluate(
            candidate,
            projection.goal,
            strict_digests=_is_benchmark_task(),
        )
        if not gate.accepted:
            await self._append(
                lease,
                "StallDiagnosisRecorded",
                {
                    "diagnosis": "completion gate rejected: " + "; ".join(gate.issues),
                    "issues": list(gate.issues),
                },
            )
            return False
        submitted_payload = candidate.to_dict()
        submitted_payload.update(
            {
                "source": source,
                "synthesized": source == "runtime",
                "gate": gate.to_dict(),
                "task_contract": task_contract.to_dict() if task_contract is not None else None,
            }
        )
        await self._append(lease, "CompletionCandidateSubmitted", submitted_payload)
        verifier = self.completion_verifier or self._valid_candidate
        await self._kernel_phase(lease.run_id, "verify", "before")
        await self.execution.append(lease.run_id, "TaskVerificationStarted", {})
        passed = bool(verifier(candidate))
        await self.execution.append(lease.run_id, "TaskVerificationFinished", {"passed": passed})
        await self._kernel_phase(lease.run_id, "verify", "after")
        latest = await self.store.load_projection(lease.run_id)
        if latest.task.pause_requested or latest.task.goal_revision != projection.task.goal_revision:
            return False
        if passed:
            try:
                if projection.goal is not None:
                    candidate.validate(projection.goal)
            except ValueError:
                return False
            if self._active_node_id:
                await self._append(
                    lease,
                    "PlanNodeCompleted",
                    {"node_id": self._active_node_id},
                )
                await self._append(lease, "TodoCompleted", {"todo_id": self._active_node_id})
                updated = await self.store.load_projection(lease.run_id)
                completed = {
                    todo.todo_id
                    for todo in updated.todos
                    if todo.status in {"done", "completed"}
                }
                remaining = [
                    node
                    for node in updated.plan.nodes
                    if node.node_id not in completed
                ]
                if remaining:
                    await self._append(
                        lease,
                        "ProgressRecorded",
                        {
                            "progress": {
                                "kind": "plan_node_completed",
                                "description": f"plan node {self._active_node_id} completed",
                                "evidence": [item.to_dict() for item in candidate.evidence],
                                "segment": 0,
                            }
                        },
                    )
                    return False
            accepted_payload = candidate.to_dict()
            accepted_payload.update(
                {
                    "source": source,
                    "synthesized": source == "runtime",
                    "evidence_refs": [item.digest for item in candidate.evidence],
                }
            )
            await self._append(lease, "CompletionAccepted", accepted_payload)
            with contextlib.suppress(Exception):
                await self.store.checkpoint(lease.run_id)
            if self.child_completion_callback is not None and await self._is_child_run(lease.run_id):
                await self.child_completion_callback(lease.run_id, candidate)
            return True
        return False

    async def _durable_contract_satisfied(
        self,
        run_id: str,
        projection: RunProjection,
        contract,
    ) -> bool:
        """Apply executable completion obligations before the structural gate.

        The model candidate is only a declaration of intent.  This check uses
        the append-only observation ledger and filesystem, so a final sentence
        cannot bypass missing artifacts, required verification, or an
        unready service.  Official benchmark tasks may rely on Harbor as the
        external oracle when no explicit local verification command was
        requested; explicit task commands remain mandatory.
        """

        if artifact_validation_issues(
            self.working_directory,
            contract.required_artifacts,
            allowed_external_roots=contract.allowed_external_roots,
        ):
            return False
        if (
            contract.require_verification_after_code_changes
            and projection.working_state.modified_paths
            and not _is_benchmark_task()
            and (
                projection.working_state.last_verification_sequence
                < projection.working_state.last_mutation_sequence
                or projection.working_state.last_verification_ok is not True
            )
        ):
            return False
        events = await self._read_all_events(run_id)
        successful_commands: set[str] = set()
        healthy_service = False
        for event in events:
            if event.event_type != "ToolObservationCommitted":
                continue
            if event.payload.get("status") != "succeeded" or not bool(
                event.payload.get("complete", True)
            ):
                continue
            artifact = str(event.payload.get("observation_artifact") or "")
            if not artifact:
                continue
            try:
                observation = ToolObservation.from_dict(
                    json.loads(self.store.artifacts.read_text(artifact))
                )
            except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
            metadata = observation.metadata
            arguments = metadata.get("request_arguments")
            command = (
                str(arguments.get("command") or "")
                if isinstance(arguments, Mapping)
                else ""
            )
            if _observation_verifies(observation, command):
                successful_commands.add(
                    _text_digest(_normalise_command(command))
                )
            if observation.tool_name == "service_status":
                health_status = str(metadata.get("health_status") or "")
                healthy_service = healthy_service or health_status == "healthy"
            readiness = metadata.get("readiness")
            if isinstance(readiness, Mapping) and readiness.get("status") == "ready":
                healthy_service = True

        for command in contract.verification_commands:
            if _text_digest(_normalise_command(command)) not in successful_commands:
                return False
        if contract.require_service_health_check and not healthy_service:
            return False
        return True

    async def _candidate_evidence_is_durable(
        self,
        run_id: str,
        candidate: CompletionCandidate,
        *,
        allow_assistant_response: bool = False,
    ) -> bool:
        """Reject fabricated full digests while preserving legacy short refs.

        Evidence references are intentionally opaque to the model.  A custom
        completion verifier remains an explicit extension point, but the
        default worker binds production-shaped SHA-256 references to a
        successful, complete observation (or one of its content blocks).  A
        short digest is treated as a legacy test fixture and is left to the
        structural gate for backwards compatibility.
        """

        strong_digests = {
            str(item.digest)
            for item in candidate.evidence
            if str(item.digest).startswith("sha256:")
            and len(str(item.digest).removeprefix("sha256:")) == 64
        }
        if not strong_digests:
            return True
        durable: set[str] = set()
        for event in await self._read_all_events(run_id):
            if allow_assistant_response and event.event_type == "AssistantMessageCommitted":
                message_artifact = str(event.payload.get("message_artifact") or "")
                if message_artifact and self.store.artifacts.exists(message_artifact):
                    durable.add(message_artifact)
                continue
            if (
                event.event_type != "ToolObservationCommitted"
                or event.payload.get("status") != "succeeded"
                or not bool(event.payload.get("complete", True))
            ):
                continue
            observation_artifact = str(event.payload.get("observation_artifact") or "")
            if not observation_artifact:
                continue
            durable.add(observation_artifact)
            try:
                observation = ToolObservation.from_dict(
                    json.loads(self.store.artifacts.read_text(observation_artifact))
                )
            except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
            durable.update(
                str(block.artifact_ref)
                for block in observation.content
                if block.artifact_ref and self.store.artifacts.exists(block.artifact_ref)
            )
        return strong_digests.issubset(durable)

    async def _read_all_events(self, run_id: str) -> tuple[RunEvent, ...]:
        events: list[RunEvent] = []
        after = 0
        while True:
            page = await self.store.read(run_id, after=after, limit=1000)
            events.extend(page.events)
            if page.next_cursor is None:
                return tuple(events)
            after = page.next_cursor

    async def _canonicalize_benchmark_candidate(
        self,
        run_id: str,
        candidate: CompletionCandidate,
    ) -> CompletionCandidate | None:
        """Bind model evidence to successful, complete observations in this Run.

        The provider can only refer to digests exposed in committed tool
        results.  We rebuild the corresponding ``EvidenceRef`` from the event
        ledger so source/kind/confidence/timestamps cannot be forged in model
        output.  A benchmark candidate also needs at least one decisive
        observation; a plain Read/Glob/Grep result is not proof.
        """

        events = await self._read_all_events(run_id)
        evidence_by_digest: dict[str, EvidenceRef] = {}
        decisive_digests: set[str] = set()
        for event in events:
            if event.event_type != "ToolObservationCommitted":
                continue
            if event.payload.get("status") != "succeeded" or not bool(
                event.payload.get("complete", True)
            ):
                continue
            artifact = str(event.payload.get("observation_artifact") or "")
            if not artifact:
                continue
            try:
                observation = ToolObservation.from_dict(
                    json.loads(self.store.artifacts.read_text(artifact))
                )
            except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
            arguments = observation.metadata.get("request_arguments")
            command = (
                str(arguments.get("command") or "")
                if isinstance(arguments, Mapping)
                else ""
            )
            source = command or observation.tool_name
            verified = _is_verification_command(observation.tool_name, command)
            decisive = _observation_is_decisive(observation, command=command)
            reference = EvidenceRef(
                evidence_id=(
                    f"verification-{observation.observation_id}"
                    if verified
                    else f"action-{observation.observation_id}"
                ),
                kind=EvidenceKind.TEST if verified else EvidenceKind.ACTION_RESULT,
                digest=artifact,
                source=source,
                recorded_at=time.time(),
                confidence=1.0,
                metadata={
                    "action_id": observation.action_id,
                    "tool_name": observation.tool_name,
                    "status": observation.status,
                    "complete": observation.complete,
                    "canonicalized": True,
                },
            )
            evidence_by_digest.setdefault(artifact, reference)
            if decisive:
                decisive_digests.add(artifact)
            # The model normally sees the observation digest.  Keep the tool
            # result artifact as an accepted alias for providers that expose
            # that lower-level pointer instead.
            for block in observation.content:
                if block.artifact_ref and self.store.artifacts.exists(block.artifact_ref):
                    alias = replace(reference, digest=block.artifact_ref)
                    evidence_by_digest.setdefault(block.artifact_ref, alias)
                    if decisive:
                        decisive_digests.add(block.artifact_ref)

        canonical: list[EvidenceRef] = []
        for evidence in candidate.evidence:
            reference = evidence_by_digest.get(evidence.digest)
            if reference is None:
                return None
            canonical.append(reference)
        if not canonical or not any(item.digest in decisive_digests for item in canonical):
            return None
        return replace(candidate, evidence=tuple(canonical))

    async def _synthesize_completion_candidate(
        self,
        lease: Lease,
        projection: RunProjection,
    ) -> CompletionCandidate | None:
        """Build a candidate only when durable evidence satisfies the goal.

        A model's final sentence is never treated as completion evidence.  The
        gate requires complete successful observations, no unresolved latest
        action, and at least one decisive observation. Ordinary user runs
        require a verification command; trusted official benchmark runs may
        use a successful durable mutation because Harbor remains the external
        oracle. Service goals additionally require a health/readiness probe.
        """

        goal = projection.goal
        if goal is None:
            return None
        if any(
            item.required and item.status not in {"completed", "accepted"}
            for item in projection.children
        ):
            return None
        if any(item.status.value == "requested" for item in projection.approvals):
            return None

        latest_actions: dict[str, Any] = {}
        for action in projection.actions:
            previous = latest_actions.get(action.action_id)
            if previous is None or action.attempt > previous.attempt:
                latest_actions[action.action_id] = action
        if any(
            action.status in {
                ActionStatus.PLANNED,
                ActionStatus.PREPARED,
                ActionStatus.STARTED,
                ActionStatus.OUTCOME_UNKNOWN,
            }
            for action in latest_actions.values()
        ):
            return None

        events = await self._read_all_events(lease.run_id)

        # Conversational runs use an explicit interaction contract. Their
        # completion proof is a non-empty assistant artifact committed by this
        # Runtime, not an untrusted final sentence. Coding runs continue to
        # the stricter tool/verification evidence path below.
        if goal.interaction_mode == "conversation":
            for event in reversed(events):
                if event.event_type != "AssistantMessageCommitted":
                    continue
                artifact = str(event.payload.get("message_artifact") or "")
                if not artifact or not self.store.artifacts.exists(artifact):
                    continue
                try:
                    message = json.loads(self.store.artifacts.read_text(artifact))
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                content = (
                    str(message.get("content") or "").strip()
                    if isinstance(message, Mapping)
                    else ""
                )
                if not content:
                    continue
                return CompletionCandidate(
                    acceptance_criteria=tuple(goal.acceptance_criteria),
                    evidence=(EvidenceRef(
                        evidence_id=f"runtime-response-{event.sequence}",
                        kind=EvidenceKind.ASSISTANT_RESPONSE,
                        digest=artifact,
                        source="durable assistant response",
                        recorded_at=time.time(),
                        confidence=1.0,
                        metadata={
                            "event_sequence": event.sequence,
                            "response_persisted": True,
                        },
                    ),),
                )
            return None

        evidence: list[EvidenceRef] = []
        verification_evidence: list[EvidenceRef] = []
        service_probe = False
        for event in events:
            if (
                event.event_type != "ToolObservationCommitted"
                or event.payload.get("status") != "succeeded"
                or not bool(event.payload.get("complete", True))
            ):
                continue
            artifact = str(event.payload.get("observation_artifact") or "")
            if not artifact:
                continue
            try:
                raw = json.loads(self.store.artifacts.read_text(artifact))
                observation = ToolObservation.from_dict(raw)
            except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
            metadata = dict(observation.metadata)
            arguments = metadata.get("request_arguments")
            command = (
                str(arguments.get("command") or "")
                if isinstance(arguments, Mapping)
                else ""
            )
            source = command or observation.tool_name
            marker_text = f"{observation.tool_name} {command}".casefold()
            is_verification = _observation_verifies(observation, command)
            if any(
                marker in marker_text
                for marker in ("curl", "health", "readiness", "grpcurl", "nc ", "ss ")
            ):
                service_probe = True
            ref = EvidenceRef(
                evidence_id=f"runtime-{observation.observation_id}",
                kind=EvidenceKind.TEST if is_verification else EvidenceKind.ACTION_RESULT,
                digest=artifact,
                source=source,
                recorded_at=time.time(),
                confidence=1.0,
                metadata={
                    "action_id": observation.action_id,
                    "status": observation.status,
                    "effect_class": observation.effect_class,
                    "modified_paths": list(observation.modified_paths),
                },
            )
            evidence.append(ref)
            if is_verification:
                verification_evidence.append(ref)
        if not evidence:
            return None

        # Explicit output paths in the task statement are part of the goal
        # contract.  Validate them against the current workspace before
        # accepting any candidate; merely mentioning or reading the path is
        # not enough.  ``/app`` is Harbor's task root, so map that prefix to
        # the durable worker root while rejecting paths outside its scope.
        output_contract = completion_contract_from_instruction(
            _goal_public_text(goal),
            trusted_public_instruction=_is_benchmark_task(),
        )
        if artifact_validation_issues(
            self.working_directory,
            output_contract.required_artifacts,
            allowed_external_roots=output_contract.allowed_external_roots,
        ):
            return None

        changed_paths = tuple(sorted(set(projection.working_state.modified_paths)))
        # A read-only observation (or a model's final sentence) is never proof.
        # For official benchmark tasks Harbor is the final correctness oracle,
        # so a successful durable mutation is enough to close the runtime
        # boundary; interactive/user runs retain the stricter verification
        # requirement.
        decisive_evidence = [
            ref
            for ref in evidence
            if ref.kind == EvidenceKind.TEST
            or str(ref.metadata.get("effect_class") or "")
            in {
                EffectClass.WORKSPACE_MUTATION.value,
                EffectClass.EXTERNAL_SIDE_EFFECT.value,
            }
            or bool(ref.metadata.get("modified_paths"))
        ]
        if not decisive_evidence:
            return None
        if not _is_benchmark_task() and not verification_evidence:
            return None
        # Use the same executable contract as the final gate. Source filenames
        # such as service.py and unrelated words containing "api" do not
        # request a running service or a health probe.
        service_goal = output_contract.require_service_health_check
        if service_goal and not service_probe:
            return None
        if goal.required_evidence and len(evidence) < len(goal.required_evidence):
            return None
        return CompletionCandidate(
            acceptance_criteria=tuple(goal.acceptance_criteria),
            evidence=tuple(evidence[-50:]),
            modified_paths=changed_paths,
        )

    def _workspace_path(self, raw_path: str) -> Path | None:
        """Resolve an explicit output path under the worker's project root."""

        candidate = Path(raw_path)
        root = self.working_directory.resolve()
        if candidate.is_absolute():
            try:
                relative = candidate.relative_to(Path("/app"))
            except ValueError:
                return None
            candidate = root / relative
        else:
            candidate = root / candidate
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError):
            return None
        return resolved

    async def _acknowledge_cancellation(self, lease: Lease, reason: str) -> bool:
        """Finalize a cooperative cancellation at a safe worker boundary.

        The client only records ``InterruptRequested`` first.  This method is
        the sole worker-owned transition to ``RunCancelled`` after any
        in-flight action has been observed (and, when necessary, marked
        ``ActionOutcomeUnknown``).  Keeping the callback here also updates the
        parent projection for child Runs that were actually cancelled.
        """

        projection = await self.store.load_projection(lease.run_id)
        if projection.status is RunStatus.CANCEL_REQUESTED:
            await self._append(lease, "RunCancelled", {"reason": reason})
            if self.child_cancellation_callback is not None:
                await self.child_cancellation_callback(lease.run_id, reason)
            return True
        if projection.status is RunStatus.CANCELLED:
            if self.child_cancellation_callback is not None:
                await self.child_cancellation_callback(lease.run_id, reason)
            return True
        return False

    async def _is_child_run(self, run_id: str) -> bool:
        record = await self.store.load_run_record(run_id)
        return bool(record.parent_run_id)

    async def _active_node_completed(self, run_id: str) -> bool:
        if not self._active_node_id:
            return False
        projection = await self.store.load_projection(run_id)
        return any(
            todo.todo_id == self._active_node_id
            and todo.status in {"done", "completed"}
            for todo in projection.todos
        )

    def _completed_plan_node_ids(self, projection: RunProjection, events: Sequence[Any]) -> set[str]:
        completed = {
            todo.todo_id
            for todo in projection.todos
            if todo.status in {"done", "completed"}
        }
        completed.update(
            str(event.payload.get("node_id"))
            for event in events
            if event.event_type == "PlanNodeCompleted" and event.payload.get("node_id")
            and event.sequence > max((x["sequence"] for x in projection.task.guidance), default=0)
        )
        return completed

    async def _ensure_plan_node_started(self, lease: Lease, projection: RunProjection) -> str | None:
        if not projection.plan.nodes:
            return None
        page = await self.store.read(lease.run_id, limit=100_000)
        completed = self._completed_plan_node_ids(projection, page.events)
        node = next(
            (
                item
                for item in projection.plan.nodes
                if item.node_id not in completed
                and all(dependency in completed for dependency in item.depends_on)
            ),
            None,
        )
        if node is None:
            return None
        if not any(
            event.event_type == "PlanNodeStarted"
            and event.payload.get("node_id") == node.node_id
            for event in page.events
        ):
            await self._append(lease, "PlanNodeStarted", {"node_id": node.node_id})
            await self._append(
                lease,
                "TodoUpdated",
                {
                    "todo": {
                        "id": node.node_id,
                        "title": node.title or next((todo.title for todo in projection.todos if todo.todo_id == node.node_id), node.node_id),
                        "status": "in_progress",
                        "active_sessions": [lease.run_id],
                        "plan_node_id": node.node_id,
                    }
                },
            )
        return node.node_id

    async def _checkpoint_memory(
        self,
        run_id: str,
        *,
        segment: int,
        committed_progress: bool,
    ):
        if self.capability_runtime is None:
            return None
        projection = await self.store.load_projection(run_id)
        messages = await self._messages_for_projection(projection, include_interactions=True)
        return await self.capability_runtime.checkpoint_memory(
            run_id,
            messages,
            segment=segment,
            committed_progress=committed_progress,
        )

    async def _append_runtime_event(
        self,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        artifact_refs: tuple[str, ...] = (),
    ) -> RunEvent:
        """Append a runtime-owned fact without requiring a live worker lease.

        Terminal invocation/outcome telemetry must survive cancellation and
        lease expiry.  It is still serialized through the same event lock and
        SQLite writer, but deliberately uses lease epoch zero so a stale
        worker cannot fence the audit record after its work has stopped.
        """

        async with self._event_lock:
            # Projection reads and SQLite appends are separate operations.  A
            # stale worker or another runtime-owned telemetry writer can win
            # that small window, so rebuild the event against the latest
            # sequence instead of surfacing a false infrastructure failure.
            for conflict_attempt in range(3):
                projection = await self.store.load_projection(run_id)
                event = RunEvent.create(
                    run_id=run_id,
                    sequence=projection.sequence + 1,
                    event_type=event_type,
                    actor=EventActor("runtime", "durable-runtime"),
                    runtime_contract_digest=str(projection.runtime_contract_digest),
                    lease_epoch=0,
                    payload=dict(payload),
                    artifact_refs=artifact_refs,
                )
                try:
                    return await self.store.append(
                        event,
                        expected_sequence=projection.sequence,
                        expected_lease_epoch=None,
                    )
                except SequenceConflict:
                    if conflict_attempt == 2:
                        raise
                    await asyncio.sleep(0)
            raise AssertionError("unreachable")

    async def _execute_action(
        self,
        lease: Lease,
        request: ActionRequest,
        *,
        planned: bool = False,
        attempt: int = 1,
    ) -> ActionStatus:
        """Execute one action while holding its durable resource leases."""

        current = await self.store.load_projection(lease.run_id)
        if current.task.pause_requested or any(not item["applied"] for item in current.task.guidance):
            key = self._action_reservations.pop(request.action_id, None)
            if key is not None:
                await self.execution.settle(lease.run_id, key, {})
            contract = self.contracts.get(request.tool_name)
            if not planned:
                await self._plan_action(lease, request, contract.effect_class, contract.digest, attempt=attempt)
            observation = make_observation(
                action_id=request.action_id, attempt=attempt, tool_name=request.tool_name,
                status="cancelled", effect_class=contract.effect_class.value,
                text="This proposal was not executed because the task was paused or requirements changed.",
                error_kind="proposal_superseded", recovery="replan", provenance=("task-control",),
            )
            artifact = self.store.artifacts.put_text(json.dumps(observation.to_dict()), media_type="application/json")
            await self._append_observation_event(lease, observation, artifact.digest)
            await self._append(lease, "ActionCancelled", {
                "action_id": request.action_id, "attempt": attempt,
                "observation_artifact": artifact.digest, "cancellation_reason": "proposal_superseded",
            })
            return ActionStatus.CANCELLED

        await self._kernel_phase(lease.run_id, "act", "before")
        contract = self.contracts.get(request.tool_name)
        effect = request.effect_class if request.effect_class != EffectClass.UNKNOWN else contract.effect_class
        amounts = {"tool_calls": 1}
        if effect in {EffectClass.EXTERNAL_SIDE_EFFECT, EffectClass.UNKNOWN}:
            amounts["risk_actions"] = 1
        reservation = self._action_reservations.pop(request.action_id, None)
        if reservation is None:
            reservation = await self.execution.reserve(lease.run_id, amounts)
        started = time.monotonic()
        try:
            return await self._execute_action_with_resources(lease, request, planned=planned, attempt=attempt)
        finally:
            await asyncio.shield(self.execution.settle(lease.run_id, reservation, {
                **amounts, "active_seconds": max(0, time.monotonic() - started),
            }))
            remaining = await self.execution.remaining_time(lease.run_id)
            if remaining is not None and remaining <= 0:
                await self.execution.pause(lease.run_id, "budget_exhausted: active_seconds")
            await self._kernel_phase(lease.run_id, "act", "after")

    async def _execute_action_with_resources(
        self, lease: Lease, request: ActionRequest, *, planned: bool = False, attempt: int = 1,
    ) -> ActionStatus:

        if self.resource_manager is None:
            return await self._execute_action_unlocked(
                lease,
                request,
                planned=planned,
                attempt=attempt,
            )
        effect = request.effect_class
        contract = self.contracts.get(request.tool_name)
        if effect == EffectClass.UNKNOWN:
            effect = contract.effect_class
        async with self.resource_manager.hold_for_action(
            lease,
            request,
            effect=effect,
            working_directory=self.working_directory,
            action_id=request.action_id,
        ):
            return await self._execute_action_unlocked(
                lease,
                request,
                planned=planned,
                attempt=attempt,
            )

    async def _execute_action_unlocked(
        self,
        lease: Lease,
        request: ActionRequest,
        *,
        planned: bool = False,
        attempt: int = 1,
    ) -> ActionStatus:
        contract = self.contracts.get(request.tool_name)
        effect = request.effect_class
        if effect == EffectClass.UNKNOWN:
            effect = contract.effect_class
        before_plan = await self.store.load_projection(lease.run_id)
        if before_plan.status in {RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLED}:
            # The request was planned by the model, but Ctrl+C arrived before
            # execution.  Do not start a new effect after cancellation.
            return ActionStatus.CANCELLED
        if not planned:
            await self._plan_action(
                lease,
                request,
                effect,
                contract.digest,
                attempt=attempt,
            )
        current = await self.store.load_projection(lease.run_id)
        action = next(
            (
                item
                for item in current.actions
                if item.action_id == request.action_id and item.attempt == attempt
            ),
            None,
        )
        if action is None or action.status is ActionStatus.PLANNED:
            await self._append(
                lease,
                "ActionPrepared",
                {"action_id": request.action_id, "attempt": attempt},
            )
            action = await self.store.load_projection(lease.run_id)
            action_item = next(
                (
                    item
                    for item in action.actions
                    if item.action_id == request.action_id and item.attempt == attempt
                ),
                None,
            )
        else:
            action_item = action
        if action_item is None or action_item.status is ActionStatus.PREPARED:
            await self._append(
                lease,
                "ActionStarted",
                {"action_id": request.action_id, "attempt": attempt},
            )
        try:
            if request.protocol_error is not None:
                # The provider produced malformed tool arguments.  Convert it
                # into a normal failed observation so the model can correct the
                # call in the next segment; never let malformed JSON escape as
                # an unhandled worker exception that kills the run.
                result = ActionExecutionResult(
                    ActionStatus.FAILED,
                    error_kind="invalid_tool_arguments",
                    observation=make_observation(
                        action_id=request.action_id,
                        attempt=attempt,
                        tool_name=request.tool_name,
                        status="failed",
                        effect_class="read_only",
                        text=request.protocol_error,
                        error_kind="invalid_tool_arguments",
                        recovery="replan",
                        provenance=("durable-worker", "protocol"),
                        metadata={"request_arguments": dict(request.arguments)},
                    ),
                )
            elif self.action_executor is None:
                result = ActionExecutionResult(
                    ActionStatus.FAILED,
                    error_kind="no_executor",
                )
            else:
                result = await self._execute_action_call(request, contract)
        except asyncio.CancelledError:
            # Cancellation is the supervisor's safety boundary.  Never turn
            # it into OUTCOME_UNKNOWN: doing so lets a wedged action swallow
            # lease-recovery cancellation and keep a stale worker alive.
            raise
        except Exception:
            result = ActionExecutionResult(
                ActionStatus.FAILED if contract.effect_class == EffectClass.READ_ONLY and contract.idempotent else ActionStatus.OUTCOME_UNKNOWN,
                error_kind="executor_lost",
            )
        cancellation_projection = await self.store.load_projection(lease.run_id)
        if (
            cancellation_projection.status in {RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLED}
            and result.status is not ActionStatus.OUTCOME_UNKNOWN
        ):
            # The external effect may have completed concurrently with the
            # interrupt.  Preserve its artifacts/paths but classify the final
            # outcome as unknown so continuation never replays it blindly.
            result = replace(
                result,
                status=ActionStatus.OUTCOME_UNKNOWN,
                observation=None,
                observation_artifact=None,
                error_kind=result.error_kind or "cancelled_during_action",
            )
        observation = result.observation
        observation_artifact = result.observation_artifact
        if observation is not None and observation.attempt != attempt:
            observation = observation.for_attempt(attempt)
            # The adapter artifact described the pre-normalized attempt; write
            # a fresh artifact so event, observation, and digest agree.
            observation_artifact = None
        if observation is None:
            status = (
                "succeeded"
                if result.status is ActionStatus.SUCCEEDED
                else "failed"
                if result.status is ActionStatus.FAILED
                else "cancelled"
                if result.status is ActionStatus.CANCELLED
                else "unknown"
            )
            observation = make_observation(
                action_id=request.action_id,
                attempt=attempt,
                tool_name=request.tool_name,
                status=status,
                effect_class=str(effect.value if isinstance(effect, EffectClass) else effect),
                text=result.error_kind or status,
                result_artifact=result.result_artifact,
                complete=result.complete,
                next_cursor=result.next_cursor,
                read_paths=result.read_paths,
                modified_paths=result.modified_paths,
                error_kind=result.error_kind,
                recovery=("reconcile" if result.status is ActionStatus.OUTCOME_UNKNOWN else "none"),
                provenance=("durable-worker", request.tool_name),
                metadata={"request_arguments": dict(request.arguments)},
            )
        if not observation_artifact:
            observation_ref = self.store.artifacts.put_text(
                json.dumps(observation.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                media_type="application/json; purpose=tool-observation",
            )
            observation_artifact = observation_ref.digest
        await self._append_observation_chunks(lease, observation)
        payload: dict[str, Any] = {"action_id": request.action_id, "attempt": attempt}
        if result.result_artifact:
            payload["result_artifact"] = result.result_artifact
        if result.modified_paths:
            payload["modified_paths"] = list(result.modified_paths)
        if result.read_paths:
            payload["read_paths"] = list(result.read_paths)
        payload["observation_artifact"] = observation_artifact
        diff_refs = set()
        change_set = observation.metadata.get("workspace_change_set") or {}
        if isinstance(change_set, Mapping):
            for kind in ("created", "modified", "deleted"):
                for change in change_set.get(kind, ()):
                    if isinstance(change, Mapping):
                        for field in ("before", "after"):
                            digest = change.get(field)
                            if isinstance(digest, str) and self.store.artifacts.object_path(digest).is_file():
                                diff_refs.add(digest)
        await self._append(
            lease,
            "ToolObservationCommitted",
            {
                "observation_id": observation.observation_id,
                "action_id": request.action_id,
                "attempt": attempt,
                "tool_name": request.tool_name,
                "observation_artifact": observation_artifact,
                "status": observation.status,
                "complete": observation.complete,
                "next_cursor": observation.next_cursor,
                "recovery": observation.recovery,
                "provenance": list(observation.provenance),
                "safety_applied": bool(observation.metadata.get("sanitized_by_l5")),
            },
            artifact_refs=(observation_artifact, *sorted(diff_refs)),
        )
        self._trigger_activation(
            "tools",
            run_id=lease.run_id,
            tool_name=request.tool_name,
            observation_id=observation.observation_id,
        )
        if result.status is ActionStatus.SUCCEEDED:
            event_type = "ActionSucceeded"
        elif result.status is ActionStatus.FAILED:
            event_type = "ActionFailed"
            payload["error_kind"] = result.error_kind or "execution"
        else:
            event_type = "ActionOutcomeUnknown"
            payload["reason"] = result.error_kind or "unknown"
        await self._append(lease, event_type, payload)
        if event_type == "ActionSucceeded":
            await self._resolve_replaced_failures(lease, request, observation, observation_artifact)
            verification = self._verification_evidence(request, observation, observation_artifact)
            if verification is not None:
                await self._append(
                    lease,
                    "VerificationRecorded",
                    {
                        "evidence": [verification.to_dict()],
                        "ok": True,
                        "source": verification.source,
                    },
                    artifact_refs=(verification.digest,),
                )
        return result.status

    def _action_timeout_for(self, request: ActionRequest, contract) -> float:
        """Resolve one bounded action timeout from request/contract/runtime."""

        if request.tool_name == "run_command" and "timeout_seconds" in request.arguments and "action_timeout_seconds" not in request.arguments:
            from .executor import resolve_run_command_timeout
            # Allow process-tree cleanup and a durable timeout receipt before
            # the outer watchdog cancels the adapter at the same deadline.
            return resolve_run_command_timeout(request.arguments["timeout_seconds"]) + 15.0
        raw = request.arguments.get("action_timeout_seconds")
        if raw is None:
            raw = request.arguments.get("timeout_seconds")
        if raw is None:
            raw = getattr(contract, "metadata", {}).get("timeout_seconds")
        return _bounded_timeout(raw, default=self.action_timeout_seconds)

    async def _execute_action_call(self, request: ActionRequest, contract) -> ActionExecutionResult:
        if self.action_executor is None:
            return ActionExecutionResult(ActionStatus.FAILED, error_kind="no_executor")
        timeout = self._action_timeout_for(request, contract)
        lease = getattr(self.execution, "worker_lease", None)
        if lease is not None:
            remaining = await self.execution.remaining_time(lease.run_id)
            if remaining is not None:
                timeout = max(0.001, min(timeout or remaining, remaining))
        task = asyncio.create_task(
            self.action_executor(request),
            name=f"cc-harness-action-{request.action_id}",
        )
        try:
            if timeout == 0:
                return await task
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            await _cancel_task_bounded(task)
            return ActionExecutionResult(
                ActionStatus.FAILED if contract.effect_class == EffectClass.READ_ONLY and contract.idempotent else ActionStatus.OUTCOME_UNKNOWN,
                error_kind=f"action_timeout:{timeout:.3f}s",
            )
        except asyncio.CancelledError:
            await _cancel_task_bounded(task)
            raise

    @staticmethod
    def _verification_evidence(
        request: ActionRequest,
        observation: ToolObservation,
        observation_artifact: str,
    ) -> EvidenceRef | None:
        """Turn an explicitly verification-shaped successful action into evidence.

        Arbitrary successful commands are not tests.  The command/source must
        contain a stable verification marker and the observation must be
        complete and successful.
        """

        if observation.status != "succeeded" or not observation.complete:
            return None
        command = str(request.arguments.get("command") or "")
        source = command or request.tool_name
        if not _observation_verifies(observation, command):
            return None
        return EvidenceRef(
            evidence_id=f"verification-{observation.observation_id}",
            kind=EvidenceKind.TEST,
            digest=observation_artifact,
            source=source,
            recorded_at=time.time(),
            confidence=1.0,
            metadata={
                "action_id": request.action_id,
                "tool_name": request.tool_name,
                "status": observation.status,
                "complete": observation.complete,
            },
        )

    async def _append_observation_chunks(
        self,
        lease: Lease,
        observation: ToolObservation,
    ) -> None:
        chunks: list[tuple[str, str]] = []
        for stream_name in ("stdout", "stderr"):
            values = observation.metadata.get(f"{stream_name}_chunks")
            if not isinstance(values, (list, tuple)):
                continue
            chunks.extend(
                (stream_name, str(value))
                for value in values
                if str(value)
            )
        for index, (stream_name, value) in enumerate(chunks):
            artifact = self.store.artifacts.put_text(
                json.dumps(
                    {"stream": stream_name, "chunk": value},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                media_type="application/json; purpose=tool-observation-chunk",
            )
            await self._append(
                lease,
                "ToolObservationChunkCommitted",
                {
                    "observation_id": observation.observation_id,
                    "chunk_index": index,
                    "observation_artifact": artifact.digest,
                    "complete": observation.complete and index == len(chunks) - 1,
                },
                artifact_refs=(artifact.digest,),
            )

    async def _plan_action(
        self,
        lease: Lease,
        request: ActionRequest,
        effect: EffectClass,
        contract_digest: str,
        *,
        attempt: int = 1,
    ) -> None:
        redacted = _redact_argument_values(dict(request.arguments))
        artifact = self.store.artifacts.put_text(
            json.dumps(redacted, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            media_type="application/json; purpose=action-arguments",
        )
        await self._append(
            lease,
            "ActionPlanned",
            {
                "action_id": request.action_id,
                "attempt": attempt,
                "tool_name": request.tool_name,
                "effect_class": effect.value if isinstance(effect, EffectClass) else effect,
                "normalized_args_digest": request.normalized_args_digest,
                "idempotency_key": request.idempotency_key,
                "contract_digest": contract_digest,
                "worker_id": self.worker_id,
                "arguments_artifact": artifact.digest,
            },
        )

    async def _kernel_phase(self, run_id: str, phase: str, timing: str) -> None:
        projection = await self.store.load_projection(run_id)
        if projection.status in {RunStatus.CANCELLED, RunStatus.COMPLETED, RunStatus.FAILED_TERMINAL}:
            return
        decision = await self.middleware.run(f"{phase}.{timing}", {
            "run_id": run_id, "phase": phase, "timing": timing,
            "task": projection.task.to_dict(), "sequence": projection.sequence,
        })
        if set(decision.patch) - {"annotations"}:
            raise ValueError("middleware attempted to modify protected runtime state")
        await self.execution.append(run_id, "TaskKernelPhase", {
            "phase": phase, "timing": timing, "annotations": decision.patch.get("annotations", {}),
        })
        if not decision.allow:
            await self.execution.pause(run_id, decision.reason or "middleware_short_circuit")
            raise BudgetExhausted(decision.reason)

    async def _model_budget(self, projection, messages) -> dict[str, float]:
        amounts: dict[str, float] = {"steps": 1, "model_calls": 1}
        limits = {}
        for run_id in await self.execution.lineage(projection.run_id):
            limits.update((await self.store.load_projection(run_id)).task.limits)
        if "tokens" in limits:
            adapter = getattr(self.kernel, "model", None)
            max_output = getattr(adapter, "budget_output_tokens", None)
            if not isinstance(max_output, int) or max_output <= 0:
                await self.execution.pause(projection.run_id, "token_budget_requires_output_bound")
                raise BudgetExhausted("token output bound unavailable")
            amounts["tokens"] = len(json.dumps([list(messages), list(self.available_tools)], ensure_ascii=False).encode("utf-8")) + max_output + 256
        if "cost" in limits:
            estimator = getattr(getattr(self.kernel, "model", None), "estimate_max_cost", None)
            if estimator is None:
                await self.execution.pause(projection.run_id, "cost_budget_requires_price_bound")
                raise BudgetExhausted("verified cost bound unavailable")
            try:
                amounts["cost"] = float(estimator(messages, self.available_tools))
            except ValueError:
                await self.execution.pause(projection.run_id, "cost_budget_requires_price_bound")
                raise BudgetExhausted("verified cost bound unavailable")
        if "active_seconds" in limits:
            amounts["active_seconds"] = 0.001
        return amounts

    async def _finish_segment(
        self,
        lease: Lease,
        segment: int,
        *,
        had_action: bool = False,
        had_progress: bool = False,
    ) -> None:
        projection = await self.store.load_projection(lease.run_id)
        if await self.execution.pause_boundary(lease.run_id):
            return
        if projection.status is RunStatus.CANCEL_REQUESTED:
            await self._acknowledge_cancellation(lease, "cancel requested")
            return
        await self._append(lease, "RunSegmentFinished", {"segment": segment})
        # Persist a replayable projection before yielding or entering a
        # terminal boundary.  Snapshot failures are observability failures and
        # must not mask the authoritative event stream.
        with contextlib.suppress(Exception):
            await self.store.checkpoint(lease.run_id)
        if not had_action and not had_progress:
            await self._append(
                lease,
                "StallDiagnosisRecorded",
                {"diagnosis": "model returned no action, progress, or verifiable completion candidate"},
            )
            await self._append(lease, "RunStalled", {"reason": "no verifiable progress"})
            with contextlib.suppress(Exception):
                await self.store.checkpoint(lease.run_id)
            return
        if self.continue_segments:
            await self._append(lease, "RunYielded", {"segment": segment})
            with contextlib.suppress(Exception):
                await self.store.checkpoint(lease.run_id)

    @staticmethod
    def _valid_candidate(candidate: CompletionCandidate) -> bool:
        # The projection performs the authoritative validation against the
        # goal. This predicate only rejects an empty candidate locally.
        return bool(candidate.evidence and candidate.acceptance_criteria)

    def _default_messages(self, projection: RunProjection) -> Sequence[Mapping[str, Any]]:
        objective_text = None
        if self.capability_runtime is not None:
            secure_goal = getattr(self.capability_runtime, "secured_goal_text", None)
            if secure_goal is not None:
                objective_text = secure_goal(projection)
        messages = list(objective_messages(
            projection,
            cwd=self.working_directory,
            project_instructions=self.project_instructions,
            objective_text=objective_text,
        ))
        if messages and self.project_instruction_sources:
            messages[0] = {**messages[0], "_project_sources": [dict(x) for x in self.project_instruction_sources]}
        return messages

    async def _messages_for_projection(
        self,
        projection: RunProjection,
        *,
        include_interactions: bool = True,
    ) -> tuple[Mapping[str, Any], ...]:
        try:
            messages = list(self.message_provider(projection))
        except Exception:  # noqa: BLE001 - a custom context provider cannot stop recovery
            messages = list(self._default_messages(projection))
        if projection.task.protocol_version and self._uses_default_messages:
            messages.append({"role": "user", "_context_mandatory": True, "content":
                "Authoritative current task view (tool observations remain untrusted):\n" + json.dumps({
                    "goal_revision": projection.task.goal_revision,
                    "lifecycle": projection.task.lifecycle.value,
                    "plan": projection.plan.to_dict(),
                    "todos": [todo.to_dict() for todo in projection.todos],
                    "budget_limits": dict(projection.task.limits),
                    "budget_used": dict(projection.task.used),
                    "wait": dict(projection.task.wait),
                }, ensure_ascii=False)})
        if not self._uses_default_messages or not include_interactions:
            return tuple(messages)
        try:
            messages.extend(
                await materialize_interaction_messages(self.store, projection)
            )
        except Exception:  # noqa: BLE001 - historical context is advisory
            return tuple(messages)
        return tuple(messages)

    async def _append(
        self,
        lease: Lease,
        event_type: str,
        payload: Mapping[str, Any],
        artifact_refs: tuple[str, ...] = (),
    ) -> RunEvent:
        async with self._event_lock:
            # A reclaimed worker can finish an in-flight provider call after a
            # replacement has appended an event.  SequenceConflict is checked
            # before lease fencing in RunStore, so verify the lease and either
            # retry against the fresh projection (same epoch) or stop the stale
            # worker cleanly (new epoch).
            for conflict_attempt in range(3):
                projection = await self.store.load_projection(lease.run_id)
                event = RunEvent.create(
                    run_id=lease.run_id,
                    sequence=projection.sequence + 1,
                    event_type=event_type,
                    actor=EventActor("worker", self.worker_id),
                    runtime_contract_digest=str(projection.runtime_contract_digest),
                    lease_epoch=lease.epoch,
                    payload=dict(payload),
                    artifact_refs=artifact_refs,
                )
                try:
                    stored = await self.store.append(
                        event,
                        expected_sequence=projection.sequence,
                        expected_lease_epoch=lease.epoch,
                    )
                    break
                except SequenceConflict:
                    current = await self.store.current_lease(lease.run_id)
                    if (
                        current is None
                        or current.epoch != lease.epoch
                        or current.worker_id != lease.worker_id
                    ):
                        raise LeaseFenceError(
                            "worker lease was fenced while appending an event"
                        )
                    if conflict_attempt == 2:
                        raise
                    await asyncio.sleep(0)
            else:
                raise AssertionError("unreachable")
        derived = outcome_for_event(event_type, payload)
        if derived is not None:
            outcome_payload = derived.to_dict()
            outcome_payload["source_event"] = event_type
            if event_type == "CompletionAccepted":
                outcome_payload["evidence_refs"] = [
                    str(item.get("digest"))
                    for item in (payload.get("evidence") or ())
                    if isinstance(item, Mapping) and item.get("digest")
                ]
            # Terminal lifecycle events can release the worker lease before
            # their derived telemetry is appended.  Record the immutable
            # outcome as a runtime-owned, lease-free event so cancellation or
            # blocking never turns into a misleading LeaseFenceError.
            await self._append_runtime_event(
                lease.run_id,
                "RunOutcomeRecorded",
                outcome_payload,
            )
        return stored


__all__ = ["ActionExecutionResult", "ModelInvocationTimeout", "RunWorker"]
