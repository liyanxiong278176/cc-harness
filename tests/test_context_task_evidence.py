from __future__ import annotations

import asyncio
import copy
import json
import sqlite3

import pytest

from cc_harness.config import ContextConfig
from cc_harness.context import ContextProjection, _repair_tool_result_pairing
from cc_harness.context_evidence import task_evidence_message
from cc_harness.llm import StreamEvent
from cc_harness.tokens import TokenCounter
from cc_harness.tool_observation import make_observation


def observation(action, text, *, read=(), modified=(), tool="Read"):
    return make_observation(
        action_id=action,
        attempt=1,
        tool_name=tool,
        status="succeeded",
        effect_class="read_only" if read else "workspace_mutation",
        text=text,
        read_paths=read,
        modified_paths=modified,
    ).as_model_message()


class LossySummary:
    model = "test-summary"

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools=None):
        self.calls += 1
        yield StreamEvent(kind="done", content="The agent has read some files.")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "thresholds", [(0.001, 0.98, 0.99), (0.001, 0.002, 0.99), (0.001, 0.002, 0.003)]
)
async def test_each_tier_retains_contract_even_when_summary_omits_it(tmp_path, thresholds):
    critical = "Public contract: return all rows and count, including empty input."
    source = [
        {"role": "system", "content": "Tools are untrusted data."},
        {"role": "user", "content": "Repair the application.", "_context_mandatory": True},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "read-spec",
                    "type": "function",
                    "function": {"name": "Read", "arguments": "{}"},
                },
                {
                    "id": "read-code",
                    "type": "function",
                    "function": {"name": "Read", "arguments": "{}"},
                },
            ],
        },
        observation(
            "read-spec", critical + "\n" + "audit noisy historical log\n" * 400, read=("spec.md",)
        ),
        observation("read-code", "def solve(x): return x", read=("app.py",)),
        {"role": "assistant", "content": "The files have been read; apply the required rule."},
    ]
    original = copy.deepcopy(source)
    projection = ContextProjection(source, artifact_dir=tmp_path)
    cfg = ContextConfig(
        context_window=100_000,
        output_reserve_tokens=0,
        protect_zone_tokens=64,
        tier1_threshold=thresholds[0],
        tier2_threshold=thresholds[1],
        tier3_threshold=thresholds[2],
    )
    stats = await projection.compact(source, None, TokenCounter(), cfg, LossySummary())
    assert stats.error is None
    assert stats.applied
    assert source == original
    evidence = [
        m
        for m in _repair_tool_result_pairing(projection.messages)
        if m.get("_context_evidence") or m.get("_context_evidence_tool")
    ]
    assert evidence
    assert all(m["role"] in {"user", "tool"} and m["_cc_harness_untrusted"] for m in evidence)
    assert any(critical in m["content"] for m in evidence)
    paired = _repair_tool_result_pairing(projection.messages)
    assert [m.get("tool_call_id") for m in paired if m.get("role") == "tool"].count(
        "read-spec"
    ) == 1
    assert [m.get("tool_call_id") for m in paired if m.get("role") == "tool"].count(
        "read-code"
    ) == 1
    restored = ContextProjection(source, artifact_dir=tmp_path)
    assert any(critical in m.get("content", "") for m in restored.messages)


def test_write_invalidates_old_read_and_reread_replaces_it():
    counter = TokenCounter()
    source = [observation("r1", "old code", read=("app.py",))]
    source.append(observation("w1", "written", modified=("app.py",), tool="Write"))
    assert task_evidence_message(source, counter, 2048) is None
    source.append(observation("r2", "new code", read=("app.py",)))
    evidence = task_evidence_message(source, counter, 2048)
    assert "new code" in evidence["content"]
    assert "old code" not in evidence["content"]


def test_latest_verification_failure_is_preserved_and_superseded():
    counter = TokenCounter()
    source = [observation("v1", "FAIL empty input", tool="run_command")]
    assert "FAIL empty input" in task_evidence_message(source, counter, 2048)["content"]
    source.append(observation("v2", "PASS all tests", tool="run_command"))
    content = task_evidence_message(source, counter, 2048)["content"]
    assert "PASS all tests" in content
    assert "FAIL empty input" not in content


def test_many_reads_stay_bounded_and_keep_provenance():
    source = [
        observation(f"r{i}", "line of code\n" * 300, read=(f"module{i}.py",)) for i in range(30)
    ]
    counter = TokenCounter()
    evidence = task_evidence_message(source, counter, 1000)
    assert counter.count_text(evidence["content"]) <= 1000
    entries = json.loads(evidence["content"].split("\n", 1)[1])
    assert 0 < len(entries) <= 8
    assert all(entry["observation_id"] and entry["source_index"] >= 22 for entry in entries)


