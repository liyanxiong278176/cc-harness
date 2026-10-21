"""Normal, abnormal and boundary contracts; no network or provider required."""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_harness.artifacts import ArtifactStore
from cc_harness.context import _repair_tool_result_pairing, select_compaction_tier
from cc_harness.config import ContextConfig
from cc_harness.local_skills import LocalSkills, read_skill_handler
from cc_harness.memory.store import MemoryStore
from cc_harness.run_kernel import KernelProtocolError, ModelSegment, ReActKernel, SegmentContext
from cc_harness.run_projection import RunProjection


def segment_context(cancelled=False):
    return SegmentContext("matrix", RunProjection.empty("matrix"), (),
                          cancellation_requested=cancelled)


@pytest.mark.asyncio
async def test_runtime_normal_json_arguments_and_stable_generated_identity():
    model = SimpleNamespace(complete=AsyncMock(return_value={
        "tool_calls": [{"name": "Read", "arguments": '{"path":"sample.txt"}'}]}))
    kernel = ReActKernel(model)
    first = await kernel.execute_segment(segment_context())
    second = await kernel.execute_segment(segment_context())
    assert first.action_requests == second.action_requests
    assert first.action_requests[0].arguments == {"path": "sample.txt"}


@pytest.mark.asyncio
async def test_runtime_boundary_cancel_calls_no_model():
    model = SimpleNamespace(complete=AsyncMock())
    result = await ReActKernel(model).execute_segment(segment_context(True))
    assert result.stop_reason == "cancel_requested" and not result.action_requests
    model.complete.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, [], {"tool_calls": [None]},
    {"tool_calls": [42]}, {"tool_calls": ["bad"]},
    ModelSegment(tool_calls=(None,)),
    ModelSegment(tool_calls=None),
    {"tool_calls": {}}, {"tool_calls": ""}, {"tool_calls": 0},
    {"tool_calls": [{"name": ["Read"]}]},
    {"tool_calls": [{"name": "  "}]},
    {"tool_calls": [{"id": "same", "name": "Read"}, {"id": "same", "name": "Write"}]},
])
async def test_runtime_abnormal_protocol_is_rejected_as_protocol_error(response):
    model = SimpleNamespace(complete=AsyncMock(return_value=response))
    with pytest.raises(KernelProtocolError):
        await ReActKernel(model).execute_segment(segment_context())


