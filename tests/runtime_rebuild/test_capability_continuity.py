from __future__ import annotations

from types import SimpleNamespace

from cc_harness.capability_gate import evaluate_capability_continuity


def test_capability_gate_requires_live_events_and_activation() -> None:
    events = [
        SimpleNamespace(event_type="ModelInvocationStarted", payload={}, actor="worker"),
        SimpleNamespace(event_type="ContextProjectionBuilt", payload={}, actor="worker"),
        SimpleNamespace(event_type="ToolObservationCommitted", payload={}, actor="worker"),
        SimpleNamespace(event_type="MemoryCheckpointCommitted", payload={}, actor="worker"),
    ]
    result = evaluate_capability_continuity(
        events,
        required=("agent_loop", "context", "tools", "memory"),
        memory_enabled=True,
        capability_activation={
            name: {"enabled": True, "initialized": True}
            for name in ("agent_loop", "context", "tools", "memory")
        },
    )
    assert result.eligible is True


def test_capability_gate_does_not_accept_source_only_evidence() -> None:
    result = evaluate_capability_continuity([], required=("agent_loop", "context", "tools"))
    assert result.eligible is False
    assert set(result.blockers) == {"agent_loop", "context", "tools"}
