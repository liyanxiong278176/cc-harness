"""Worker execution of one durable agent segment.

The worker owns only a lease-scoped segment. Every lifecycle, action,
approval, and yield transition is appended through :class:`RunStore`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import traceback
import re
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .action_contracts import ToolContractRegistry
from .activation import ActivationManifest
from .capability_runtime import AgentCapabilityRuntime
from .interaction_history import assistant_message, materialize_interaction_messages, objective_messages
from .lease import LeaseManager
from .run_events import EventActor, RunEvent
from .run_kernel import ActionRequest, AgentKernel, SegmentContext
from .run_model import ActionStatus, CompletionCandidate, EffectClass, Lease, RunStatus
from .run_projection import RunProjection
from .run_store import RunStore
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

_SENSITIVE_KEY = re.compile(
    r"(?:api.?key|access.?token|authorization|cookie|credential|password|passwd|private.?key|secret|token)",
    re.IGNORECASE,
)


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
        action_executor: ActionExecutor | None = None,
        contracts: ToolContractRegistry | None = None,
        completion_verifier: CompletionVerifier | None = None,
        message_provider: MessageProvider | None = None,
        available_tools: Sequence[Mapping[str, Any]] = (),
        continue_segments: bool = True,
        capability_runtime: AgentCapabilityRuntime | None = None,
        persist_interactions: bool = True,
        activation_manifest: ActivationManifest | None = None,
    ) -> None:
        self.store = store
        self.kernel = kernel
        self.worker_id = worker_id
        self.lease_manager = lease_manager or LeaseManager(store)
        self.action_executor = action_executor
        self.contracts = contracts or ToolContractRegistry.first_party()
        self.completion_verifier = completion_verifier
        self._uses_default_messages = message_provider is None
        self.message_provider = message_provider or self._default_messages
        self.available_tools = tuple(dict(item) for item in available_tools)
        self.continue_segments = continue_segments
        self.capability_runtime = capability_runtime
        self.persist_interactions = persist_interactions
        self.activation_manifest = activation_manifest
        self._active_node_id: str | None = None
        self._event_lock = asyncio.Lock()

    async def claim(self, run_id: str) -> Lease:
        return await self.lease_manager.claim(run_id, self.worker_id)

    async def execute(self, lease: Lease) -> None:
        if lease.is_expired():
            raise RuntimeError("worker lease expired before execution")
        heartbeat_task: asyncio.Task[None] | None = None
        current_lease = lease
        try:
            projection = await self.store.load_projection(lease.run_id)
            segment = self._next_segment(projection)
            await self._append(current_lease, "RunSegmentStarted", {"segment": segment})
            self._active_node_id = await self._ensure_plan_node_started(current_lease, projection)
            if projection.plan.nodes and self._active_node_id is None:
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
                await self._append(current_lease, "RunCancelled", {"reason": "cancel requested"})
                return

            # Approval is a durable pause. Once granted, resume the exact
            # planned action from its redacted argument artifact.
            had_action = False
            had_progress = False
            approved = await self._execute_granted_actions(current_lease, projection)
            had_action = approved
            recovered_action, recovery_blocked = await self._recover_inflight_actions(
                current_lease,
                await self.store.load_projection(lease.run_id),
            )
            had_action = had_action or recovered_action
            if recovery_blocked:
                return

            # A Segment is a recoverable interaction interval, not one model
            # call.  It ends at a plan-node/approval/cancellation/completion
            # boundary or when the model returns no verifiable next action.
            while True:
                projection = await self.store.load_projection(lease.run_id)
                if projection.status is RunStatus.CANCEL_REQUESTED:
                    await self._append(current_lease, "RunCancelled", {"reason": "cancel requested"})
                    return
                if self.capability_runtime is not None:
                    validate_goal = getattr(self.capability_runtime, "validate_goal", None)
                    if validate_goal is not None:
                        goal_allowed, goal_reason = await validate_goal(projection)
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
                messages = await self._messages_for_projection(
                    projection,
                    include_interactions=self.capability_runtime is None,
                )
                if self.capability_runtime is not None:
                    context_build = await self.capability_runtime.build_context(
                        projection,
                        messages,
                        self.available_tools,
                        query=projection.goal.objective if projection.goal else "",
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
                    context_overflow = (
                        self.capability_runtime.context_config.enabled
                        and context_build.compaction.after_tokens
                        > self.capability_runtime.context_config.context_window
                    )
                    compaction_error = context_build.compaction.error
                    if context_overflow and not compaction_error:
                        compaction_error = "mandatory context overflow after compaction"
                    if (
                        int(context_build.compaction.tier) > 0
                        or context_build.compaction.summarized
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
                round_index = len(projection.actions)
                await self._append(
                    current_lease,
                    "ModelInvocationStarted",
                    {
                        "invocation_id": f"model-{lease.run_id}-{segment}-{round_index}",
                        "segment": segment,
                        "round": round_index,
                        "context_manifest_artifact": context_manifest_artifact,
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
                    available_tools=self.available_tools,
                    working_state=projection.working_state.to_dict(),
                    lease_epoch=current_lease.epoch,
                    worker_id=self.worker_id,
                    cancellation_requested=projection.status is RunStatus.CANCEL_REQUESTED,
                )
                outcome = await self.kernel.execute_segment(context)
                protect_output = getattr(self.capability_runtime, "protect_model_output", None)
                if protect_output is not None:
                    safe_text, _security_details = protect_output(outcome.model_text, messages)
                    outcome = replace(outcome, model_text=safe_text)
                message_requests = outcome.action_requests
                for request_index, request in enumerate(outcome.action_requests):
                    contract = self.contracts.get(request.tool_name)
                    if request.requires_approval or contract.requires_approval:
                        # Provider protocols require one tool result for every
                        # persisted call. Calls behind this approval boundary
                        # are replanned after the approved result is committed.
                        message_requests = (outcome.action_requests[request_index],)
                        break
                await self._commit_assistant_message(
                    current_lease,
                    segment,
                    round_index,
                    outcome,
                    tool_requests=message_requests,
                )

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
                    if outcome.progress is not None:
                        await self._commit_memory_checkpoint(
                            current_lease,
                            segment=segment,
                            committed_progress=True,
                        )
                    break

                had_action = True
                requests = tuple(
                    request
                    for request in outcome.action_requests
                    if not self._action_already_committed(projection, request)
                )
                if not requests:
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
                    break
                for request in requests:
                    contract = self.contracts.get(request.tool_name)
                    if request.requires_approval or contract.requires_approval:
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
                statuses = await self._execute_request_batch(current_lease, requests)
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
                if await self._try_accept_completion(current_lease, outcome.completion_candidate):
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
        except Exception as exc:
            # A worker exception must become durable state before its lease is
            # released.  Otherwise the supervisor has neither an active task
            # nor an expired lease to reclaim and the run appears to hang in
            # RUNNING forever.
            with contextlib.suppress(Exception):
                await self._append(
                    current_lease,
                    "RunFailed",
                    {
                        "reason": (
                            f"unhandled worker exception: {type(exc).__name__}: "
                            f"{str(exc)[:300]} | "
                            f"{traceback.format_exc(limit=4).replace(chr(10), ' ')[:900]}"
                        ),
                        "target_status": RunStatus.FAILED_RECOVERABLE.value,
                    },
                )
            raise
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
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
    def _action_already_committed(projection: RunProjection, request: ActionRequest) -> bool:
        return any(
            action.action_id == request.action_id
            and action.status
            in {
                ActionStatus.SUCCEEDED,
                ActionStatus.FAILED,
                ActionStatus.CANCELLED,
                ActionStatus.OUTCOME_UNKNOWN,
            }
            for action in projection.actions
        )

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
            reasoning_content=str(getattr(outcome, "reasoning_content", "") or ""),
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
        if candidate is None:
            return False
        await self._append(
            lease,
            "CompletionCandidateSubmitted",
            candidate.to_dict(),
        )
        verifier = self.completion_verifier or self._valid_candidate
        projection = await self.store.load_projection(lease.run_id)
        if verifier(candidate):
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
            await self._append(lease, "CompletionAccepted", candidate.to_dict())
            return True
        return False

    async def _active_node_completed(self, run_id: str) -> bool:
        if not self._active_node_id:
            return False
        projection = await self.store.load_projection(run_id)
        return any(
            todo.todo_id == self._active_node_id
            and todo.status in {"done", "completed"}
            for todo in projection.todos
        )

    async def _ensure_plan_node_started(self, lease: Lease, projection: RunProjection) -> str | None:
        if not projection.plan.nodes:
            return None
        completed = {
            todo.todo_id
            for todo in projection.todos
            if todo.status in {"done", "completed"}
        }
        page = await self.store.read(lease.run_id, limit=100_000)
        completed.update(
            str(event.payload.get("node_id"))
            for event in page.events
            if event.event_type == "PlanNodeCompleted" and event.payload.get("node_id")
        )
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
                        "title": node.node_id,
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

    async def _execute_action(
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
            if self.action_executor is None:
                result = ActionExecutionResult(
                    ActionStatus.OUTCOME_UNKNOWN,
                    error_kind="no_executor",
                )
            else:
                result = await self.action_executor(request)
        except BaseException:
            result = ActionExecutionResult(
                ActionStatus.OUTCOME_UNKNOWN,
                error_kind="executor_lost",
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
            artifact_refs=(observation_artifact,),
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
        return result.status

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
                "contract_digest": contract_digest,
                "worker_id": self.worker_id,
                "arguments_artifact": artifact.digest,
            },
        )

    async def _finish_segment(
        self,
        lease: Lease,
        segment: int,
        *,
        had_action: bool = False,
        had_progress: bool = False,
    ) -> None:
        projection = await self.store.load_projection(lease.run_id)
        if projection.status is RunStatus.CANCEL_REQUESTED:
            await self._append(lease, "RunCancelled", {"reason": "cancel requested"})
            return
        await self._append(lease, "RunSegmentFinished", {"segment": segment})
        if not had_action and not had_progress:
            await self._append(
                lease,
                "StallDiagnosisRecorded",
                {"diagnosis": "model returned no action, progress, or verifiable completion candidate"},
            )
            await self._append(lease, "RunStalled", {"reason": "no verifiable progress"})
            return
        if self.continue_segments:
            await self._append(lease, "RunYielded", {"segment": segment})

    @staticmethod
    def _valid_candidate(candidate: CompletionCandidate) -> bool:
        # The projection performs the authoritative validation against the
        # goal. This predicate only rejects an empty candidate locally.
        return bool(candidate.evidence and candidate.acceptance_criteria)

    @staticmethod
    def _default_messages(projection: RunProjection) -> Sequence[Mapping[str, Any]]:
        return objective_messages(projection)

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
            return await self.store.append(
                event,
                expected_sequence=projection.sequence,
                expected_lease_epoch=lease.epoch,
            )


__all__ = ["ActionExecutionResult", "RunWorker"]
