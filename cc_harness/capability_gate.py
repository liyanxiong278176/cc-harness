"""Runtime evidence gate for the durable capability cutover."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class CapabilityContinuityResult:
    eligible: bool
    required: tuple[str, ...]
    observed: Mapping[str, bool]
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "cc-harness.capability-continuity.v1",
            "eligible": self.eligible,
            "required": list(self.required),
            "observed": dict(self.observed),
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True)
class CapabilityGateReport:
    """Non-compensating per-capability evidence report."""

    passed: bool
    checks: Mapping[str, Mapping[str, Any]]
    blockers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "cc-harness.capability-gate-report.v1",
            "passed": self.passed,
            "checks": {name: dict(value) for name, value in self.checks.items()},
            "blockers": list(self.blockers),
        }


def run_capability_gate(
    events: Iterable[Any],
    *,
    capability_activation: Mapping[str, Mapping[str, Any]] | None = None,
    required: Iterable[str] = (
        "multi_round",
        "context",
        "offload",
        "retrieval",
        "memory",
        "tool",
        "safety",
        "scheduler",
        "handoff",
        "migration",
    ),
) -> CapabilityGateReport:
    """Run independent hard checks over one live durable evidence stream."""

    event_list = tuple(events)
    event_types = {str(getattr(event, "event_type", "")) for event in event_list}
    tool_names = {
        str(getattr(event, "payload", {}).get("tool_name", ""))
        for event in event_list
        if getattr(event, "event_type", "") == "ToolObservationCommitted"
    }
    checks: dict[str, dict[str, Any]] = {
        "multi_round": {
            "passed": sum(
                getattr(event, "event_type", "") == "ModelInvocationStarted"
                for event in event_list
            )
            >= 2
            or any(
                int(getattr(event, "payload", {}).get("round", 0)) >= 1
                for event in event_list
                if getattr(event, "event_type", "") == "ModelInvocationStarted"
            ),
            "evidence": ["ModelInvocationStarted"],
        },
        "context": {
            "passed": "ContextProjectionBuilt" in event_types,
            "evidence": ["ContextProjectionBuilt", "ContextCompacted"],
        },
        "offload": {
            "passed": any(
                bool(getattr(event, "payload", {}).get("offload_applied"))
                for event in event_list
            ),
            "evidence": ["ToolObservationCommitted.offload_applied"],
        },
        "retrieval": {
            "passed": "RecallRunContext" in tool_names
            or "read_ref" in tool_names
            or "search_ref" in tool_names,
            "evidence": sorted(tool_names & {"RecallRunContext", "read_ref", "search_ref"}),
        },
        "memory": {
            "passed": "MemoryCheckpointCommitted" in event_types,
            "evidence": ["MemoryCandidateRecorded", "MemoryCheckpointCommitted"],
        },
        "tool": {
            "passed": "ToolObservationCommitted" in event_types,
            "evidence": ["ToolObservationCommitted"],
        },
        "safety": {
            "passed": any(
                bool(getattr(event, "payload", {}).get("safety_applied"))
                for event in event_list
            ),
            "evidence": ["ToolObservationCommitted.safety_applied"],
        },
        "scheduler": {
            "passed": "PlanNodeStarted" in event_types
            or "PlanNodeCompleted" in event_types,
            "evidence": ["PlanNodeStarted", "PlanNodeCompleted"],
        },
        "handoff": {
            "passed": bool(
                event_types
                & {"PredecessorHandoffCommitted", "ChildDelegationCommitted"}
            ),
            "evidence": ["PredecessorHandoffCommitted", "ChildDelegationCommitted"],
        },
        "migration": {
            "passed": bool(event_types & {"LegacyRunImported", "RunRuntimeMigrated"}),
            "evidence": ["LegacyRunImported", "RunRuntimeMigrated"],
        },
    }
    blockers: list[str] = []
    for name in dict.fromkeys(str(item) for item in required):
        check = checks.setdefault(name, {"passed": False, "evidence": []})
        if not check["passed"]:
            blockers.append(name)
        activation = (capability_activation or {}).get(name)
        if activation and activation.get("degraded_reason"):
            blockers.append(f"{name}: {activation['degraded_reason']}")
    blockers = list(dict.fromkeys(blockers))
    return CapabilityGateReport(not blockers, checks, tuple(blockers))


def evaluate_capability_continuity(
    events: Iterable[Any],
    *,
    capability_activation: Mapping[str, Mapping[str, Any]] | None = None,
    required: Iterable[str] = ("agent_loop", "context", "tools"),
    memory_enabled: bool = False,
) -> CapabilityContinuityResult:
    """Require live durable evidence, not merely retained source code/tests."""

    required_names = list(dict.fromkeys(str(item) for item in required))
    if memory_enabled and "memory" not in required_names:
        required_names.append("memory")
    event_list = tuple(events)
    event_types = {str(getattr(event, "event_type", "")) for event in event_list}
    observed = {
        "agent_loop": "ModelInvocationStarted" in event_types,
        "context": "ContextProjectionBuilt" in event_types,
        "tools": "ToolObservationCommitted" in event_types,
        "memory": "MemoryCheckpointCommitted" in event_types,
        "safety": any(
            bool(getattr(event, "payload", {}).get("safety_applied"))
            or
            str(getattr(event, "actor", "")).find("safety") >= 0
            or str(getattr(event, "payload", {}).get("provenance", "")).find("safety") >= 0
            for event in event_list
        ),
        "mcp": any(
            str(getattr(event, "payload", {}).get("tool_name", "")).startswith("mcp__")
            for event in event_list
        ),
    }
    blockers = [name for name in required_names if not observed.get(name, False)]
    if capability_activation is not None:
        for name in required_names:
            state = capability_activation.get(name) or {}
            if state.get("enabled", True) and state.get("degraded_reason"):
                blockers.append(f"{name}: {state['degraded_reason']}")
            if state.get("enabled", True) and state.get("initialized") is False:
                blockers.append(f"{name}: not initialized")
    blockers = list(dict.fromkeys(blockers))
    return CapabilityContinuityResult(
        eligible=not blockers,
        required=tuple(required_names),
        observed=observed,
        blockers=tuple(blockers),
    )


__all__ = [
    "CapabilityContinuityResult",
    "CapabilityGateReport",
    "evaluate_capability_continuity",
    "run_capability_gate",
]
