"""Deterministic task control, separate from worker scheduling and context storage."""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping


class TaskLifecycle(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_EVENT = "waiting_event"
    PAUSED = "paused"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_TASKS = frozenset({"completed", "failed", "cancelled"})
BUDGET_DIMENSIONS = frozenset({
    "steps", "tokens", "cost", "active_seconds", "tool_calls", "model_calls",
    "risk_actions", "concurrent_children",
})


def budget_values(values: Mapping[str, Any]) -> dict[str, float]:
    if not isinstance(values, Mapping):
        raise ValueError("budget values must be an object")
    result = {}
    for key, raw in values.items():
        if key not in BUDGET_DIMENSIONS:
            raise ValueError(f"unknown budget dimension: {key}")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"budget {key} must be a finite nonnegative number")
        value = float(raw)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"budget {key} must be a finite nonnegative number")
        result[key] = value
    return result


@dataclass(frozen=True)
class TaskControl:
    lifecycle: TaskLifecycle = TaskLifecycle.CREATED
    scheduling: str = "idle"
    reason: str = ""
    pause_requested: bool = False
    goal_revision: int = 1
    guidance: tuple[dict[str, Any], ...] = ()
    limits: Mapping[str, float] = field(default_factory=dict)
    used: Mapping[str, float] = field(default_factory=dict)
    metering: Mapping[str, str] = field(default_factory=dict)
    reservations: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    wait: Mapping[str, Any] = field(default_factory=dict)
    owner_terminal: Mapping[str, Any] = field(default_factory=dict)
    plan_approval_required: bool = False
    approved_plan_digest: str | None = None
    protocol_version: int = 0
    owner: str = "main"

    def to_dict(self) -> dict[str, Any]:
        return {
            "lifecycle": self.lifecycle.value, "scheduling": self.scheduling,
            "reason": self.reason, "pause_requested": self.pause_requested,
            "goal_revision": self.goal_revision, "guidance": list(self.guidance),
            "limits": dict(self.limits), "used": dict(self.used),
            "metering": dict(self.metering),
            "reservations": dict(self.reservations), "wait": dict(self.wait),
            "owner_terminal": dict(self.owner_terminal),
            "plan_approval_required": self.plan_approval_required,
            "approved_plan_digest": self.approved_plan_digest,
            "protocol_version": self.protocol_version,
            "owner": self.owner,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TaskControl:
        return cls(
            lifecycle=TaskLifecycle(data.get("lifecycle", "created")),
            scheduling=str(data.get("scheduling", "idle")), reason=str(data.get("reason", "")),
            pause_requested=bool(data.get("pause_requested", False)),
            goal_revision=int(data.get("goal_revision", 1)),
            guidance=tuple(dict(x) for x in data.get("guidance", ())),
            limits=budget_values(data.get("limits", {})), used=budget_values(data.get("used", {})),
            metering=dict(data.get("metering", {})),
            reservations=dict(data.get("reservations", {})), wait=dict(data.get("wait", {})),
            owner_terminal=dict(data.get("owner_terminal", {})),
            plan_approval_required=bool(data.get("plan_approval_required", False)),
            approved_plan_digest=data.get("approved_plan_digest"),
            protocol_version=int(data.get("protocol_version", 0)),
            owner=str(data.get("owner", "main")),
        )

    def unavailable(self, amounts: Mapping[str, float]) -> tuple[str, ...]:
        amounts = budget_values(amounts)
        reserved: dict[str, float] = {}
        for item in self.reservations.values():
            for key, value in item["amounts"].items():
                reserved[key] = reserved.get(key, 0) + value
        for key, limit in self.limits.items():
            if key != "concurrent_children" and self.used.get(key, 0) >= limit:
                amounts.setdefault(key, 0.001)
        return tuple(key for key, value in amounts.items() if key in self.limits and (
            self.used.get(key, 0) + reserved.get(key, 0) + value > self.limits[key]
        ))

    def apply(self, event: str, payload: Mapping[str, Any], sequence: int) -> TaskControl:
        """Apply only committed events; replay contains no clocks or external reads."""
        state = self
        lifecycle = {
            "RunCreated": "created", "RunClaimed": "running", "RunResumed": "running",
            "ApprovalRequested": "waiting_approval", "ApprovalGranted": "running",
            "ApprovalRejected": "running", "RunStalled": "paused", "RunBlocked": "waiting_input",
            "TaskApprovalSuperseded": "running",
            "RunCancelled": "cancelled", "RunFailed": "failed", "CompletionAccepted": "completed",
            "RunPaused": "paused", "TaskWaitStarted": str(payload.get("kind", "waiting_event")),
            "TaskWaitResolved": "running", "TaskVerificationStarted": "verifying",
            "TaskVerificationFinished": "running",
        }.get(event)
        if lifecycle:
            state = replace(state, lifecycle=TaskLifecycle(lifecycle), reason=str(payload.get("reason", "")))
        scheduling = {
            "RunQueued": "queued", "RunResumed": "queued", "RunClaimed": "leased",
            "RunYielded": "queued", "WorkerLeaseExpired": "idle", "RunPaused": "idle",
            "ApprovalRequested": "idle", "RunBlocked": "idle", "RunStalled": "idle",
            "RunCancelled": "idle", "RunFailed": "idle", "CompletionAccepted": "idle",
            "TaskWaitStarted": "idle", "TaskWaitResolved": "queued",
            "TaskApprovalSuperseded": "queued",
        }.get(event)
        if scheduling:
            state = replace(state, scheduling=scheduling)
        if event == "TaskControlEnabled":
            state = replace(state, protocol_version=1)
        elif event == "RunCreated" and payload.get("task_protocol_version"):
            state = replace(state, protocol_version=int(payload["task_protocol_version"]))
        elif event == "TaskPauseRequested":
            state = replace(state, pause_requested=True, reason=str(payload["reason"]))
        elif event == "RunPaused":
            state = replace(state, pause_requested=False)
        elif event == "RunResumed":
            state = replace(state, pause_requested=False, wait={})
        elif event == "TaskBudgetConfigured":
            state = replace(state, limits=budget_values(payload["limits"]))
        elif event == "TaskBudgetReserved":
            reservations = dict(state.reservations)
            key = str(payload["reservation_id"])
            if key in reservations:
                raise ValueError("duplicate budget reservation")
            amounts = budget_values(payload["amounts"])
            if state.unavailable(amounts):
                raise ValueError("budget reservation exceeds remaining allowance")
            reservations[key] = {"owner_run_id": str(payload["owner_run_id"]), "amounts": amounts}
            state = replace(state, reservations=reservations)
        elif event == "TaskBudgetSettled":
            reservations = dict(state.reservations)
            if str(payload["reservation_id"]) not in reservations:
                raise ValueError("budget settlement has no reservation")
            reservations.pop(str(payload["reservation_id"]))
            used = dict(state.used)
            metering = dict(state.metering)
            for key, amount in budget_values(payload["actual"]).items():
                used[key] = used.get(key, 0) + amount
                if key in payload.get("estimated_dimensions", ()):
                    metering[key] = "conservative_estimate"
                else:
                    metering.setdefault(key, "reported")
            state = replace(state, reservations=reservations, used=used, metering=metering)
        elif event == "TaskGuidanceReceived":
            entry = {"guidance_id": str(payload["guidance_id"]), "text": str(payload["text"]),
                     "revision": state.goal_revision + 1, "sequence": sequence,
                     "affected_children": list(payload.get("affected_children", ())), "applied": False}
            state = replace(state, guidance=(*state.guidance, entry), goal_revision=state.goal_revision + 1)
        elif event == "TaskGuidanceApplied" or (event == "GoalContractRevised" and "guidance_ids" in payload):
            ids = set(payload["guidance_ids"])
            if not ids.issubset({x["guidance_id"] for x in state.guidance}):
                raise ValueError("unknown guidance acknowledgement")
            state = replace(state, guidance=tuple(
                {**x, "applied": True} if x["guidance_id"] in ids else x for x in state.guidance
            ))
        elif event == "TaskWaitRequested":
            state = replace(state, wait={**dict(payload), "requested": True})
        elif event == "TaskWaitStarted":
            state = replace(state, wait=dict(payload))
        elif event == "TaskWaitResolved":
            if payload["wait_id"] != state.wait.get("wait_id"):
                raise ValueError("event does not match current wait")
            state = replace(state, wait={})
        elif event == "TaskTerminalAttached":
            state = replace(state, owner_terminal=dict(payload))
        elif event == "TaskHandoffRecorded":
            if str(payload["from_owner"]) != state.owner:
                raise ValueError("handoff sender is not the current task owner")
            state = replace(state, owner=str(payload["to_owner"]))
        elif event == "TaskPlanApprovalRequired":
            state = replace(state, plan_approval_required=True, approved_plan_digest=None)
        elif event == "TaskPlanApproved":
            state = replace(state, approved_plan_digest=str(payload["plan_digest"]))
        elif event == "PlanRevised" and state.plan_approval_required:
            state = replace(state, approved_plan_digest=None)
        return state


CONTROL_EVENT_FIELDS = {
    "TaskControlEnabled": (), "TaskPauseRequested": ("reason",), "RunPaused": ("reason",),
    "TaskBudgetConfigured": ("limits",),
    "TaskBudgetReserved": ("reservation_id", "owner_run_id", "amounts"),
    "TaskBudgetSettled": ("reservation_id", "actual"),
    "TaskGuidanceReceived": ("guidance_id", "text"),
    "TaskGuidanceApplied": ("guidance_ids",),
    "TaskWaitStarted": ("wait_id", "kind", "reason"), "TaskWaitResolved": ("wait_id",),
    "TaskWaitRequested": ("wait_id", "kind", "reason"),
    "TaskTerminalAttached": ("pid", "identity"),
    "TaskPlanApprovalRequired": (), "TaskPlanApproved": ("plan_digest",),
    "TaskApprovalSuperseded": ("approval_id", "reason"),
    "TaskVerificationStarted": (), "TaskVerificationFinished": ("passed",),
    "TaskHandoffRecorded": ("from_owner", "to_owner", "continuation_artifact"),
    "TaskKernelPhase": ("phase",),
    "TaskProgressRequested": ("question",), "TaskProgressReported": ("text",),
}


def validate_control_payload(event: str, payload: Mapping[str, Any]) -> None:
    if event not in CONTROL_EVENT_FIELDS:
        return
    missing = set(CONTROL_EVENT_FIELDS[event]) - payload.keys()
    if missing:
        raise ValueError(f"{event} missing fields: {sorted(missing)}")
    for key in set(CONTROL_EVENT_FIELDS[event]) - {"limits", "amounts", "actual", "guidance_ids", "passed", "pid"}:
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise ValueError(f"{event}.{key} must be a nonempty string")
    if "guidance_ids" in payload and (
        not isinstance(payload["guidance_ids"], (tuple, list))
        or not all(isinstance(value, str) and value for value in payload["guidance_ids"])
    ):
        raise ValueError("guidance_ids must be a list of strings")
    if "passed" in payload and not isinstance(payload["passed"], bool):
        raise ValueError("verification passed must be a boolean")
    if "pid" in payload and (isinstance(payload["pid"], bool) or not isinstance(payload["pid"], int) or payload["pid"] <= 0):
        raise ValueError("terminal pid must be a positive integer")
    deadline = payload.get("timeout_at")
    if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)):
        raise ValueError("wait deadline must be a finite timestamp")
    for key in ("limits", "amounts", "actual"):
        if key in payload:
            budget_values(payload[key])
    if event in {"TaskWaitStarted", "TaskWaitRequested"} and payload["kind"] not in {
        "waiting_input", "waiting_approval", "waiting_event",
    }:
        raise ValueError("invalid wait kind")
    if event == "TaskKernelPhase" and payload["phase"] not in {
        "prepare", "model", "act", "observe", "verify",
    }:
        raise ValueError("invalid kernel phase")
