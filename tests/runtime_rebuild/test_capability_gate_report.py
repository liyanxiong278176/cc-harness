from __future__ import annotations

from types import SimpleNamespace

from cc_harness.capability_gate import run_capability_gate


def _event(event_type: str, **payload):
    return SimpleNamespace(event_type=event_type, payload=payload)


def test_gate_report_keeps_each_capability_non_compensating() -> None:
    events = [
        _event("ModelInvocationStarted", round=0),
        _event("ModelInvocationStarted", round=1),
        _event("ContextProjectionBuilt"),
        _event(
            "ToolObservationCommitted",
            tool_name="RecallRunContext",
            offload_applied=True,
            safety_applied=True,
        ),
        _event("MemoryCheckpointCommitted"),
        _event("PlanNodeStarted"),
        _event("PredecessorHandoffCommitted"),
        _event("LegacyRunImported"),
    ]

    report = run_capability_gate(events)

    assert report.passed is True
    assert report.blockers == ()
    assert report.to_dict()["checks"]["offload"]["passed"] is True


def test_gate_report_does_not_compensate_for_missing_safety() -> None:
    events = [
        _event("ModelInvocationStarted", round=0),
        _event("ModelInvocationStarted", round=1),
        _event("ContextProjectionBuilt"),
        _event("ToolObservationCommitted", tool_name="RecallRunContext"),
        _event("MemoryCheckpointCommitted"),
        _event("PlanNodeStarted"),
        _event("PredecessorHandoffCommitted"),
        _event("LegacyRunImported"),
    ]

    report = run_capability_gate(events)

    assert report.passed is False
    assert "safety" in report.blockers
