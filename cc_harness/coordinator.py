"""Client-facing RunCoordinator seam."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from .approvals import ApprovalDecision, ApprovalService
from .artifacts import ArtifactStore
from .followups import FollowUpService
from .goals import GoalAssessment, GoalContractService
from .run_events import EventActor, RunEvent
from .run_model import (
    CandidateChangeSet,
    EffectClass,
    GoalContract,
    PlanGraph,
    PlanNode,
    Run,
    RuntimeContract,
    RunStatus,
)
from .plan_graph import PlanGraphService
from .run_projection import RunProjection
from .run_store import RunStore


@dataclass(frozen=True)
class RunRequest:
    objective: str
    acceptance_criteria: tuple[str, ...]
    constraints: tuple[str, ...] = ()
    allowed_scope: tuple[str, ...] = ()
    excluded_scope: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    runtime_contract: RuntimeContract | None = None
    plan_discovery: bool = False
    # This is intentionally not inferred from task text.  Only an isolated
    # official benchmark adapter may opt into this provenance, so words such
    # as "push" in a fixture do not trigger the user-facing high-risk gate.
    goal_provenance: str = "user"


@dataclass(frozen=True)
class RunHandle:
    run_id: str


@dataclass(frozen=True)
class RunView:
    run_id: str
    status: RunStatus
    sequence: int
    projection: RunProjection


@dataclass(frozen=True)
class QueueReceipt:
    run_id: str
    sequence: int
    message_artifact: str
    follow_up_run_id: str | None = None


@dataclass(frozen=True)
class ControlReceipt:
    run_id: str
    status: RunStatus
    sequence: int


@dataclass(frozen=True)
class ChildReceipt:
    parent_run_id: str
    child_run_id: str
    node_id: str


class RunCoordinator:
    def __init__(self, store: RunStore, *, artifacts: ArtifactStore | None = None) -> None:
        self.store = store
        self.artifacts = artifacts or store.artifacts
        self.approvals = ApprovalService(store)
        self.followups = FollowUpService(store, artifacts=self.artifacts)
        self.goals = GoalContractService()
        self.plans = PlanGraphService()

    async def submit(self, request: RunRequest) -> RunHandle:
        run_id = str(uuid.uuid4())
        contract = request.runtime_contract or RuntimeContract(
            "runtime-rebuild-v1", 1, "sha256:tools", "sha256:model", "sha256:policy", "sha256:cap"
        )
        goal = GoalContract(
            request.objective,
            request.acceptance_criteria,
            constraints=request.constraints,
            allowed_scope=request.allowed_scope,
            excluded_scope=request.excluded_scope,
            required_evidence=request.required_evidence,
        )
        assessment = self.goals.assess(goal, goal_provenance=request.goal_provenance)
        await self.store.create_run(Run(run_id, goal, contract))
        actor = EventActor("client", "local-client")
        await self._append(
            run_id,
            "RunCreated",
            {
                "goal": goal.to_dict(),
                "runtime_contract": contract.to_dict(),
                "goal_provenance": request.goal_provenance,
            },
            actor,
        )
        root_node = PlanNode(f"root-{run_id}", "root", depth=0)
        root_plan = PlanGraph(nodes=(root_node,), revision=1)
        await self._append(run_id, "PlanCreated", {"plan": root_plan.to_dict()}, actor)
        await self._append(
            run_id,
            "TodoCreated",
            {
                "todo": {
                    "id": root_node.node_id,
                    "title": request.objective,
                    "status": "pending",
                    "active_sessions": [run_id],
                    "plan_node_id": root_node.node_id,
                }
            },
            actor,
        )
        if request.plan_discovery:
            await self._append(
                run_id,
                "PlanDiscoveryStarted",
                {
                    "discovery_id": f"discovery-{run_id}",
                    "mutation_gate": "read_only",
                    "reason": "complex goal requires an explicit dependency plan",
                },
                actor,
            )
        if assessment.accepted:
            await self._append(run_id, "GoalContractAccepted", {"goal": goal.to_dict()}, actor)
            await self._append(run_id, "RunQueued", {}, actor)
        else:
            await self._append(
                run_id,
                "RunBlocked",
                {
                    "reason": "goal_requires_decision",
                    "questions": list(assessment.questions),
                    "reasons": list(assessment.reasons),
                },
                actor,
            )
        return RunHandle(run_id)

    async def assess_goal(self, request: RunRequest) -> GoalAssessment:
        goal = GoalContract(
            request.objective,
            request.acceptance_criteria,
            constraints=request.constraints,
            allowed_scope=request.allowed_scope,
            excluded_scope=request.excluded_scope,
            required_evidence=request.required_evidence,
        )
        return self.goals.assess(goal)

    async def inspect(self, run_id: str) -> RunView:
        projection = await self.store.load_projection(run_id)
        return RunView(run_id, projection.status, projection.sequence, projection)

    async def interrupt(self, run_id: str, reason: str) -> ControlReceipt:
        view = await self.inspect(run_id)
        actor = EventActor("client", "local-client")
        if view.status in {RunStatus.RUNNING, RunStatus.QUEUED}:
            await self._append(run_id, "InterruptRequested", {"reason": reason}, actor)
        updated = await self.inspect(run_id)
        return ControlReceipt(run_id, updated.status, updated.sequence)

    async def resume(self, run_id: str, reason: str = "client resume") -> ControlReceipt:
        view = await self.inspect(run_id)
        if view.status in {
            RunStatus.STALLED,
            RunStatus.BLOCKED,
            RunStatus.FAILED_RECOVERABLE,
            RunStatus.WAITING_ON_PREDECESSOR,
        }:
            await self._append(run_id, "RunResumed", {"reason": reason}, EventActor("client", "local-client"))
        updated = await self.inspect(run_id)
        return ControlReceipt(run_id, updated.status, updated.sequence)

    async def cancel(self, run_id: str, reason: str) -> ControlReceipt:
        view = await self.inspect(run_id)
        actor = EventActor("client", "local-client")
        if view.status is RunStatus.RUNNING:
            await self._append(run_id, "InterruptRequested", {"reason": reason}, actor)
            # Let the owning worker finish its current action boundary and
            # append the cancellation fact. Writing RunCancelled immediately
            # would race a started action and fence its terminal outcome.
            updated = await self.inspect(run_id)
            return ControlReceipt(run_id, updated.status, updated.sequence)
        view = await self.inspect(run_id)
        if view.status not in {RunStatus.CANCELLED, RunStatus.COMPLETED, RunStatus.FAILED_TERMINAL}:
            await self._append(run_id, "RunCancelled", {"reason": reason}, actor)
        updated = await self.inspect(run_id)
        return ControlReceipt(run_id, updated.status, updated.sequence)

    async def rollback(self, run_id: str, reason: str = "rollback requested") -> ControlReceipt:
        view = await self.inspect(run_id)
        if view.status is RunStatus.RUNNING:
            await self._append(run_id, "InterruptRequested", {"reason": reason}, EventActor("client", "local-client"))
        view = await self.inspect(run_id)
        if view.status not in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED_TERMINAL}:
            await self._append(run_id, "RunBlocked", {"reason": "rollback_requested", "detail": reason}, EventActor("client", "local-client"))
        updated = await self.inspect(run_id)
        return ControlReceipt(run_id, updated.status, updated.sequence)

    async def approve(
        self,
        *,
        run_id: str,
        approval_id: str,
        action_args_digest: str,
    ) -> ApprovalDecision:
        return await self.approvals.grant(
            run_id=run_id,
            approval_id=approval_id,
            action_args_digest=action_args_digest,
            actor=EventActor("client", "local-client"),
        )

    async def reject(self, *, run_id: str, approval_id: str, reason: str) -> ApprovalDecision:
        return await self.approvals.reject(
            run_id=run_id,
            approval_id=approval_id,
            reason=reason,
            actor=EventActor("client", "local-client"),
        )

    async def list(self, statuses: set[str] | None = None) -> tuple[RunView, ...]:
        records = await self.store.list_runs(statuses)
        views = [await self.inspect(record.run_id) for record in records]
        return tuple(views)

    async def create_plan(
        self,
        run_id: str,
        nodes: tuple[PlanNode, ...],
        *,
        max_concurrent_children: int = 3,
        max_child_depth: int = 2,
    ) -> PlanGraph:
        plan = self.plans.create(
            nodes,
            max_concurrent_children=max_concurrent_children,
            max_child_depth=max_child_depth,
        )
        await self._append(run_id, "PlanCreated", {"plan": plan.to_dict()}, EventActor("coordinator", "local-coordinator"))
        for node in plan.nodes:
            await self._append(
                run_id,
                "TodoCreated",
                {
                    "todo": {
                        "id": node.node_id,
                        "title": node.node_id,
                        "status": "pending",
                        "active_sessions": [run_id],
                        "plan_node_id": node.node_id,
                    }
                },
                EventActor("coordinator", "local-coordinator"),
            )
        projection = await self.store.load_projection(run_id)
        if projection.discovery_status == "awaiting":
            await self._append(
                run_id,
                "PlanDiscoveryCompleted",
                {
                    "discovery_id": f"discovery-{run_id}",
                    "result": "accepted",
                    "plan_digest": plan.digest,
                    "mutation_gate": "open",
                },
                EventActor("coordinator", "local-coordinator"),
            )
        return plan

    async def complete_plan_discovery(
        self,
        run_id: str,
        nodes: tuple[PlanNode, ...],
        *,
        max_concurrent_children: int = 3,
        max_child_depth: int = 2,
        reason: str = "read-only plan discovery completed",
    ) -> PlanGraph:
        """Commit a dependency plan and open mutations in one durable boundary."""

        projection = await self.store.load_projection(run_id)
        if projection.discovery_status != "awaiting":
            raise ValueError("run is not waiting for plan discovery")
        if any(
            action.effect_class != "read_only"
            and action.effect_class != EffectClass.READ_ONLY
            for action in projection.actions
        ):
            raise ValueError("plan discovery cannot complete after a mutating action")
        plan = self.plans.create(
            nodes,
            max_concurrent_children=max_concurrent_children,
            max_child_depth=max_child_depth,
        )
        await self._append(
            run_id,
            "PlanCreated",
            {"plan": plan.to_dict(), "discovery_reason": reason},
            EventActor("coordinator", "local-coordinator"),
        )
        await self._append(
            run_id,
            "PlanDiscoveryCompleted",
            {
                "discovery_id": f"discovery-{run_id}",
                "result": "accepted",
                "plan_digest": plan.digest,
                "mutation_gate": "open",
            },
            EventActor("coordinator", "local-coordinator"),
        )
        return plan

    async def revise_plan(self, run_id: str, nodes: tuple[PlanNode, ...], *, reason: str) -> PlanGraph:
        view = await self.inspect(run_id)
        if view.projection.discovery_status == "awaiting":
            raise ValueError("plan mutation is blocked while discovery is active")
        if any(
            action.status.value in {"started", "succeeded"}
            and action.effect_class not in {"read_only", EffectClass.READ_ONLY}
            for action in view.projection.actions
        ):
            raise ValueError("plan mutation requires a quiescent run after mutating actions")
        revision = self.plans.revise(view.projection.plan, nodes, reason=reason)
        await self._append(
            run_id,
            "PlanRevised",
            {"plan": revision.plan.to_dict(), "reason": reason},
            EventActor("coordinator", "local-coordinator"),
        )
        return revision.plan

    async def create_child(
        self,
        parent_run_id: str,
        *,
        node_id: str,
        objective: str,
        acceptance_criteria: tuple[str, ...],
        depth: int = 1,
    ) -> ChildReceipt:
        parent = await self.inspect(parent_run_id)
        if parent.projection.discovery_status == "awaiting":
            raise ValueError("child delegation is blocked until plan discovery completes")
        self.plans.validate_child_depth(
            PlanGraph(
                nodes=parent.projection.plan.nodes,
                revision=parent.projection.plan.revision,
                max_concurrent_children=parent.projection.plan.max_concurrent_children,
                max_child_depth=parent.projection.plan.max_child_depth,
            ),
            node_id,
        )
        active_children = sum(item.status in {"running", "candidate_submitted"} for item in parent.projection.children)
        if active_children >= parent.projection.plan.max_concurrent_children:
            raise ValueError("maximum concurrent child runs exceeded")
        child_id = str(uuid.uuid4())
        contract = parent.projection.runtime_contract
        if contract is None:
            raise ValueError("parent has no pinned runtime contract")
        goal = GoalContract(objective, acceptance_criteria, constraints=(f"child_depth={depth}",))
        await self.store.create_run(
            Run(child_id, goal, contract, parent_run_id=parent_run_id)
        )
        await self._append(
            parent_run_id,
            "ChildRunCreated",
            {"child_run_id": child_id, "node_id": node_id, "depth": depth},
            EventActor("coordinator", "local-coordinator"),
        )
        delegation = self.artifacts.put_text(
            json.dumps(
                {
                    "schema_version": "cc-harness.child-delegation.v1",
                    "parent_run_id": parent_run_id,
                    "child_run_id": child_id,
                    "node_id": node_id,
                    "objective": objective,
                    "acceptance_criteria": list(acceptance_criteria),
                    "depth": depth,
                    "allowed_scope": list(parent.projection.goal.allowed_scope if parent.projection.goal else ()),
                    "excluded_scope": list(parent.projection.goal.excluded_scope if parent.projection.goal else ()),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            media_type="application/json; purpose=child-delegation",
        )
        await self._append(
            parent_run_id,
            "ChildDelegationCommitted",
            {"child_run_id": child_id, "delegation_artifact": delegation.digest, "node_id": node_id},
            EventActor("coordinator", "local-coordinator"),
            artifact_refs=(delegation.digest,),
        )
        await self._append(child_id, "RunCreated", {"goal": goal.to_dict(), "runtime_contract": contract.to_dict()}, EventActor("coordinator", "local-coordinator"))
        await self._append(
            child_id,
            "PredecessorHandoffCommitted",
            {
                "predecessor_run_id": parent_run_id,
                "handoff_artifact": delegation.digest,
                "authorized_recall": [parent_run_id],
                "node_id": node_id,
            },
            EventActor("coordinator", "local-coordinator"),
            artifact_refs=(delegation.digest,),
        )
        child_plan = PlanGraph(nodes=(PlanNode(node_id, "child", depth=depth),), revision=1)
        await self._append(child_id, "PlanCreated", {"plan": child_plan.to_dict()}, EventActor("coordinator", "local-coordinator"))
        await self._append(
            child_id,
            "TodoCreated",
            {
                "todo": {
                    "id": node_id,
                    "title": objective,
                    "status": "pending",
                    "active_sessions": [child_id],
                    "plan_node_id": node_id,
                }
            },
            EventActor("coordinator", "local-coordinator"),
        )
        await self._append(child_id, "GoalContractAccepted", {"goal": goal.to_dict()}, EventActor("coordinator", "local-coordinator"))
        await self._append(child_id, "RunQueued", {}, EventActor("coordinator", "local-coordinator"))
        return ChildReceipt(parent_run_id, child_id, node_id)

    async def submit_child_candidate(self, parent_run_id: str, candidate: CandidateChangeSet) -> None:
        await self._append(
            parent_run_id,
            "ChildCandidateSubmitted",
            candidate.to_dict(),
            EventActor("child", candidate.child_run_id),
        )

    async def accept_child_candidate(self, parent_run_id: str, child_run_id: str) -> None:
        await self._append(
            parent_run_id,
            "ChildCandidateAccepted",
            {"child_run_id": child_run_id},
            EventActor("coordinator", "local-coordinator"),
        )

    async def reject_child_candidate(self, parent_run_id: str, child_run_id: str, reason: str) -> None:
        await self._append(
            parent_run_id,
            "ChildCandidateRejected",
            {"child_run_id": child_run_id, "reason": reason},
            EventActor("coordinator", "local-coordinator"),
        )

    async def send(self, run_id: str, message: str) -> QueueReceipt:
        receipt = await self.followups.enqueue(run_id, message)
        item = await self.store.load_projection(run_id)
        queued = next(item for item in item.queue if item.follow_up_run_id == receipt.follow_up_run_id)
        return QueueReceipt(run_id, receipt.sequence, queued.message_artifact, receipt.follow_up_run_id)

    async def _append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        actor: EventActor,
        *,
        artifact_refs: tuple[str, ...] = (),
    ) -> RunEvent:
        projection = await self.store.load_projection(run_id)
        runtime_contract_digest = str(projection.runtime_contract_digest or "")
        if event_type == "RunCreated" and not runtime_contract_digest:
            runtime_contract_digest = RuntimeContract.from_dict(payload["runtime_contract"]).digest
        event = RunEvent.create(
            run_id=run_id,
            sequence=projection.sequence + 1,
            event_type=event_type,
            actor=actor,
            runtime_contract_digest=runtime_contract_digest,
            payload=payload,
            lease_epoch=projection.lease_epoch if actor.kind == "worker" else 0,
            artifact_refs=artifact_refs,
        )
        return await self.store.append(event, expected_sequence=projection.sequence)


__all__ = [
    "ChildReceipt",
    "ControlReceipt",
    "QueueReceipt",
    "RunCoordinator",
    "RunHandle",
    "RunRequest",
    "RunView",
]
