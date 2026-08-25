from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cc_harness.config import ContextConfig
from cc_harness.context import CompactionTier, ContextProjection
from cc_harness.context_refs import (
    context_message_ref,
    context_scope_ref,
    message_digest,
    messages_digest,
)
from cc_harness.context_state import ContextCommitConflict, SqliteContextState
from cc_harness.llm import StreamEvent
from cc_harness.memory.offload.offload import maybe_offload
from cc_harness.memory.offload.read_ref import read_ref_handler, search_ref_handler
from cc_harness.tokens import TokenCounter


class CharCounter:
    def count_text(self, text):
        return len(text or "")

    def categorize(self, messages, tools=None):
        result = {
            "user_input": 0,
            "tool_calls": 0,
            "llm_output": 0,
            "system_prompt": 0,
            "summary": 0,
            "tool_definitions": 0,
        }
        for message in messages:
            content = message.get("content")
            size = len(content) if isinstance(content, str) else 0
            role = message.get("role")
            if role == "system":
                result["system_prompt"] += size
            elif role == "user":
                result["user_input"] += size
            elif role == "tool":
                result["tool_calls"] += size
            else:
                result["llm_output"] += size
        return result


class SummaryLLM:
    model = "summary-test-model"

    def __init__(self, content: str = "bounded summary") -> None:
        self.content = content
        self.calls = 0

    async def chat(self, messages, tools=None):
        del messages
        assert tools is None
        self.calls += 1
        yield StreamEvent(kind="done", content=self.content)


def config(**values) -> ContextConfig:
    defaults = dict(
        context_window=2_000,
        output_reserve_tokens=0,
        tool_schema_reserve_tokens=0,
        protect_zone_tokens=5,
    )
    defaults.update(values)
    return ContextConfig(**defaults)


@pytest.mark.asyncio
async def test_compaction_has_no_post_transform_ratio_gate(tmp_path):
    source = [
        {"role": "system", "content": "system", "_context_mandatory": True},
        {"role": "tool", "content": "0123456789\n" * 30},
        {"role": "user", "content": "current"},
    ]
    projection = ContextProjection(source, artifact_dir=tmp_path, context_id="no-target")

    stats = await projection.compact(
        source,
        None,
        CharCounter(),
        config(
            context_window=900,
            tier1_threshold=0.01,
            tier2_threshold=0.99,
            tier3_threshold=0.995,
        ),
        SummaryLLM(),
    )

    assert stats.tier == CompactionTier.SNIP
    assert stats.messages_snip == 1
    assert stats.ratio_after > 0.5
    assert stats.error is None
    assert projection.current_version == 1


@pytest.mark.asyncio
async def test_three_compactions_are_transactional_versions_not_overwrites(tmp_path):
    source = [
        {"role": "system", "content": "system", "_context_mandatory": True},
        {"role": "tool", "content": "line\n" * 80},
        {"role": "user", "content": "current"},
    ]
    projection = ContextProjection(source, artifact_dir=tmp_path, context_id="run-1")
    first = await projection.compact(
        source,
        None,
        CharCounter(),
        config(tier1_threshold=0.1, tier2_threshold=0.8, tier3_threshold=0.95),
        SummaryLLM(),
    )
    assert first.tier is CompactionTier.SNIP

    source.insert(-1, {"role": "tool", "content": "z" * 1_500})
    second = await projection.compact(
        source,
        None,
        CharCounter(),
        config(tier1_threshold=0.1, tier2_threshold=0.2, tier3_threshold=0.95),
        SummaryLLM(),
    )
    assert second.tier is CompactionTier.PRUNE

    source.insert(-1, {"role": "assistant", "content": "history " * 500})
    third = await projection.compact(
        source,
        None,
        CharCounter(),
        config(tier1_threshold=0.01, tier2_threshold=0.02, tier3_threshold=0.03),
        SummaryLLM(),
    )
    assert third.tier is CompactionTier.SUMMARIZE

    with sqlite3.connect(tmp_path / "context.sqlite3") as db:
        rows = db.execute(
            "SELECT version, tier, parent_version, payload_json FROM context_compaction_version "
            "WHERE context_id='run-1' ORDER BY version"
        ).fetchall()
        current = db.execute(
            "SELECT version FROM context_current_compaction WHERE context_id='run-1'"
        ).fetchone()
    assert [(row[0], row[1], row[2]) for row in rows] == [
        (1, "snip", None),
        (2, "prune", 1),
        (3, "summarize", 2),
    ]
    assert current == (3,)
    assert third.manifest_path and third.manifest_path.startswith("sqlite:///")

    payloads = [json.loads(row[3]) for row in rows]
    entries = [payload["cumulative_entries"] for payload in payloads]
    entry_ids = [{entry["entry_id"] for entry in version} for version in entries]
    assert entry_ids[0]
    assert entry_ids[0] <= entry_ids[1] <= entry_ids[2]
    assert all(payload["schema_version"] == "cc-harness.context-compaction.v4" for payload in payloads)

    first_tool = next(entry for entry in entries[1] if entry.get("source_index") == 1)
    assert {rep["tier"] for rep in first_tool["representations"]} == {"snip", "prune"}
    for version_entries in entries:
        for entry in version_entries:
            digests = [rep["representation_digest"] for rep in entry["representations"]]
            assert len(digests) == len(set(digests))

    restored = ContextProjection(source, artifact_dir=tmp_path, context_id="run-1")
    assert restored.current_version == 3
    assert restored.cumulative_entries == entries[-1]


