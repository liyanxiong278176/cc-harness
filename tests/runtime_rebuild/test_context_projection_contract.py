from __future__ import annotations

import pytest

from cc_harness.context_projection import (
    AdvisoryMemoryEvidence,
    ContextProjectionError,
    ContextProjector,
)
from cc_harness.run_projection import RunProjection


def test_context_projection_keeps_goal_and_memory_provenance_separate() -> None:
    projection = RunProjection.empty("run-1")
    memory = AdvisoryMemoryEvidence("m-1", "remember this", "session-1", "project-1", 1.0, 0.8)
    projected = ContextProjector().project(
        projection,
        [{"role": "tool", "artifact_ref": "sha256:tool"}],
        pinned_facts=("must stay local",),
        memory=(memory,),
    )
    assert projected.pinned_facts == ("must stay local",)
    assert projected.memory[0].source == "session-1"
    assert projected.tool_result_refs == ("sha256:tool",)
    assert projected.digest.startswith("sha256:")


def test_memory_without_provenance_is_rejected() -> None:
    with pytest.raises(ContextProjectionError):
        AdvisoryMemoryEvidence("m-1", "unsafe", "", "project-1", 1.0, 0.5)