def test_context_normal_reorders_results_without_changing_history():
    messages = [{"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
                {"role": "tool", "tool_call_id": "b", "content": "B"},
                {"role": "tool", "tool_call_id": "a", "content": "A"}]
    original = copy.deepcopy(messages)
    repaired = _repair_tool_result_pairing(messages)
    assert [m.get("tool_call_id") for m in repaired[1:]] == ["a", "b"]
    assert messages == original


@pytest.mark.parametrize("calls", [[None], [{"function": {"name": "Read"}}],
    [{"id": "a"}, {"id": "a"}], [{"id": "a"}, None]])
def test_context_abnormal_declarations_are_sanitized(calls):
    messages = [{"role": "assistant", "content": "inspect", "tool_calls": calls},
                {"role": "tool", "tool_call_id": "a", "content": "result"}]
    original = copy.deepcopy(messages)
    repaired = _repair_tool_result_pairing(messages)
    ids = [c["id"] for c in repaired[0].get("tool_calls", [])]
    assert len(ids) == len(set(ids))
    assert len(repaired) == 1 + len(ids)
    assert repaired[0]["content"] == "inspect" and messages == original


@pytest.mark.parametrize("threshold,tier", [("tier1_threshold", "snip"),
    ("tier2_threshold", "prune"), ("tier3_threshold", "summarize")])
def test_context_boundary_threshold_is_inclusive(threshold, tier):
    config = ContextConfig()
    assert select_compaction_tier(getattr(config, threshold), config).name.lower() == tier


@pytest.fixture
async def memory(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", 3)
    await store.init_schema()
    try:
        yield store
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_memory_normal_update_and_restart(memory):
    item = await memory.add("first", [1., 0., 0.], "pipeline")
    await memory.update(item.id, "second", [0., 1., 0.])
    await memory.close()
    await memory.init_schema()
    assert (await memory.get(item.id)).text == "second"
    assert (await memory.search_similar([0., 1., 0.], k=1))[0][0].id == item.id


@pytest.mark.asyncio
async def test_memory_abnormal_update_cannot_reintroduce_forgotten_source(memory):
    source = hashlib.sha256(b"user\x00private preference").hexdigest()
    secret = await memory.add("private preference", [1., 0., 0.], "pipeline",
                              provenance_json=json.dumps({"source_digest": source}))
    survivor = await memory.add("unrelated", [0., 1., 0.], "pipeline")
    await memory.forget(secret.id)
    with pytest.raises(ValueError, match="forgotten"):
        await memory.update(survivor.id, "paraphrased private preference", [1., 0., 0.],
                            provenance_json=json.dumps({"source_digest": source}))
    assert (await memory.get(survivor.id)).text == "unrelated"


@pytest.mark.asyncio
@pytest.mark.parametrize("k", [0, -1])
async def test_memory_boundary_nonpositive_retrieval_is_empty(memory, k):
    await memory.add("fact", [1., 0., 0.], "pipeline")
    await memory.add("another fact", [0., 1., 0.], "pipeline")
    assert await memory.search_similar([1., 0., 0.], k=k) == []
    assert await memory.search_fts("fact", k=k) == []


@pytest.fixture
def skills(tmp_path):
    root = tmp_path / ".cc-harness" / "skills" / "demo"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: demo\n---\nInstructions", encoding="utf-8")
    registry = LocalSkills(tmp_path, ArtifactStore(tmp_path / "objects"), tmp_path / "user")
    registry.configure("project:demo", True)
    return registry


def test_skill_normal_pins_bytes_across_edit_disable_and_restart(skills):
    digest, _ = skills.snapshot("task")
    path = skills.cwd / ".cc-harness" / "skills" / "demo" / "SKILL.md"
    path.write_text("changed", encoding="utf-8")
    skills.configure("project:demo", False)
    restarted = LocalSkills(skills.cwd, skills.artifacts, skills.user_root)
    assert restarted.read("task", "project:demo", tool_names=set())["content"].endswith("Instructions")
    assert restarted.snapshot("task")[0] == digest
    assert restarted.snapshot("new-task")[1]["skills"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{}, {"skill_id": "project:demo", "resource": []},
    {"skill_id": "project:demo", "resource": None},
    {"skill_id": ["project:demo"]}])
async def test_skill_abnormal_arguments_return_tool_error(skills, args):
    result = await read_skill_handler(args, cwd=str(skills.cwd), skills=skills,
                                      run_id="task", tool_names=set())
    assert result.is_error


@pytest.mark.parametrize("resource", ["../SKILL.md", "/SKILL.md", "scripts/../SKILL.md", ""])
def test_skill_boundary_resource_requires_exact_package_member(skills, resource):
    with pytest.raises(ValueError, match="resource"):
        skills.read("task", "project:demo", resource, tool_names=set())


def test_skill_abnormal_materialized_edit_is_detected(skills):
    result = skills.read("task", "project:demo", tool_names=set())
    from pathlib import Path
    Path(result["resource_path"]).write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        skills.read("task", "project:demo", tool_names=set())


@pytest.mark.parametrize("size,allowed", [(2_000_000, True), (2_000_001, False)])
def test_skill_boundary_resource_byte_budget(skills, size, allowed):
    path = skills.cwd / ".cc-harness" / "skills" / "demo" / "resource.txt"
    path.write_bytes(b"x" * size)
    if allowed:
        assert len(skills.read("task", "project:demo", "resource.txt", tool_names=set())["content"]) == size
    else:
        with pytest.raises(ValueError, match="budget"):
            skills.snapshot("task")
        # A rejected snapshot must not leave a partially pinned revision.
        path.unlink()
        assert skills.snapshot("task")[1]["revision"] == 1


def test_skill_abnormal_cross_task_snapshot_reuse_is_rejected(skills):
    digest, _ = skills.snapshot("task")
    with pytest.raises(ValueError, match="different task"):
        skills.read("other-task", "project:demo", tool_names=set(), pinned_digest=digest)


def test_skill_abnormal_missing_environment_is_visible_and_read_is_blocked(skills, monkeypatch):
    monkeypatch.delenv("CC_HARNESS_MATRIX_REQUIREMENT", raising=False)
    path = skills.cwd / ".cc-harness" / "skills" / "demo" / "SKILL.md"
    path.write_text("---\nname: demo\nrequired_environment: [CC_HARNESS_MATRIX_REQUIREMENT]\n---\nbody", encoding="utf-8")
    item = skills.catalog("task", set())[1]["skills"][0]
    assert item["missing_environment"] == ["CC_HARNESS_MATRIX_REQUIREMENT"]
    assert not item["available"]
    with pytest.raises(ValueError, match="environment"):
        skills.read("task", "project:demo", tool_names=set())