@pytest.mark.asyncio
async def test_summary_versions_append_delta_fragments_without_rewriting_old_summary(tmp_path):
    source = [
        {"role": "system", "content": "system", "_context_mandatory": True},
        {"role": "assistant", "content": "first-history " * 300},
        {"role": "user", "content": "first-current"},
    ]
    cfg = config(
        context_window=2_000,
        tier1_threshold=0.01,
        tier2_threshold=0.02,
        tier3_threshold=0.03,
    )
    projection = ContextProjection(source, artifact_dir=tmp_path, context_id="summary-chain")
    first = await projection.compact(source, None, CharCounter(), cfg, SummaryLLM("summary-one"))
    assert first.tier is CompactionTier.SUMMARIZE

    source.extend(
        [
            {"role": "assistant", "content": "second-history " * 300},
            {"role": "user", "content": "second-current"},
        ]
    )
    second = await projection.compact(source, None, CharCounter(), cfg, SummaryLLM("summary-two"))
    assert second.tier is CompactionTier.SUMMARIZE

    summaries = [
        message for message in projection.messages if message.get("_compaction_summary")
    ]
    assert len(summaries) == 2
    assert summaries[0]["content"].endswith("summary-one")
    assert summaries[1]["content"].endswith("summary-two")
    assert summaries[0]["_compaction_coverage_end"] <= summaries[1]["_compaction_coverage_start"]

    current_payload = projection.state_store.current().payload
    summary_entries = [
        entry
        for entry in current_payload["cumulative_entries"]
        if entry["entry_type"] == "summary"
    ]
    assert len(summary_entries) == 2

    source.extend(
        [
            {"role": "tool", "content": "third-tool-line\n" * 50},
            {"role": "user", "content": "third-current"},
        ]
    )
    third = await projection.compact(
        source,
        None,
        CharCounter(),
        config(
            context_window=2_000,
            tier1_threshold=0.001,
            tier2_threshold=0.99,
            tier3_threshold=0.995,
        ),
        SummaryLLM("unused"),
    )
    assert third.tier is CompactionTier.SNIP

    restored = ContextProjection(source, artifact_dir=tmp_path, context_id="summary-chain")
    assert restored.current_version == 3
    assert restored.summary_version == 2
    assert len(
        [message for message in restored.messages if message.get("_compaction_summary")]
    ) == 2


def test_stale_writer_cannot_overwrite_new_parent(tmp_path):
    state = SqliteContextState(tmp_path / "runtime.db", "run-lease")
    epoch1, parent1 = state.acquire("old", ttl_seconds=30)
    with sqlite3.connect(tmp_path / "runtime.db") as db:
        db.execute("UPDATE context_writer_lease SET expires_at=0 WHERE context_id='run-lease'")
    epoch2, parent2 = state.acquire("new", ttl_seconds=30)
    payload = {"projection_messages": [], "after_tokens": 0, "ratio_after": 0.0}
    state.commit(
        owner_id="new",
        epoch=epoch2,
        expected_parent=parent2,
        compaction_key="new-key",
        tier="snip",
        source_digest="sha256:new",
        source_count=0,
        summary_version=None,
        payload=payload,
    )
    with pytest.raises(ContextCommitConflict):
        state.commit(
            owner_id="old",
            epoch=epoch1,
            expected_parent=parent1,
            compaction_key="old-key",
            tier="snip",
            source_digest="sha256:old",
            source_count=0,
            summary_version=None,
            payload=payload,
        )


