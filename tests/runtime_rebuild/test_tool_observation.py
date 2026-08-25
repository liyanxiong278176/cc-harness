from __future__ import annotations

from cc_harness.tool_observation import (
    ContinueToolResult,
    ToolObservation,
    make_observation,
)


def test_incomplete_observation_round_trips_with_explicit_continuation() -> None:
    observation = make_observation(
        action_id="read-1",
        attempt=1,
        tool_name="Read",
        status="succeeded",
        effect_class="read_only",
        text="first page",
        complete=False,
        next_cursor="21",
        metadata={"cursor_kind": "offset"},
    )

    restored = ToolObservation.from_dict(observation.to_dict())
    assert restored.digest == observation.digest
    assert restored.continuation == ContinueToolResult(observation.observation_id, "21")
    message = restored.as_model_message()
    assert message["role"] == "tool"
    assert message["_cc_harness_untrusted"] is True
    assert '"complete": false' in message["content"]
