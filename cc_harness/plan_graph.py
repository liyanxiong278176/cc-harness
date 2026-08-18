"""Plan Graph command and scheduling helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .run_model import DomainValidationError, PlanGraph, PlanNode


class PlanGraphError(DomainValidationError):
    """Raised when a plan command violates graph or isolation rules."""


@dataclass(frozen=True)
class PlanBatch:
    node_ids: tuple[str, ...]
    parallel: bool


@dataclass(frozen=True)
class PlanRevision:
    previous_revision: int
    plan: PlanGraph
    reason: str


class PlanGraphService:
    """Pure plan operations used by the coordinator and worker scheduler."""

    def create(
        self,
        nodes: Iterable[PlanNode],
        *,
        max_concurrent_children: int = 3,
        max_child_depth: int = 2,
    ) -> PlanGraph:
        return PlanGraph(
            nodes=tuple(nodes),
            revision=1,
            max_concurrent_children=max_concurrent_children,
            max_child_depth=max_child_depth,
        )

    def revise(
        self,
        current: PlanGraph,
        nodes: Iterable[PlanNode],
        *,
        reason: str,
    ) -> PlanRevision:
        if not reason.strip():
            raise PlanGraphError("plan revision reason is required")
        revised = PlanGraph(
            nodes=tuple(nodes),
            revision=current.revision + 1,
            max_concurrent_children=current.max_concurrent_children,
            max_child_depth=current.max_child_depth,
        )
        return PlanRevision(current.revision, revised, reason)

    def ready_nodes(
        self,
        plan: PlanGraph,
        *,
        completed_nodes: Iterable[str] = (),
        active_nodes: Iterable[str] = (),
    ) -> tuple[PlanNode, ...]:
        completed = set(completed_nodes)
        active = set(active_nodes)
        return tuple(
            node
            for node in plan.nodes
            if node.node_id not in completed
            and node.node_id not in active
            and all(dependency in completed for dependency in node.depends_on)
        )

    def choose_batch(
        self,
        plan: PlanGraph,
        node_ids: Iterable[str],
        *,
        active_child_count: int = 0,
    ) -> PlanBatch:
        selected_ids = tuple(node_ids)
        try:
            selected = tuple(plan.by_id[node_id] for node_id in selected_ids)
        except KeyError as exc:
            raise PlanGraphError(f"unknown plan node: {exc.args[0]}") from exc
        children = sum(node.kind == "child" for node in selected)
        if active_child_count + children > plan.max_concurrent_children:
            raise PlanGraphError("maximum concurrent child runs exceeded")
        if not plan.can_run_parallel(selected_ids):
            raise PlanGraphError("selected nodes have dependency or file ownership conflict")
        return PlanBatch(selected_ids, parallel=len(selected_ids) > 1)

    def validate_child_depth(self, plan: PlanGraph, node_id: str) -> PlanNode:
        try:
            node = plan.by_id[node_id]
        except KeyError as exc:
            raise PlanGraphError(f"unknown plan node: {node_id}") from exc
        if node.kind == "child" and node.depth > plan.max_child_depth:
            raise PlanGraphError("child depth exceeds the plan limit")
        return node


__all__ = ["PlanBatch", "PlanGraphError", "PlanGraphService", "PlanRevision"]
