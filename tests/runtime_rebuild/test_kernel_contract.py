from __future__ import annotations

import pytest

from cc_harness.run_kernel import (
    KernelProtocolError,
    ModelSegment,
    ReActKernel,
    SegmentContext,
)
from cc_harness.run_model import GoalContract, Run, RuntimeContract
from cc_harness.run_projection import RunProjection


class FakeModel:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def complete(self, messages, tools):
        self.calls.append((messages, tools))
        return self.response


def context() -> SegmentContext:
    goal = GoalContract("kernel task", ("tool planned",))
    contract = RuntimeContract("kernel-test", 1, "tools", "model", "policy", "cap")
    run = Run("00000000-0000-0000-0000-000000000401", goal, contract)
    return SegmentContext(
        run_id=run.run_id,
        projection=RunProjection.empty(run.run_id),
        messages=({"role": "user", "content": "inspect"},),
        available_tools=({"name": "Read"},),
        worker_id="worker-1",
    )


@pytest.mark.asyncio
async def test_kernel_returns_action_intent_without_persistence_side_effects() -> None:
    model = FakeModel(
        ModelSegment(
            text="I will inspect the file.",
            tool_calls=({"id": "action-1", "name": "Read", "arguments": {"path": "README.md"}},),
        )
    )
    outcome = await ReActKernel(model).execute_segment(context())
    assert outcome.action_requests[0].tool_name == "Read"
    assert outcome.event_intents[0].event_type == "ActionPlanned"
    assert model.calls[0][0][0]["role"] == "user"


@pytest.mark.asyncio
async def test_kernel_supports_json_arguments_and_completion_candidate() -> None:
    model = FakeModel(
        {
            "content": "done",
            "tool_calls": [],
            "completion_candidate": {
                "acceptance_criteria": ["tool planned"],
                "evidence": [
                    {
                        "evidence_id": "e-1",
                        "kind": "test",
                        "digest": "sha256:test",
                        "source": "pytest",
                        "recorded_at": 1.0,
                    }
                ],
            },
        }
    )
    outcome = await ReActKernel(model).execute_segment(context())
    assert outcome.completion_candidate is not None
    assert outcome.model_text == "done"


@pytest.mark.asyncio
async def test_kernel_rejects_malformed_tool_arguments_and_honors_cancel() -> None:
    model = FakeModel({"tool_calls": ({"name": "Read", "arguments": "not-json"},)})
    with pytest.raises(KernelProtocolError):
        await ReActKernel(model).execute_segment(context())
    cancelled = context()
    cancelled = SegmentContext(**{**cancelled.__dict__, "cancellation_requested": True})
    outcome = await ReActKernel(model).execute_segment(cancelled)
    assert outcome.stop_reason == "cancel_requested"
    assert len(model.calls) == 1
