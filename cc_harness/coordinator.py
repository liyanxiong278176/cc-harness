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
from .durable_subagents import ChildTaskSpec
from .worktrees import ChildWorktree, WorktreeManager, WorktreeError


def _scope_path(value: str) -> str:
    return value.replace("\\", "/").strip().strip("/") or "."


def _scope_contains(path: str, roots: tuple[str, ...]) -> bool:
    candidate = _scope_path(path)
    return not roots or any(
        root == "." or candidate == root or candidate.startswith(f"{root}/")
        for root in (_scope_path(item) for item in roots)
    )


def _scope_overlaps(path: str, roots: tuple[str, ...]) -> bool:
    candidate = _scope_path(path)
    return any(
        root == "."
        or candidate == root
        or candidate.startswith(f"{root}/")
        or root.startswith(f"{candidate}/")
        for root in (_scope_path(item) for item in roots)
    )


def _looks_like_run_id(value: str) -> bool:
    """Return whether a context reference is a durable Run identifier."""

    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _validate_parent_context_refs(parent_run_id: str, spec: ChildTaskSpec) -> None:
    """Keep child context parent-coordinated; sibling Run refs are forbidden.

    ``context_refs`` may contain artifact/checkpoint identifiers and the parent
    Run id.  A child Run id is deliberately rejected: child-to-child
    communication is not part of the v1 protocol and all cross-child facts
    must be summarized by the parent into an artifact or a later handoff.
    """

    for ref in spec.context_refs:
        if ref == parent_run_id:
            continue
        if _looks_like_run_id(ref) or ref.startswith(("run:", "child:")):
            raise ValueError(
                f"child {spec.node_id} context_refs may reference the parent or "
                "artifacts only; child peer communication is disabled"
            )


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
    def __init__(
        self,
        store: RunStore,
        *,
        artifacts: ArtifactStore | None = None,
        worktrees: WorktreeManager | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts or store.artifacts
        self.approvals = ApprovalService(store)
        self.followups = FollowUpService(store, artifacts=self.artifacts)
        self.goals = GoalContractService()
        self.plans = PlanGraphService()
        self.worktrees = worktrees

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
        if view.status in {RunStatus.RUNNING, RunStatus.QUEUED, RunStatus.AWAITING_APPROVAL}:
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
            RunStatus.CANCELLED,
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
        if view.status is RunStatus.CANCEL_REQUESTED:
            # A live worker owns the action boundary.  Do not force a terminal
            # event while it may still be committing ToolObservationCommitted
            # / ActionOutcomeUnknown; that would hide an uncertain side effect
            # and make a later continuation unsafe.  A worker (or lease
            # reclaimer) will append RunCancelled after the boundary.  Runs
            # which have no lease can be finalized immediately.
            if await self.store.current_lease(run_id) is not None:
                return ControlReceipt(run_id, view.status, view.sequence)
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
        spec = ChildTaskSpec(
            node_id=node_id,
            objective=objective,
            acceptance_criteria=tuple(acceptance_criteria),
            required=True,
            effect_class=EffectClass.READ_ONLY,
        )
        receipts = await self.create_children(parent_run_id, (spec,), depth=depth)
        return receipts[0]

    async def create_children(
        self,
        parent_run_id: str,
        specs: tuple[ChildTaskSpec, ...],
        *,
        depth: int | None = None,
    ) -> tuple[ChildReceipt, ...]:
        """Persist a dynamic child DAG before any child worker can run."""
        if not specs:
            raise ValueError("at least one child task is required")
        # Ten is the hard runtime fan-out ceiling.  The parent plan may use a
        # smaller limit, but it must never widen the safety boundary.
        if len(specs) > 10:
            raise ValueError("maximum concurrent child runs exceeded")
        parent = await self.inspect(parent_run_id)
        if parent.projection.discovery_status == "awaiting":
            raise ValueError("child delegation is blocked until plan discovery completes")
        if parent.projection.status in {RunStatus.CANCELLED, RunStatus.COMPLETED, RunStatus.FAILED_TERMINAL}:
            raise ValueError("cannot dispatch children from a terminal parent Run")
        for spec in specs:
            _validate_parent_context_refs(parent_run_id, spec)
        graph = PlanGraph(
            nodes=parent.projection.plan.nodes,
            revision=parent.projection.plan.revision,
            max_concurrent_children=parent.projection.plan.max_concurrent_children,
            max_child_depth=parent.projection.plan.max_child_depth,
        )
        if len(specs) > graph.max_concurrent_children:
            raise ValueError("maximum concurrent child runs exceeded")
        existing_ids = set(graph.by_id)
        if len({spec.node_id for spec in specs}) != len(specs):
            raise ValueError("child node_id values must be unique")
        existing_specs = tuple(spec for spec in specs if spec.node_id in existing_ids)
        if existing_specs:
            # Dispatch is idempotent across retries and process restarts.  A
            # caller may re-submit the same dynamic contract after Ctrl+C,
            # but a mixed existing/new batch would otherwise create a partial
            # DAG, so reject that shape explicitly.
            if len(existing_specs) != len(specs):
                raise ValueError("child dispatch batch mixes persisted and new node ids")
            receipts: list[ChildReceipt] = []
            for spec in specs:
                node = graph.by_id[spec.node_id]
                if not node.child_run_id:
                    raise ValueError(f"persisted child node {spec.node_id} has no child run")
                if depth is not None and depth != node.depth:
                    raise ValueError(f"persisted child depth mismatch for {spec.node_id}")
                child_projection = await self.store.load_projection(node.child_run_id)
                if child_projection.goal is None or child_projection.goal.objective != spec.objective:
                    raise ValueError(f"persisted child contract mismatch for {spec.node_id}")
                if tuple(child_projection.goal.acceptance_criteria) != tuple(spec.acceptance_criteria):
                    raise ValueError(f"persisted child acceptance mismatch for {spec.node_id}")
                if node.required != spec.required:
                    raise ValueError(f"persisted child required flag mismatch for {spec.node_id}")
                if node.effect_class != spec.effect_class.value:
                    raise ValueError(f"persisted child effect class mismatch for {spec.node_id}")
                if tuple(node.depends_on) != tuple(spec.depends_on):
                    raise ValueError(f"persisted child dependency mismatch for {spec.node_id}")
                if tuple(node.owned_paths) != tuple(spec.owned_paths):
                    raise ValueError(f"persisted child scope mismatch for {spec.node_id}")
                events = await self.store.read(parent_run_id, limit=100_000)
                delegation_event = next(
                    (
                        event
                        for event in events.events
                        if event.event_type == "ChildDelegationCommitted"
                        and event.payload.get("child_run_id") == node.child_run_id
                    ),
                    None,
                )
                if delegation_event is None:
                    raise ValueError(f"persisted child contract is unavailable for {spec.node_id}")
                try:
                    raw_contract = json.loads(
                        self.artifacts.read_text(
                            str(delegation_event.payload["delegation_artifact"])
                        )
                    )
                    persisted_spec = ChildTaskSpec.from_dict(raw_contract["task_contract"])
                except (KeyError, TypeError, ValueError, OSError) as exc:
                    raise ValueError(
                        f"persisted child contract cannot be reconstructed for {spec.node_id}"
                    ) from exc
                if persisted_spec.digest != spec.digest:
                    raise ValueError(f"persisted child contract mismatch for {spec.node_id}")
                receipts.append(ChildReceipt(parent_run_id, node.child_run_id, spec.node_id))
            return tuple(receipts)
        parent_depth = await self._run_depth(parent_run_id)
        expected_depth = parent_depth + 1
        child_depth = depth if depth is not None else expected_depth
        if child_depth != expected_depth:
            raise ValueError(f"child depth must be exactly parent depth + 1 ({expected_depth})")
        if child_depth < 1 or child_depth > graph.max_child_depth:
            raise ValueError(f"child depth must be in [1, {graph.max_child_depth}]")
        contract = parent.projection.runtime_contract
        if contract is None:
            raise ValueError("parent has no pinned runtime contract")
        parent_goal = parent.projection.goal
        parent_allowed = tuple(parent_goal.allowed_scope if parent_goal else ())
        parent_excluded = tuple(parent_goal.excluded_scope if parent_goal else ())
        effective_scopes: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        for spec in specs:
            if parent_allowed and spec.allowed_scope:
                outside = tuple(
                    path for path in spec.allowed_scope
                    if not _scope_contains(path, parent_allowed)
                )
                if outside:
                    raise ValueError(
                        f"child {spec.node_id} allowed_scope exceeds parent scope: {outside}"
                    )
            allowed = tuple(spec.allowed_scope or parent_allowed)
            excluded = tuple(dict.fromkeys((*parent_excluded, *spec.excluded_scope)))
            if any(_scope_overlaps(path, excluded) for path in spec.owned_paths):
                raise ValueError(f"child {spec.node_id} owned_paths intersects excluded_scope")
            if allowed and any(not _scope_contains(path, allowed) for path in spec.owned_paths):
                raise ValueError(f"child {spec.node_id} owned_paths exceeds allowed_scope")
            effective_scopes[spec.node_id] = (allowed, excluded)
        if any(spec.effect_class is EffectClass.WORKSPACE_MUTATION for spec in specs) and self.worktrees is None:
            raise ValueError("mutating child requires a WorktreeManager for isolated execution")
        prepared_node_ids = {spec.node_id for spec in specs}
        if any(
            dependency not in graph.by_id and dependency not in prepared_node_ids
            for spec in specs
            for dependency in spec.depends_on
        ):
            missing = sorted({
                dependency
                for spec in specs
                for dependency in spec.depends_on
                if dependency not in graph.by_id and dependency not in prepared_node_ids
            })
            raise ValueError(f"child dependency refers to missing node(s): {', '.join(missing)}")
        # Validate the proposed DAG before allocating any Git worktree.  A
        # cycle or duplicate dependency must not leave filesystem state behind.
        PlanGraph(
            nodes=tuple(graph.nodes)
            + tuple(
                PlanNode(
                    spec.node_id,
                    "child",
                    depth=child_depth,
                    depends_on=spec.depends_on,
                    owned_paths=spec.owned_paths,
                    required=spec.required,
                    effect_class=spec.effect_class.value,
                    acceptance_criteria=spec.acceptance_criteria,
                    budget=spec.budget,
                    timeout_seconds=spec.timeout_seconds,
                    max_retries=spec.max_retries,
                    output_schema=spec.output_schema,
                )
                for spec in specs
            ),
            revision=graph.revision + 1,
            max_concurrent_children=graph.max_concurrent_children,
            max_child_depth=graph.max_child_depth,
        )
        actor = EventActor("coordinator", "local-coordinator")
        prepared: list[tuple[ChildTaskSpec, str, ChildWorktree | None]] = []
        try:
            for spec in specs:
                child_id = str(uuid.uuid4())
                worktree = None
                if self.worktrees is not None:
                    worktree = self.worktrees.create_child(
                        parent_run_id,
                        child_id,
                        depth=child_depth,
                        write=spec.effect_class is EffectClass.WORKSPACE_MUTATION,
                    )
                    if spec.effect_class is EffectClass.WORKSPACE_MUTATION and not worktree.isolated:
                        raise ValueError("mutating child requires an isolated Git worktree")
                prepared.append((spec, child_id, worktree))
        except Exception:
            # A failed preflight must not leave a partially persisted child DAG.
            for _spec, _child_id, worktree in prepared:
                if worktree is not None and worktree.isolated:
                    try:
                        self.worktrees.remove(worktree)
                    except Exception:
                        pass
            raise
        nodes = list(graph.nodes)
        for spec, child_id, worktree in prepared:
            dependencies = tuple(spec.depends_on)
            nodes.append(
                PlanNode(
                    spec.node_id,
                    "child",
                    depth=child_depth,
                    depends_on=dependencies,
                    owned_paths=spec.owned_paths,
                    child_run_id=child_id,
                    worktree_id=str(worktree.path) if worktree and worktree.isolated else None,
                    required=spec.required,
                    effect_class=spec.effect_class.value,
                    acceptance_criteria=spec.acceptance_criteria,
                    worktree_base_commit=worktree.base_commit if worktree else None,
                    budget=spec.budget,
                    timeout_seconds=spec.timeout_seconds,
                    max_retries=spec.max_retries,
                    output_schema=spec.output_schema,
                )
            )
        plan = PlanGraph(
            nodes=tuple(nodes),
            revision=graph.revision + 1,
            max_concurrent_children=graph.max_concurrent_children,
            max_child_depth=graph.max_child_depth,
        )
        await self._append(parent_run_id, "PlanRevised", {"plan": plan.to_dict(), "reason": "durable dynamic child dispatch"}, actor)
        receipts: list[ChildReceipt] = []
        parent_scope = parent.projection.goal
        for spec, child_id, worktree in prepared:
            allowed_scope, excluded_scope = effective_scopes[spec.node_id]
            goal = GoalContract(
                spec.objective,
                spec.acceptance_criteria or (("return a structured report",) if not spec.required else ()),
                constraints=(f"child_depth={child_depth}", f"effect_class={spec.effect_class.value}"),
                allowed_scope=allowed_scope,
                excluded_scope=excluded_scope,
                required_evidence=spec.acceptance_criteria,
            )
            delegation_payload = {
                "schema_version": "cc-harness.child-delegation.v2",
                "parent_run_id": parent_run_id,
                "child_run_id": child_id,
                "task_contract": spec.to_dict(),
                "depth": child_depth,
                "parent_sequence": parent.projection.sequence,
                "authorized_context_refs": list(spec.context_refs),
                "parent_allowed_scope": list(parent_scope.allowed_scope if parent_scope else ()),
                "parent_excluded_scope": list(parent_scope.excluded_scope if parent_scope else ()),
                "worktree": {
                    "path": str(worktree.path) if worktree and worktree.isolated else None,
                    "base_commit": worktree.base_commit if worktree else None,
                    "isolated": bool(worktree and worktree.isolated),
                },
            }
            delegation = self.artifacts.put_text(
                json.dumps(delegation_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                media_type="application/json; purpose=child-delegation",
            )
            await self.store.create_run(Run(child_id, goal, contract, parent_run_id=parent_run_id))
            await self._append(parent_run_id, "ChildRunCreated", {
                "child_run_id": child_id,
                "node_id": spec.node_id,
                "depth": child_depth,
                "required": spec.required,
                "effect_class": spec.effect_class.value,
                "acceptance_criteria": list(spec.acceptance_criteria),
            }, actor)
            await self._append(parent_run_id, "ChildDelegationCommitted", {"child_run_id": child_id, "delegation_artifact": delegation.digest, "node_id": spec.node_id}, actor, artifact_refs=(delegation.digest,))
            await self._append(child_id, "RunCreated", {"goal": goal.to_dict(), "runtime_contract": contract.to_dict()}, actor)
            await self._append(child_id, "PredecessorHandoffCommitted", {
                "predecessor_run_id": parent_run_id,
                "handoff_artifact": delegation.digest,
                "authorized_recall": [parent_run_id, *spec.context_refs],
                "node_id": spec.node_id,
            }, actor, artifact_refs=(delegation.digest,))
            child_plan = PlanGraph(
                nodes=(PlanNode(
                    spec.node_id,
                    "child",
                    depth=child_depth,
                    owned_paths=spec.owned_paths,
                    worktree_id=str(worktree.path) if worktree and worktree.isolated else None,
                    required=spec.required,
                    effect_class=spec.effect_class.value,
                    acceptance_criteria=goal.acceptance_criteria,
                    worktree_base_commit=worktree.base_commit if worktree else None,
                    budget=spec.budget,
                    timeout_seconds=spec.timeout_seconds,
                    max_retries=spec.max_retries,
                    output_schema=spec.output_schema,
                ),),
                revision=1,
            )
            await self._append(child_id, "PlanCreated", {"plan": child_plan.to_dict()}, actor)
            await self._append(child_id, "TodoCreated", {"todo": {"id": spec.node_id, "title": spec.objective, "status": "pending", "active_sessions": [child_id], "plan_node_id": spec.node_id}}, actor)
            await self._append(child_id, "GoalContractAccepted", {"goal": goal.to_dict()}, actor)
            await self._append(child_id, "RunQueued", {}, actor)
            receipts.append(ChildReceipt(parent_run_id, child_id, spec.node_id))
        return tuple(receipts)

    async def _run_depth(self, run_id: str) -> int:
        """Resolve a run's own DAG depth without using already-added siblings."""

        record = await self.store.load_run_record(run_id)
        if not record.parent_run_id:
            return 0
        parent = await self.store.load_projection(record.parent_run_id)
        child = next(
            (item for item in parent.children if item.child_run_id == run_id),
            None,
        )
        if child is None:
            raise ValueError(f"parent projection has no child record for {run_id}")
        node = next(
            (item for item in parent.plan.nodes if item.node_id == child.node_id),
            None,
        )
        if node is None:
            raise ValueError(f"parent plan has no child node for {run_id}")
        return node.depth

    async def submit_child_candidate(self, parent_run_id: str, candidate: CandidateChangeSet) -> None:
        await self._append(
            parent_run_id,
            "ChildCandidateSubmitted",
            candidate.to_dict(),
            EventActor("child", candidate.child_run_id),
        )

    async def accept_child_candidate(self, parent_run_id: str, child_run_id: str) -> dict[str, Any]:
        parent = await self.inspect(parent_run_id)
        if parent.status in {RunStatus.CANCELLED, RunStatus.COMPLETED, RunStatus.FAILED_TERMINAL}:
            raise ValueError("cannot accept a child candidate for a terminal parent Run")
        child = next((item for item in parent.projection.children if item.child_run_id == child_run_id), None)
        if child is None or child.status != "candidate_submitted":
            raise ValueError("child candidate is not awaiting acceptance")
        node = next((item for item in parent.projection.plan.nodes if item.node_id == child.node_id), None)
        if node is None or node.effect_class != EffectClass.WORKSPACE_MUTATION.value:
            raise ValueError("only mutating child candidates require integration")
        if self.worktrees is None or not node.worktree_id or not node.worktree_base_commit:
            raise ValueError("candidate worktree metadata is unavailable")
        from .run_model import CandidateChangeSet
        candidate = CandidateChangeSet(
            child_run_id=child_run_id,
            base_commit=node.worktree_base_commit,
            candidate_commit=str(child.candidate_commit),
            diff_digest=str(child.diff_digest),
            modified_paths=tuple(child.modified_paths),
        )
        try:
            result = self.worktrees.integrate_candidate(candidate, integration_root=self.store.project_root)
        except WorktreeError as exc:
            await self._append(parent_run_id, "IntegrationConflictRaised", {"child_run_id": child_run_id, "paths": list(child.modified_paths), "reason": str(exc)}, EventActor("coordinator", "local-coordinator"))
            raise ValueError(str(exc)) from exc
        if not result.accepted:
            await self._append(parent_run_id, "IntegrationConflictRaised", {"child_run_id": child_run_id, "paths": list(result.conflict_paths), "reason": result.reason or "integration conflict"}, EventActor("coordinator", "local-coordinator"))
            raise ValueError(result.reason or "integration conflict")
        await self._append(
            parent_run_id,
            "ChildCandidateAccepted",
            {"child_run_id": child_run_id},
            EventActor("coordinator", "local-coordinator"),
        )
        return {"child_run_id": child_run_id, "status": "accepted", "candidate_commit": candidate.candidate_commit}

    async def reject_child_candidate(self, parent_run_id: str, child_run_id: str, reason: str) -> None:
        parent = await self.inspect(parent_run_id)
        if parent.status in {RunStatus.CANCELLED, RunStatus.COMPLETED, RunStatus.FAILED_TERMINAL}:
            raise ValueError("cannot reject a child candidate for a terminal parent Run")
        if not reason.strip():
            raise ValueError("candidate rejection reason is required")
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