@pytest.mark.asyncio
async def test_summary_scope_search_then_paginated_exact_read(tmp_path):
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "alpha\nneedle exact fact\nomega"},
        {"role": "assistant", "content": "ack"},
    ]
    scope = context_scope_ref("run-search", 0, 2, messages_digest(messages[:2]))
    atomic_ref = context_message_ref("run-search", 1, message_digest(messages[1]))
    state = SqliteContextState(tmp_path / "runtime.db", "run-search")
    epoch, parent = state.acquire("writer", ttl_seconds=30)
    state.commit(
        owner_id="writer",
        epoch=epoch,
        expected_parent=parent,
        compaction_key="search-key",
        tier="summarize",
        source_digest="sha256:search",
        source_count=2,
        summary_version=1,
        payload={
            "source_scope": scope,
            "source_refs": [{"source_ref": atomic_ref}],
            "projection_messages": [],
        },
    )

    async def history():
        return messages

    searched = await search_ref_handler(
        {"summary_id": scope, "query": "needle", "limit": 1},
        cwd=str(tmp_path),
        refs_dir=tmp_path / "refs",
        history_reader=history,
        history_context_id="run-search",
        reference_authorizer=state.authorizes_ref,
    )
    search_payload = json.loads(searched.llm_text)
    assert search_payload["complete"] is True
    source_ref = search_payload["hits"][0]["source_ref"]
    read = await read_ref_handler(
        {"source_ref": source_ref, "offset": 1, "limit": 1},
        cwd=str(tmp_path),
        refs_dir=tmp_path / "refs",
        history_reader=history,
        history_context_id="run-search",
        reference_authorizer=state.authorizes_ref,
    )
    header, body = read.llm_text.split("\n", 1)
    assert json.loads(header)["trust"] == "untrusted_evidence"
    assert body == "needle exact fact"
    forged = context_scope_ref("run-search", 1, 3, messages_digest(messages[1:3]))
    denied = await search_ref_handler(
        {"summary_id": forged, "query": "needle"},
        cwd=str(tmp_path),
        refs_dir=tmp_path / "refs",
        history_reader=history,
        history_context_id="run-search",
        reference_authorizer=state.authorizes_ref,
    )
    assert denied.is_error


@pytest.mark.asyncio
async def test_tool_result_larger_than_old_hard_limit_is_still_offloaded(tmp_path):
    text = "large-result-line\n" * 20_000
    result = await maybe_offload(
        text,
        "Read",
        {},
        1,
        tmp_path / "refs",
        None,
        TokenCounter(),
        manifest_path=tmp_path / "nodes.jsonl",
        session_id="large-run",
        state_db_path=tmp_path / "runtime.db",
    )
    assert result is not None
    assert result.size_bytes > 256 * 1024
    assert "source_ref=node:" in result.pointer_msg
    assert Path(result.refs_path).read_text(encoding="utf-8") == text
    with sqlite3.connect(tmp_path / "runtime.db") as db:
        row = db.execute(
            "SELECT content_digest FROM context_offload_node WHERE context_id='large-run'"
        ).fetchone()
    assert row == (result.content_digest,)


@pytest.mark.asyncio
async def test_mandatory_working_state_survives_summary(tmp_path):
    source = [
        {"role": "system", "content": "rules", "_context_mandatory": True},
        {"role": "user", "content": "acceptance criterion", "_context_mandatory": True},
        {"role": "user", "content": "recalled project fact", "_memory_block": True},
        {"role": "assistant", "content": "old history " * 300},
        {"role": "user", "content": "latest"},
    ]
    projection = ContextProjection(source, artifact_dir=tmp_path, context_id="mandatory")
    stats = await projection.compact(
        source,
        None,
        CharCounter(),
        config(tier1_threshold=0.01, tier2_threshold=0.02, tier3_threshold=0.03),
        SummaryLLM(),
    )
    assert stats.summarized
    assert any(message.get("content") == "acceptance criterion" for message in projection.messages)
    assert any(message.get("content") == "recalled project fact" for message in projection.messages)
    summary = next(message for message in projection.messages if message.get("_compaction_summary"))
    assert summary["role"] == "system"
