import pytest

from cc_harness.plan_graph import PlanGraphError, PlanGraphService
from cc_harness.run_model import PlanNode


def test_plan_revision_and_ready_nodes_preserve_dag_constraints() -> None:
    service = PlanGraphService()
    plan = service.create(
        [
            PlanNode("read", "action", owned_paths=("src",)),
            PlanNode("write", "child", depends_on=("read",), depth=1, owned_paths=("src",)),
            PlanNode("docs", "child", depth=1, owned_paths=("docs",)),
        ]
    )
    assert [node.node_id for node in service.ready_nodes(plan)] == ["read", "docs"]
    assert [node.node_id for node in service.ready_nodes(plan, completed_nodes=("read",))] == ["write", "docs"]
    revised = service.revise(plan, plan.nodes, reason="retain the verified graph")
    assert revised.plan.revision == 2


def test_plan_batch_rejects_ownership_and_child_concurrency_conflicts() -> None:
    service = PlanGraphService()
    plan = service.create(
        [
            PlanNode("a", "child", owned_paths=("src",)),
            PlanNode("b", "child", owned_paths=("src/api",)),
            PlanNode("c", "child", owned_paths=("docs",)),
        ],
        max_concurrent_children=2,
    )
    with pytest.raises(PlanGraphError):
        service.choose_batch(plan, ("a", "b"))
    with pytest.raises(PlanGraphError):
        service.choose_batch(plan, ("a", "c"), active_child_count=2)
