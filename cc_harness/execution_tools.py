"""Task-control tools; changes are committed by the existing action plane."""
from __future__ import annotations

import json
import uuid
from typing import Any, Mapping

from .mcp_client import ToolResult
from .run_model import PlanNode


def _spec(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}}


TASK_PLAN_SPEC = _spec("task_plan", "Persist or revise task milestones, dependencies and evidence gates. "
    "In explicit plan mode this submits the plan for human approval; it does not grant permission.",
    {"nodes": {"type": "array", "items": {"type": "object", "properties": {
        "node_id": {"type": "string"}, "kind": {"type": "string", "enum": ["root", "stage"]},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "owned_paths": {"type": "array", "items": {"type": "string"}},
    }, "required": ["node_id", "kind", "acceptance_criteria"]}},
     "reason": {"type": "string"}}, ["nodes", "reason"])
TASK_WAIT_SPEC = _spec("task_wait", "Wait for user input or a known asynchronous event after committing "
    "the current tool observation. Do not poll repeatedly or declare completion while waiting.",
    {"kind": {"type": "string", "enum": ["waiting_input", "waiting_event"]},
     "reason": {"type": "string"}, "wait_id": {"type": "string"},
     "timeout_at": {"type": "number"}}, ["kind", "reason"])


async def task_plan(args: Mapping[str, Any], *, coordinator, run_id: str, **kwargs):
    try:
        nodes = tuple(PlanNode.from_dict(node) for node in args["nodes"])
        if not nodes:
            raise ValueError("plan needs at least one milestone")
        view = await coordinator.inspect(run_id)
        if view.projection.discovery_status == "awaiting":
            plan = await coordinator.complete_plan_discovery(run_id, nodes, reason=str(args["reason"]))
        else:
            plan = await coordinator.revise_plan(run_id, nodes, reason=str(args["reason"]))
        return ToolResult.success(json.dumps({"plan_digest": plan.digest,
            "requires_approval": view.projection.task.plan_approval_required}), source="task_control")
    except (ValueError, KeyError, TypeError) as exc:
        return ToolResult.error(str(exc), f"Plan rejected: {exc}")


async def task_wait(args: Mapping[str, Any], *, coordinator, run_id: str, **kwargs):
    try:
        wait_id = str(args.get("wait_id") or uuid.uuid4())
        await coordinator.execution.append(run_id, "TaskWaitRequested", {
            "wait_id": wait_id, "kind": str(args["kind"]), "reason": str(args["reason"]),
            "timeout_at": args.get("timeout_at"),
        })
        return ToolResult.success(json.dumps({"wait_id": wait_id, "waiting": True}), source="task_control")
    except (ValueError, KeyError, TypeError) as exc:
        return ToolResult.error(str(exc), f"Wait rejected: {exc}")