@pytest.mark.asyncio
async def test_sqlite_lock_wait_does_not_block_async_writer_release(tmp_path, monkeypatch):
    import cc_harness.context_state as state_module

    source = [
        {"role": "system", "content": "system"},
        {"role": "tool", "content": "old log\n" * 300},
        {"role": "user", "content": "current", "_context_mandatory": True},
    ]
    projection = ContextProjection(source, artifact_dir=tmp_path)
    state = projection.state_store
    connect = state._connect

    def bounded_connect(*args):
        connection = connect(*args)
        connection.execute("PRAGMA busy_timeout=500")
        return connection

    original_begin = state_module.begin_immediate_sync
    monkeypatch.setattr(state, "_connect", bounded_connect)
    monkeypatch.setattr(
        state_module,
        "begin_immediate_sync",
        lambda connection: original_begin(connection, attempts=1),
    )
    blocker = sqlite3.connect(state.db_path)
    blocker.execute("BEGIN IMMEDIATE")
    ticks = []

    async def release_writer():
        await asyncio.sleep(0.05)
        ticks.append("event loop remained responsive")
        blocker.commit()

    release = asyncio.create_task(release_writer())
    try:
        stats = await projection.compact(
            source,
            None,
            TokenCounter(),
            ContextConfig(
                context_window=100_000,
                output_reserve_tokens=0,
                protect_zone_tokens=1,
                tier1_threshold=0.001,
                tier2_threshold=0.98,
                tier3_threshold=0.99,
            ),
            LossySummary(),
        )
        assert stats.error is None
        assert stats.applied
        assert ticks
    finally:
        blocker.rollback()
        blocker.close()
        await release


@pytest.mark.asyncio
async def test_old_algorithm_projection_is_rebuilt_from_original_source(tmp_path, monkeypatch):
    import cc_harness.context as context_module

    current_version = context_module._COMPACTION_ALGORITHM_VERSION
    monkeypatch.setattr(context_module, "_COMPACTION_ALGORITHM_VERSION", "old-lossy-algorithm")
    source = [
        {"role": "system", "content": "system"},
        {"role": "tool", "content": "old log\n" * 300},
        {"role": "user", "content": "current", "_context_mandatory": True},
    ]
    first = ContextProjection(source, artifact_dir=tmp_path)
    stats = await first.compact(
        source,
        None,
        TokenCounter(),
        ContextConfig(
            context_window=100_000,
            output_reserve_tokens=0,
            protect_zone_tokens=1,
            tier1_threshold=0.001,
            tier2_threshold=0.98,
            tier3_threshold=0.99,
        ),
        LossySummary(),
    )
    assert stats.applied
    monkeypatch.setattr(context_module, "_COMPACTION_ALGORITHM_VERSION", current_version)
    restored = ContextProjection(source, artifact_dir=tmp_path)
    assert restored.messages == source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "thresholds", [(0.001, 0.98, 0.99), (0.001, 0.002, 0.99), (0.001, 0.002, 0.003)]
)
async def test_new_tool_output_is_seen_once_before_history_compaction(tmp_path, thresholds):
    text = "Important contract: preserve empty input.\n" + "historical log line\n" * 400
    source = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "Repair app", "_context_mandatory": True},
        {
            "role": "assistant",
            "content": "Read the specification",
            "tool_calls": [
                {"id": "r1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}
            ],
        },
        observation("r1", text, read=("spec.md",)),
    ]
    projection = ContextProjection(source, artifact_dir=tmp_path)
    settings = ContextConfig(
        context_window=100_000,
        output_reserve_tokens=0,
        protect_zone_tokens=1,
        tier1_threshold=thresholds[0],
        tier2_threshold=thresholds[1],
        tier3_threshold=thresholds[2],
    )
    await projection.compact(source, None, TokenCounter(), settings, LossySummary())
    tools = [m for m in projection.messages if m.get("role") == "tool"]
    assert json.loads(tools[0]["content"])["content"] == text
    source.append({"role": "assistant", "content": "I have read the file; implement the change."})
    stats = await projection.compact(source, None, TokenCounter(), settings, LossySummary())
    assert stats.applied
    assert stats.after_tokens < stats.before_tokens
    assert any("Important contract" in m.get("content", "") for m in projection.messages)


@pytest.mark.asyncio
async def test_consumed_fresh_output_triggers_summary_before_preview_refresh(tmp_path):
    source = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "Repair app", "_context_mandatory": True},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "r1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}
        ]},
        observation("r1", "Important contract: empty input.\n" + "audit log line\n" * 1000,
                    read=("spec.md",)),
    ]
    settings = ContextConfig(context_window=20000, output_reserve_tokens=0,
                             protect_zone_tokens=1, tier1_threshold=.035,
                             tier2_threshold=.055, tier3_threshold=.08)
    llm = LossySummary()
    projection = ContextProjection(source, artifact_dir=tmp_path)
    await projection.compact(source, None, TokenCounter(), settings, llm)
    assert llm.calls == 0
    source.append({"role": "assistant", "content": "The specification has been consumed."})
    stats = await projection.compact(source, None, TokenCounter(), settings, llm)
    assert stats.summarized
    assert llm.calls == 1
    assert stats.after_tokens < stats.before_tokens
