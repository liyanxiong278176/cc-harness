"""Deterministic projections rebuilt from the append-only Run Event stream."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Mapping

from .run_events import EventValidator, RunEvent
from .run_model import (
    ActionAttempt,
    ActionStatus,
    ApprovalStatus,
    CompletionCandidate,
    EffectClass,
    EvidenceRef,
    GoalContract,
    PlanGraph,
    RunStateMachine,
    RunStatus,
    RuntimeContract,
    digest_json,
)


class ProjectionError(ValueError):
    """Raised when a valid event stream cannot be projected consistently."""


def _event_epoch(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise ProjectionError("event occurred_at is not a valid timestamp") from exc


@dataclass(frozen=True)
class TodoProjection:
    todo_id: str
    title: str
    status: str
    active_sessions: tuple[str, ...] = ()
    updated_sequence: int = 0
    evidence: tuple[EvidenceRef, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "todo_id": self.todo_id,
            "title": self.title,
            "status": self.status,
            "active_sessions": list(self.active_sessions),
            "updated_sequence": self.updated_sequence,
            "evidence": [item.to_dict() for item in self.evidence],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TodoProjection":
        return cls(
            todo_id=str(data["todo_id"]),
            title=str(data["title"]),
            status=str(data["status"]),
            active_sessions=tuple(str(item) for item in data.get("active_sessions") or ()),
            updated_sequence=int(data.get("updated_sequence", 0)),
            evidence=tuple(EvidenceRef.from_dict(item) for item in data.get("evidence") or ()),
        )


@dataclass(frozen=True)
class ApprovalProjection:
    approval_id: str
    run_id: str
    action_id: str
    action_args_digest: str
    scope: tuple[str, ...]
    status: ApprovalStatus = ApprovalStatus.REQUESTED
    decided_by: str | None = None
    decided_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "run_id": self.run_id,
            "action_id": self.action_id,
            "action_args_digest": self.action_args_digest,
            "scope": list(self.scope),
            "status": self.status.value,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApprovalProjection":
        return cls(
            approval_id=str(data["approval_id"]),
            run_id=str(data["run_id"]),
            action_id=str(data["action_id"]),
            action_args_digest=str(data["action_args_digest"]),
            scope=tuple(str(item) for item in data.get("scope") or ()),
            status=ApprovalStatus(str(data.get("status", ApprovalStatus.REQUESTED.value))),
            decided_by=data.get("decided_by"),
            decided_at=str(data["decided_at"]) if data.get("decided_at") is not None else None,
        )


@dataclass(frozen=True)
class QueueProjection:
    follow_up_run_id: str
    predecessor_run_id: str | None
    message_artifact: str
    gate: str = "waiting"
    status: str = "queued"
    queued_sequence: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "follow_up_run_id": self.follow_up_run_id,
            "predecessor_run_id": self.predecessor_run_id,
            "message_artifact": self.message_artifact,
            "gate": self.gate,
            "status": self.status,
            "queued_sequence": self.queued_sequence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "QueueProjection":
        return cls(
            follow_up_run_id=str(data["follow_up_run_id"]),
            predecessor_run_id=data.get("predecessor_run_id"),
            message_artifact=str(data["message_artifact"]),
            gate=str(data.get("gate", "waiting")),
            status=str(data.get("status", "queued")),
            queued_sequence=int(data.get("queued_sequence", 0)),
        )


@dataclass(frozen=True)
class ChildProjection:
    child_run_id: str
    status: str = "planned"
    node_id: str | None = None
    candidate_commit: str | None = None
    diff_digest: str | None = None
    modified_paths: tuple[str, ...] = ()
    accepted_sequence: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_run_id": self.child_run_id,
            "status": self.status,
            "node_id": self.node_id,
            "candidate_commit": self.candidate_commit,
            "diff_digest": self.diff_digest,
            "modified_paths": list(self.modified_paths),
            "accepted_sequence": self.accepted_sequence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChildProjection":
        return cls(
            child_run_id=str(data["child_run_id"]),
            status=str(data.get("status", "planned")),
            node_id=data.get("node_id"),
            candidate_commit=data.get("candidate_commit"),
            diff_digest=data.get("diff_digest"),
            modified_paths=tuple(str(item) for item in data.get("modified_paths") or ()),
            accepted_sequence=(
                int(data["accepted_sequence"])
                if data.get("accepted_sequence") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class ToolObservationProjection:
    """Index of a committed observation; the artifact remains authoritative."""

    observation_id: str
    action_id: str
    attempt: int
    tool_name: str
    observation_artifact: str
    status: str
    complete: bool
    next_cursor: str | None = None
    recovery: str = "none"
    provenance: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "action_id": self.action_id,
            "attempt": self.attempt,
            "tool_name": self.tool_name,
            "observation_artifact": self.observation_artifact,
            "status": self.status,
            "complete": self.complete,
            "next_cursor": self.next_cursor,
            "recovery": self.recovery,
            "provenance": list(self.provenance),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ToolObservationProjection":
        return cls(
            observation_id=str(data["observation_id"]),
            action_id=str(data["action_id"]),
            attempt=int(data["attempt"]),
            tool_name=str(data["tool_name"]),
            observation_artifact=str(data["observation_artifact"]),
            status=str(data["status"]),
            complete=bool(data.get("complete", True)),
            next_cursor=(str(data["next_cursor"]) if data.get("next_cursor") else None),
            recovery=str(data.get("recovery") or "none"),
            provenance=tuple(str(item) for item in data.get("provenance") or ()),
        )


@dataclass(frozen=True)
class WorkingStateProjection:
    modified_paths: tuple[str, ...] = ()
    read_paths: tuple[str, ...] = ()
    last_mutation_sequence: int = 0
    last_verification_sequence: int = 0
    last_verification_ok: bool | None = None
    unresolved_errors: tuple[dict[str, Any], ...] = ()
    result_fingerprints: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "modified_paths": list(self.modified_paths),
            "read_paths": list(self.read_paths),
            "last_mutation_sequence": self.last_mutation_sequence,
            "last_verification_sequence": self.last_verification_sequence,
            "last_verification_ok": self.last_verification_ok,
            "unresolved_errors": [dict(item) for item in self.unresolved_errors],
            "result_fingerprints": list(self.result_fingerprints),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkingStateProjection":
        return cls(
            modified_paths=tuple(str(item) for item in data.get("modified_paths") or ()),
            read_paths=tuple(str(item) for item in data.get("read_paths") or ()),
            last_mutation_sequence=int(data.get("last_mutation_sequence", 0)),
            last_verification_sequence=int(data.get("last_verification_sequence", 0)),
            last_verification_ok=data.get("last_verification_ok"),
            unresolved_errors=tuple(dict(item) for item in data.get("unresolved_errors") or ()),
            result_fingerprints=tuple(str(item) for item in data.get("result_fingerprints") or ()),
        )


@dataclass(frozen=True)
class RunProjection:
    run_id: str
    sequence: int = 0
    last_event_id: str | None = None
    status: RunStatus = RunStatus.DRAFT
    goal: GoalContract | None = None
    runtime_contract: RuntimeContract | None = None
    runtime_contract_digest: str | None = None
    plan: PlanGraph = field(default_factory=PlanGraph)
    discovery_status: str = "not_required"
    mutation_gate: str = "open"
    actions: tuple[ActionAttempt, ...] = ()
    observations: tuple[ToolObservationProjection, ...] = ()
    approvals: tuple[ApprovalProjection, ...] = ()
    todos: tuple[TodoProjection, ...] = ()
    queue: tuple[QueueProjection, ...] = ()
    children: tuple[ChildProjection, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    progress: tuple[dict[str, Any], ...] = ()
    working_state: WorkingStateProjection = field(default_factory=WorkingStateProjection)
    active_worker_id: str | None = None
    lease_epoch: int = 0

    @classmethod
    def empty(cls, run_id: str = "") -> "RunProjection":
        return cls(run_id=run_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "sequence": self.sequence,
            "last_event_id": self.last_event_id,
            "status": self.status.value,
            "goal": self.goal.to_dict() if self.goal else None,
            "runtime_contract": self.runtime_contract.to_dict() if self.runtime_contract else None,
            "runtime_contract_digest": self.runtime_contract_digest,
            "plan": self.plan.to_dict(),
            "discovery_status": self.discovery_status,
            "mutation_gate": self.mutation_gate,
            "actions": [_action_to_dict(item) for item in self.actions],
            "observations": [item.to_dict() for item in self.observations],
            "approvals": [item.to_dict() for item in self.approvals],
            "todos": [item.to_dict() for item in self.todos],
            "queue": [item.to_dict() for item in self.queue],
            "children": [item.to_dict() for item in self.children],
            "evidence": [item.to_dict() for item in self.evidence],
            "progress": [dict(item) for item in self.progress],
            "working_state": self.working_state.to_dict(),
            "active_worker_id": self.active_worker_id,
            "lease_epoch": self.lease_epoch,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunProjection":
        runtime_contract_data = data.get("runtime_contract")
        goal_data = data.get("goal")
        return cls(
            run_id=str(data.get("run_id", "")),
            sequence=int(data.get("sequence", 0)),
            last_event_id=data.get("last_event_id"),
            status=RunStatus(str(data.get("status", RunStatus.DRAFT.value))),
            goal=GoalContract.from_dict(goal_data) if goal_data else None,
            runtime_contract=(
                RuntimeContract.from_dict(runtime_contract_data) if runtime_contract_data else None
            ),
            runtime_contract_digest=data.get("runtime_contract_digest"),
            plan=PlanGraph.from_dict(data.get("plan") or {}),
            discovery_status=str(data.get("discovery_status", "not_required")),
            mutation_gate=str(data.get("mutation_gate", "open")),
            actions=tuple(_action_from_dict(item) for item in data.get("actions") or ()),
            observations=tuple(
                ToolObservationProjection.from_dict(item)
                for item in data.get("observations") or ()
            ),
            approvals=tuple(ApprovalProjection.from_dict(item) for item in data.get("approvals") or ()),
            todos=tuple(TodoProjection.from_dict(item) for item in data.get("todos") or ()),
            queue=tuple(QueueProjection.from_dict(item) for item in data.get("queue") or ()),
            children=tuple(ChildProjection.from_dict(item) for item in data.get("children") or ()),
            evidence=tuple(EvidenceRef.from_dict(item) for item in data.get("evidence") or ()),
            progress=tuple(dict(item) for item in data.get("progress") or ()),
            working_state=WorkingStateProjection.from_dict(data.get("working_state") or {}),
            active_worker_id=data.get("active_worker_id"),
            lease_epoch=int(data.get("lease_epoch", 0)),
        )

    @property
    def digest(self) -> str:
        return digest_json(self.to_dict())


def _action_to_dict(action: ActionAttempt) -> dict[str, Any]:
    return {
        "action_id": action.action_id,
        "run_id": action.run_id,
        "tool_name": action.tool_name,
        "normalized_args_digest": action.normalized_args_digest,
        "effect_class": action.effect_class.value
        if isinstance(action.effect_class, EffectClass)
        else action.effect_class,
        "contract_digest": action.contract_digest,
        "actor_kind": action.actor_kind,
        "actor_id": action.actor_id,
        "worker_id": action.worker_id,
        "attempt": action.attempt,
        "status": action.status.value,
        "lease_epoch": action.lease_epoch,
        "started_at": action.started_at,
        "arguments_artifact": action.arguments_artifact,
        "result_artifact": action.result_artifact,
        "error_kind": action.error_kind,
    }


def _action_from_dict(data: Mapping[str, Any]) -> ActionAttempt:
    return ActionAttempt(
        action_id=str(data["action_id"]),
        run_id=str(data["run_id"]),
        tool_name=str(data["tool_name"]),
        normalized_args_digest=str(data.get("normalized_args_digest", "")),
        effect_class=str(data.get("effect_class", EffectClass.UNKNOWN.value)),
        contract_digest=str(data.get("contract_digest", "")),
        actor_kind=str(data.get("actor_kind", "unknown")),
        actor_id=str(data.get("actor_id", "unknown")),
        worker_id=str(data.get("worker_id", "unknown")),
        attempt=int(data.get("attempt", 1)),
        status=ActionStatus(str(data.get("status", ActionStatus.PLANNED.value))),
        lease_epoch=int(data.get("lease_epoch", 0)),
        started_at=(float(data["started_at"]) if data.get("started_at") is not None else None),
        arguments_artifact=data.get("arguments_artifact"),
        result_artifact=data.get("result_artifact"),
        error_kind=data.get("error_kind"),
    )


@dataclass
class _MutableProjection:
    run_id: str
    sequence: int = 0
    last_event_id: str | None = None
    status: RunStatus = RunStatus.DRAFT
    goal: GoalContract | None = None
    runtime_contract: RuntimeContract | None = None
    runtime_contract_digest: str | None = None
    plan: PlanGraph = field(default_factory=PlanGraph)
    discovery_status: str = "not_required"
    mutation_gate: str = "open"
    actions: dict[tuple[str, int], ActionAttempt] = field(default_factory=dict)
    observations: dict[tuple[str, int], ToolObservationProjection] = field(default_factory=dict)
    approvals: dict[str, ApprovalProjection] = field(default_factory=dict)
    todos: dict[str, TodoProjection] = field(default_factory=dict)
    queue: dict[str, QueueProjection] = field(default_factory=dict)
    children: dict[str, ChildProjection] = field(default_factory=dict)
    evidence: list[EvidenceRef] = field(default_factory=list)
    progress: list[dict[str, Any]] = field(default_factory=list)
    working_state: WorkingStateProjection = field(default_factory=WorkingStateProjection)
    active_worker_id: str | None = None
    lease_epoch: int = 0

    @classmethod
    def from_projection(cls, projection: RunProjection) -> "_MutableProjection":
        return cls(
            run_id=projection.run_id,
            sequence=projection.sequence,
            last_event_id=projection.last_event_id,
            status=projection.status,
            goal=projection.goal,
            runtime_contract=projection.runtime_contract,
            runtime_contract_digest=projection.runtime_contract_digest,
            plan=projection.plan,
            discovery_status=projection.discovery_status,
            mutation_gate=projection.mutation_gate,
            actions={(item.action_id, item.attempt): item for item in projection.actions},
            observations={(item.action_id, item.attempt): item for item in projection.observations},
            approvals={item.approval_id: item for item in projection.approvals},
            todos={item.todo_id: item for item in projection.todos},
            queue={item.follow_up_run_id: item for item in projection.queue},
            children={item.child_run_id: item for item in projection.children},
            evidence=list(projection.evidence),
            progress=[dict(item) for item in projection.progress],
            working_state=projection.working_state,
            active_worker_id=projection.active_worker_id,
            lease_epoch=projection.lease_epoch,
        )

    def freeze(self) -> RunProjection:
        return RunProjection(
            run_id=self.run_id,
            sequence=self.sequence,
            last_event_id=self.last_event_id,
            status=self.status,
            goal=self.goal,
            runtime_contract=self.runtime_contract,
            runtime_contract_digest=self.runtime_contract_digest,
            plan=self.plan,
            discovery_status=self.discovery_status,
            mutation_gate=self.mutation_gate,
            actions=tuple(self.actions[key] for key in sorted(self.actions)),
            observations=tuple(self.observations[key] for key in sorted(self.observations)),
            approvals=tuple(self.approvals[key] for key in sorted(self.approvals)),
            todos=tuple(self.todos[key] for key in sorted(self.todos)),
            queue=tuple(self.queue[key] for key in sorted(self.queue)),
            children=tuple(self.children[key] for key in sorted(self.children)),
            evidence=tuple(self.evidence),
            progress=tuple(dict(item) for item in self.progress),
            working_state=self.working_state,
            active_worker_id=self.active_worker_id,
            lease_epoch=self.lease_epoch,
        )


class ProjectionBuilder:
    """Replays events into a deterministic, immutable RunProjection."""

    def rebuild(
        self,
        events: list[RunEvent] | tuple[RunEvent, ...],
        *,
        snapshot: RunProjection | None = None,
        run_id: str | None = None,
    ) -> RunProjection:
        if snapshot is not None:
            state = _MutableProjection.from_projection(snapshot)
            if run_id is not None and run_id != state.run_id:
                raise ProjectionError("snapshot run_id does not match requested run")
            selected = tuple(event for event in events if event.sequence > state.sequence)
            full_history = False
        else:
            inferred_run_id = run_id or (events[0].run_id if events else "")
            state = _MutableProjection(inferred_run_id)
            selected = tuple(events)
            full_history = True

        if not selected:
            return state.freeze()
        if not state.run_id:
            state.run_id = selected[0].run_id
        if full_history:
            EventValidator.validate_history(list(selected))
        for event in selected:
            if event.run_id != state.run_id:
                raise ProjectionError("event belongs to a different run")
            self._validate_increment(event, state)
            self._apply(event, state)
        return state.freeze()

    @staticmethod
    def _validate_increment(event: RunEvent, state: _MutableProjection) -> None:
        expected_runtime = state.runtime_contract_digest
        EventValidator.validate_event(
            event,
            expected_sequence=state.sequence + 1,
            expected_run_id=state.run_id,
            expected_runtime_contract_digest=expected_runtime,
            current_status=state.status,
            goal=state.goal,
        )
        if event.event_type == "PlanRevised" and state.discovery_status == "awaiting":
            raise ProjectionError("plan mutation is blocked while read-only discovery is active")
        if event.event_type == "ActionPlanned" and state.mutation_gate == "read_only":
            effect = str(event.payload.get("effect_class", EffectClass.UNKNOWN.value))
            if effect != EffectClass.READ_ONLY.value:
                raise ProjectionError("mutating action is blocked by the discovery mutation gate")

    def _apply(self, event: RunEvent, state: _MutableProjection) -> None:
        payload = event.payload
        target = None
        if event.event_type == "RunFailed":
            target = RunStatus(str(payload["target_status"]))
        elif event.event_type == "RunRuntimeMigrated":
            target = state.status
        try:
            if event.event_type == "CompletionAccepted":
                candidate = CompletionCandidate.from_dict(payload)
                state.status = RunStateMachine().transition(
                    state.status,
                    event.event_type,
                    completion=candidate,
                    goal=state.goal,
                )
            else:
                state.status = RunStateMachine().transition(
                    state.status,
                    event.event_type,
                    target=target,
                )
        except ValueError as exc:
            raise ProjectionError(str(exc)) from exc

        if event.event_type == "RunCreated":
            state.goal = GoalContract.from_dict(payload["goal"])
            state.runtime_contract = RuntimeContract.from_dict(payload["runtime_contract"])
            state.runtime_contract_digest = event.runtime_contract_digest
        elif event.event_type in {"GoalContractAccepted", "GoalContractRevised"}:
            state.goal = GoalContract.from_dict(payload["goal"])
        elif event.event_type == "RunRuntimeMigrated":
            state.runtime_contract_digest = str(payload["new_runtime_contract_digest"])
            state.runtime_contract = (
                RuntimeContract.from_dict(payload["new_runtime_contract"])
                if isinstance(payload.get("new_runtime_contract"), Mapping)
                else None
            )
        elif event.event_type == "PlanDiscoveryStarted":
            state.discovery_status = "awaiting"
            state.mutation_gate = "read_only"
        elif event.event_type == "PlanDiscoveryCompleted":
            state.discovery_status = "completed"
            state.mutation_gate = "open"
        elif event.event_type == "RunClaimed":
            state.active_worker_id = str(payload["worker_id"])
            state.lease_epoch = event.lease_epoch
        elif event.event_type == "WorkerHeartbeat":
            state.lease_epoch = max(state.lease_epoch, event.lease_epoch)
        elif event.event_type in {
            "RunResumed",
            "RunYielded",
            "WorkerLeaseExpired",
            "RunCancelled",
            "RunBlocked",
            "RunStalled",
        }:
            state.active_worker_id = None
        elif event.event_type == "PlanCreated" or event.event_type == "PlanRevised":
            state.plan = PlanGraph.from_dict(payload["plan"])
        elif event.event_type == "ToolObservationCommitted":
            self._apply_observation(event, state)
        elif event.event_type.startswith("Action"):
            self._apply_action(event, state)
        elif event.event_type.startswith("Reconciliation"):
            self._apply_reconciliation(event, state)
        elif event.event_type.startswith("Approval"):
            self._apply_approval(event, state)
            if event.event_type == "ApprovalRequested":
                state.active_worker_id = None
        elif event.event_type == "FollowUpQueued":
            state.queue[str(payload["follow_up_run_id"])] = QueueProjection(
                follow_up_run_id=str(payload["follow_up_run_id"]),
                predecessor_run_id=payload.get("predecessor_run_id"),
                message_artifact=str(payload["message_artifact"]),
                gate=str(payload.get("gate", "waiting")),
                queued_sequence=event.sequence,
            )
        elif event.event_type == "FollowUpStarted":
            follow_up_id = str(payload["follow_up_run_id"])
            item = state.queue.get(follow_up_id)
            if item is None:
                raise ProjectionError(f"follow-up event has no queued item: {follow_up_id}")
            state.queue[follow_up_id] = replace(item, status="started")
        elif event.event_type == "PredecessorBypassed":
            predecessor_id = str(payload["predecessor_run_id"])
            target_id = str(payload.get("follow_up_run_id") or "")
            for key, item in state.queue.items():
                if item.predecessor_run_id == predecessor_id and (
                    not target_id or key == target_id
                ):
                    state.queue[key] = replace(item, gate="bypassed")
        elif event.event_type.startswith("Child") or event.event_type == "IntegrationConflictRaised":
            self._apply_child(event, state)
        elif event.event_type in {"VerificationRecorded"}:
            evidence = self._parse_evidence(payload["evidence"])
            state.evidence.extend(evidence)
            state.working_state = replace(
                state.working_state,
                last_verification_sequence=event.sequence,
                last_verification_ok=bool(payload.get("ok", True)),
            )
        elif event.event_type == "CompletionAccepted":
            state.evidence.extend(self._parse_evidence(payload["evidence"]))
        elif event.event_type == "ProgressRecorded":
            progress = dict(payload["progress"])
            state.progress.append({"sequence": event.sequence, **progress})
        elif event.event_type in {"TodoCreated", "TodoUpdated"}:
            todo = self._parse_todo(payload["todo"], event.sequence)
            state.todos[todo.todo_id] = todo
        elif event.event_type == "TodoCompleted":
            todo_id = str(payload["todo_id"])
            previous = state.todos.get(todo_id)
            state.todos[todo_id] = replace(
                previous
                or TodoProjection(todo_id=todo_id, title=todo_id, status="pending"),
                status="done",
                updated_sequence=event.sequence,
            )
        state.sequence = event.sequence
        state.last_event_id = event.event_id

    @staticmethod
    def _apply_observation(event: RunEvent, state: _MutableProjection) -> None:
        payload = event.payload
        key = (str(payload["action_id"]), int(payload["attempt"]))
        if key in state.observations:
            raise ProjectionError(
                f"observation already committed: {payload['action_id']}/{payload['attempt']}"
            )
        if key not in state.actions:
            raise ProjectionError(
                f"observation has no planned action: {payload['action_id']}/{payload['attempt']}"
            )
        state.observations[key] = ToolObservationProjection(
            observation_id=str(payload["observation_id"]),
            action_id=key[0],
            attempt=key[1],
            tool_name=str(payload.get("tool_name") or state.actions[key].tool_name),
            observation_artifact=str(payload["observation_artifact"]),
            status=str(payload["status"]),
            complete=bool(payload["complete"]),
            next_cursor=(str(payload["next_cursor"]) if payload.get("next_cursor") else None),
            recovery=str(payload.get("recovery") or "none"),
            provenance=tuple(str(item) for item in payload.get("provenance") or ()),
        )

    def _apply_action(self, event: RunEvent, state: _MutableProjection) -> None:
        payload = event.payload
        action_id = str(payload["action_id"])
        attempt = int(payload["attempt"])
        key = (action_id, attempt)
        action = state.actions.get(key)
        if event.event_type == "ActionPlanned":
            if action is not None:
                raise ProjectionError(f"action already planned: {action_id}/{attempt}")
            state.actions[key] = ActionAttempt(
                action_id=action_id,
                run_id=event.run_id,
                tool_name=str(payload["tool_name"]),
                normalized_args_digest=str(payload.get("normalized_args_digest", "")),
                effect_class=str(payload["effect_class"]),
                contract_digest=str(payload.get("contract_digest", "")),
                actor_kind=event.actor.kind,
                actor_id=event.actor.actor_id,
                worker_id=str(payload.get("worker_id", event.actor.actor_id)),
                attempt=attempt,
                lease_epoch=event.lease_epoch,
                arguments_artifact=payload.get("arguments_artifact"),
            )
            return
        if action is None:
            raise ProjectionError(f"action event has no planned action: {action_id}/{attempt}")
        transitions = {
            "ActionPrepared": ActionStatus.PREPARED,
            "ActionStarted": ActionStatus.STARTED,
            "ActionSucceeded": ActionStatus.SUCCEEDED,
            "ActionFailed": ActionStatus.FAILED,
            "ActionCancelled": ActionStatus.CANCELLED,
            "ActionOutcomeUnknown": ActionStatus.OUTCOME_UNKNOWN,
        }
        if event.event_type == "ActionProgressRecorded":
            return
        next_status = transitions[event.event_type]
        updates: dict[str, Any] = {}
        if next_status is ActionStatus.SUCCEEDED:
            updates["result_artifact"] = payload.get("result_artifact")
            self._observe_action_paths(event, state, is_error=False)
        elif next_status is ActionStatus.STARTED:
            updates["started_at"] = _event_epoch(event.occurred_at)
        elif next_status is ActionStatus.FAILED:
            updates["error_kind"] = str(payload["error_kind"])
            self._observe_action_paths(event, state, is_error=True)
        elif next_status is ActionStatus.CANCELLED:
            self._observe_action_paths(event, state, is_error=True)
        elif next_status is ActionStatus.OUTCOME_UNKNOWN:
            updates["error_kind"] = str(payload.get("reason", "unknown"))
        state.actions[key] = action.advance(next_status, **updates)

    def _apply_reconciliation(self, event: RunEvent, state: _MutableProjection) -> None:
        if event.event_type != "ReconciliationResolved":
            return
        action_id = str(event.payload["action_id"])
        candidates = [key for key in state.actions if key[0] == action_id]
        if not candidates:
            raise ProjectionError(f"reconciliation has no action: {action_id}")
        key = max(candidates, key=lambda item: item[1])
        action = state.actions[key]
        resolved = ActionStatus(str(event.payload["resolved_status"]))
        if resolved not in {ActionStatus.SUCCEEDED, ActionStatus.FAILED}:
            raise ProjectionError("reconciliation must resolve to succeeded or failed")
        state.actions[key] = action.advance(
            resolved,
            result_artifact=event.payload.get("result_artifact"),
            error_kind=event.payload.get("error_kind"),
        )
    def _apply_approval(self, event: RunEvent, state: _MutableProjection) -> None:
        payload = event.payload
        approval_id = str(payload.get("approval_id", ""))
        if event.event_type == "ApprovalRequested":
            if approval_id in state.approvals:
                raise ProjectionError(f"approval already exists: {approval_id}")
            state.approvals[approval_id] = ApprovalProjection(
                approval_id=approval_id,
                run_id=event.run_id,
                action_id=str(payload["action_id"]),
                action_args_digest=str(payload["action_args_digest"]),
                scope=tuple(str(item) for item in payload["scope"]),
            )
            return
        approval = state.approvals.get(approval_id)
        if approval is None:
            raise ProjectionError(f"approval event has no request: {approval_id}")
        if event.event_type == "ApprovalGranted":
            if str(payload["action_args_digest"]) != approval.action_args_digest:
                raise ProjectionError("approval parameters changed after request")
            state.approvals[approval_id] = replace(
                approval,
                status=ApprovalStatus.GRANTED,
                decided_by=event.actor.actor_id,
                decided_at=event.occurred_at,
            )
        elif event.event_type == "ApprovalRejected":
            state.approvals[approval_id] = replace(
                approval,
                status=ApprovalStatus.REJECTED,
                decided_by=event.actor.actor_id,
                decided_at=event.occurred_at,
            )

    def _apply_child(self, event: RunEvent, state: _MutableProjection) -> None:
        payload = event.payload
        child_id = str(payload["child_run_id"])
        child = state.children.get(child_id)
        if event.event_type == "ChildRunCreated":
            state.children[child_id] = ChildProjection(
                child_run_id=child_id,
                node_id=payload.get("node_id"),
            )
        elif child is None:
            raise ProjectionError(f"child event has no child run: {child_id}")
        elif event.event_type == "ChildRunClaimed":
            state.children[child_id] = replace(child, status="running")
        elif event.event_type == "ChildCandidateSubmitted":
            state.children[child_id] = replace(
                child,
                status="candidate_submitted",
                candidate_commit=str(payload["candidate_commit"]),
                diff_digest=str(payload["diff_digest"]),
                modified_paths=tuple(str(item) for item in payload.get("modified_paths") or ()),
            )
        elif event.event_type == "ChildCandidateAccepted":
            state.children[child_id] = replace(
                child,
                status="accepted",
                accepted_sequence=event.sequence,
            )
        elif event.event_type == "ChildCandidateRejected":
            state.children[child_id] = replace(child, status="rejected")
        elif event.event_type == "IntegrationConflictRaised":
            state.children[child_id] = replace(child, status="blocked")

    @staticmethod
    def _parse_evidence(raw: Any) -> tuple[EvidenceRef, ...]:
        if not isinstance(raw, (list, tuple)):
            raise ProjectionError("evidence must be a list")
        try:
            return tuple(EvidenceRef.from_dict(item) for item in raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectionError("invalid evidence reference") from exc

    @staticmethod
    def _parse_todo(raw: Any, sequence: int) -> TodoProjection:
        if not isinstance(raw, Mapping):
            raise ProjectionError("todo must be an object")
        todo_id = str(raw.get("id", ""))
        title = str(raw.get("title", ""))
        if not todo_id or not title:
            raise ProjectionError("todo id and title are required")
        return TodoProjection(
            todo_id=todo_id,
            title=title,
            status=str(raw.get("status", "pending")),
            active_sessions=tuple(str(item) for item in raw.get("active_sessions") or ()),
            updated_sequence=sequence,
            evidence=ProjectionBuilder._parse_evidence(raw.get("evidence") or ()),
        )

    @staticmethod
    def _observe_action_paths(event: RunEvent, state: _MutableProjection, *, is_error: bool) -> None:
        payload = event.payload
        modified = set(state.working_state.modified_paths)
        read = set(state.working_state.read_paths)
        modified.update(str(item) for item in payload.get("modified_paths") or ())
        read.update(str(item) for item in payload.get("read_paths") or ())
        unresolved = list(state.working_state.unresolved_errors)
        if is_error:
            unresolved.append(
                {
                    "sequence": event.sequence,
                    "action_id": str(payload["action_id"]),
                    "kind": payload.get("error_kind", "unknown"),
                }
            )
        state.working_state = replace(
            state.working_state,
            modified_paths=tuple(sorted(modified)),
            read_paths=tuple(sorted(read)),
            last_mutation_sequence=(
                event.sequence if payload.get("modified_paths") else state.working_state.last_mutation_sequence
            ),
            unresolved_errors=tuple(unresolved),
        )


__all__ = [
    "ApprovalProjection",
    "ChildProjection",
    "ProjectionBuilder",
    "ProjectionError",
    "QueueProjection",
    "RunProjection",
    "ToolObservationProjection",
    "TodoProjection",
    "WorkingStateProjection",
]
