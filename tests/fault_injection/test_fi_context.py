"""Context-domain fault injection (C1-C10).

The tests treat the authoritative message list as input and inspect both the
model-facing projection and the durable compaction manifest.  They also cover
the intentionally untrusted offload/reference path used after compaction.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from cc_harness.config import ContextConfig
from cc_harness.context import (
    CompactionTier,
    ContextProjection,
    select_compaction_tier,
    usable_input_budget,
)
from cc_harness.context_refs import context_message_ref, context_scope_ref, message_digest, messages_digest
from cc_harness.context_state import SqliteContextState
from cc_harness.llm import StreamEvent
from cc_harness.memory.offload.offload import maybe_offload
from cc_harness.memory.offload.read_ref import read_ref_handler, search_ref_handler
from cc_harness.tokens import TokenCounter


class CharCounter:
    def count_text(self, text):
        return len(text or "")

    def categorize(self, messages, tools=None):
        del tools
        categories = {
            "user_input": 0,
            "tool_calls": 0,
            "llm_output": 0,
            "system_prompt": 0,
            "summary": 0,
            "tool_definitions": 0,
        }
        for message in messages:
            size = len(message.get("content")) if isinstance(message.get("content"), str) else 0
            role = message.get("role")
            if role == "system":
                categories["system_prompt"] += size
            elif role == "user":
                categories["user_input"] += size
            elif role == "tool":
                categories["tool_calls"] += size
            else:
                categories["llm_output"] += size
        return categories


class SummaryLLM:
    model = "fi-summary-model"

    def __init__(self, content: str = "summary"):
        self.content = content
        self.calls = 0

    async def chat(self, messages, tools=None):
        del messages, tools
        self.calls += 1
        yield StreamEvent(kind="done", content=self.content)


def cfg(**overrides) -> ContextConfig:
    values = {
        "context_window": 2_000,
        "output_reserve_tokens": 0,
        "tool_schema_reserve_tokens": 0,
        "protect_zone_tokens": 5,
        "tier1_threshold": 0.6,
        "tier2_threshold": 0.8,
        "tier3_threshold": 0.95,
    }
    values.update(overrides)
    return ContextConfig(**values)


def _source(history: int = 20) -> list[dict]:
    messages = [{"role": "system", "content": "rules", "_context_mandatory": True}]
    messages.extend({"role": "tool", "content": f"tool-line-{index}\n" * history} for index in range(4))
    messages.append({"role": "user", "content": "latest instruction", "_context_mandatory": True})
    return messages


def test_c1_compaction_tiers_are_mutually_exclusive_and_reserve_output():
    settings = cfg(context_window=100, output_reserve_tokens=20)
    assert select_compaction_tier(0.59, settings) is CompactionTier.NONE
    assert select_compaction_tier(0.60, settings) is CompactionTier.SNIP
    assert select_compaction_tier(0.80, settings) is CompactionTier.PRUNE
    assert select_compaction_tier(0.95, settings) is CompactionTier.SUMMARIZE
    total, usable, ratio = usable_input_budget(
        [{"role": "user", "content": "x" * 50}],
        [{"type": "function", "function": {"name": "huge", "description": "y" * 20}}],
        CharCounter(),
        settings,
    )
    # The local CharCounter intentionally counts the message body only;
    # tool definitions are reserved separately by the production tokenizer.
    assert total == 50 and usable == 80 and ratio > 0


@pytest.mark.asyncio
async def test_c2_incremental_compaction_reduces_projection_without_duplicate_summary(tmp_path):
    source = _source(70)
    projection = ContextProjection(source, artifact_dir=tmp_path, context_id="c2")
    summary = SummaryLLM("first summary")
    first = await projection.compact(source, None, CharCounter(), cfg(
        tier1_threshold=0.01, tier2_threshold=0.02, tier3_threshold=0.03
    ), summary)
    assert first.tier is CompactionTier.SUMMARIZE
    assert first.summarized and first.summary_version == 1
    source.extend([
        {"role": "assistant", "content": "new history " * 70},
        {"role": "user", "content": "new latest", "_context_mandatory": True},
    ])
    second = await projection.compact(source, None, CharCounter(), cfg(
        tier1_threshold=0.01, tier2_threshold=0.02, tier3_threshold=0.03
    ), SummaryLLM("second summary"))
    assert second.summarized and second.summary_version == 2
    summaries = [item for item in projection.messages if item.get("_compaction_summary")]
    assert len(summaries) == 2
    assert summaries[0]["content"].endswith("first summary")
    assert summaries[1]["content"].endswith("second summary")
    assert second.after_tokens < second.before_tokens


@pytest.mark.asyncio
async def test_c3_compaction_writer_lease_blocks_competing_projection(tmp_path):
    source = _source(70)
    first = ContextProjection(source, state_db_path=tmp_path / "context.sqlite3", context_id="c3")
    second = ContextProjection(source, state_db_path=tmp_path / "context.sqlite3", context_id="c3")
    settings = cfg(tier1_threshold=0.01, tier2_threshold=0.02, tier3_threshold=0.03)
    one, two = await asyncio.gather(
        first.compact(source, None, CharCounter(), settings, SummaryLLM("one")),
        second.compact(source, None, CharCounter(), settings, SummaryLLM("two")),
    )
    outcomes = (one, two)
    # The two writers are serialized.  Depending on scheduling the second
    # call either reuses the same idempotency key or publishes the next
    # immutable version; both outcomes must remain valid and lock-free.
    assert all(item.error is None for item in outcomes)
    with sqlite3.connect(tmp_path / "context.sqlite3") as db:
        assert 1 <= db.execute(
            "SELECT COUNT(*) FROM context_compaction_version WHERE context_id='c3'"
        ).fetchone()[0] <= 2


def test_c4_compaction_commit_is_atomic_and_idempotent(tmp_path):
    state = SqliteContextState(tmp_path / "context.sqlite3", "c4")
    epoch, parent = state.acquire("writer", ttl_seconds=30)
    payload = {"projection_messages": [{"role": "system", "content": "safe"}], "after_tokens": 1}
    first = state.commit(
        owner_id="writer", epoch=epoch, expected_parent=parent,
        compaction_key="same-key", tier="snip", source_digest="sha256:source",
        source_count=1, summary_version=None, payload=payload,
    )
    # The retry after a process crash sees the existing immutable key and does
    # not publish a second version.
    retry = state.commit(
        owner_id="writer", epoch=epoch, expected_parent=parent,
        compaction_key="same-key", tier="snip", source_digest="sha256:source",
        source_count=1, summary_version=None, payload=payload,
    )
    assert retry.version == first.version == 1
    assert state.current().version == 1
    with sqlite3.connect(tmp_path / "context.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM context_compaction_version").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_c5_uncompressible_mandatory_state_is_reported_not_dropped(tmp_path):
    source = [
        {"role": "system", "content": "mandatory rules " * 400, "_context_mandatory": True},
        {"role": "user", "content": "current", "_context_mandatory": True},
    ]
    projection = ContextProjection(source, artifact_dir=tmp_path, context_id="c5")
    result = await projection.compact(source, None, CharCounter(), cfg(
        context_window=80,
        output_reserve_tokens=0,
        protect_zone_tokens=0,
        tier1_threshold=0.01,
        tier2_threshold=0.02,
        tier3_threshold=0.03,
    ), SummaryLLM())
    assert result.after_tokens >= result.before_tokens or result.error is not None
    assert any(item.get("_context_mandatory") for item in projection.messages)


@pytest.mark.asyncio
async def test_c6_offload_reference_requires_authorized_scope_and_digest(tmp_path):
    history = [
        {"role": "system", "content": "rules"},
        {"role": "tool", "content": "needle exact fact\nother line"},
    ]
    scope = context_scope_ref("c6", 0, 2, messages_digest(history))
    atom = context_message_ref("c6", 1, message_digest(history[1]))
    state = SqliteContextState(tmp_path / "runtime.sqlite3", "c6")
    epoch, parent = state.acquire("writer", ttl_seconds=30)
    state.commit(
        owner_id="writer", epoch=epoch, expected_parent=parent, compaction_key="scope",
        tier="summarize", source_digest="sha256:history", source_count=2,
        summary_version=1, payload={
            "source_scope": scope, "source_refs": [{"source_ref": atom}],
            "projection_messages": [],
        },
    )

    async def read_history():
        return history

    search = await search_ref_handler(
        {"summary_id": scope, "query": "needle", "limit": 1},
        cwd=str(tmp_path), refs_dir=tmp_path / "refs", history_reader=read_history,
        history_context_id="c6", reference_authorizer=state.authorizes_ref,
    )
    body = json.loads(search.llm_text)
    assert body["complete"] and body["hits"][0]["source_ref"] == atom
    read = await read_ref_handler(
        {"source_ref": atom, "offset": 0, "limit": 1}, cwd=str(tmp_path),
        refs_dir=tmp_path / "refs", history_reader=read_history,
        history_context_id="c6", reference_authorizer=state.authorizes_ref,
    )
    assert '"trust": "untrusted_evidence"' in read.llm_text
    forged = context_scope_ref("c6", 0, 1, messages_digest(history[:1]))
    denied = await search_ref_handler(
        {"summary_id": forged, "query": "needle"}, cwd=str(tmp_path),
        refs_dir=tmp_path / "refs", history_reader=read_history,
        history_context_id="c6", reference_authorizer=state.authorizes_ref,
    )
    assert denied.is_error


@pytest.mark.asyncio
async def test_c7_cumulative_summary_keeps_old_fragments_and_rebuilds_after_restart(tmp_path):
    source = _source(60)
    projection = ContextProjection(source, state_db_path=tmp_path / "context.sqlite3", context_id="c7")
    settings = cfg(tier1_threshold=0.01, tier2_threshold=0.02, tier3_threshold=0.03)
    await projection.compact(source, None, CharCounter(), settings, SummaryLLM("v1"))
    source.extend([
        {"role": "assistant", "content": "later " * 80},
        {"role": "user", "content": "latest", "_context_mandatory": True},
    ])
    await projection.compact(source, None, CharCounter(), settings, SummaryLLM("v2"))
    restored = ContextProjection(source, state_db_path=tmp_path / "context.sqlite3", context_id="c7")
    assert restored.current_version == 2
    assert restored.summary_version == 2
    entries = restored.cumulative_entries
    assert len([item for item in entries if item.get("entry_type") == "summary"]) == 2


def test_c8_stable_prefix_and_call_manifest_are_content_addressed(tmp_path):
    state = SqliteContextState(
        tmp_path / "context.sqlite3", "c8", call_db_path=tmp_path / "calls.sqlite3"
    )
    first = state.record_call({"prefix": "system+tools", "turn": 1})
    second = state.record_call({"prefix": "system+tools", "turn": 2})
    assert first != second and first.endswith("/1") and second.endswith("/2")
    with sqlite3.connect(tmp_path / "calls.sqlite3") as db:
        rows = db.execute(
            "SELECT call_sequence, payload_json FROM context_call_manifest WHERE context_id='c8' ORDER BY call_sequence"
        ).fetchall()
        assert [row[0] for row in rows] == [1, 2]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE context_call_manifest SET payload_json='tampered' WHERE context_id='c8' AND call_sequence=1")


def test_c9_retention_priority_preserves_mandatory_and_permission_state(tmp_path):
    source = [
        {"role": "system", "content": "safety", "_context_mandatory": True},
        {"role": "user", "content": "acceptance", "_context_mandatory": True},
        {"role": "system", "content": "permission=default", "_permission_state": True},
        {"role": "tool", "content": "history " * 300},
        {"role": "user", "content": "latest", "_context_mandatory": True},
    ]
    projection = ContextProjection(source, artifact_dir=tmp_path, context_id="c9")
    # Make summary unavoidable while retaining the mandatory reserve.
    import asyncio as _asyncio
    stats = _asyncio.run(projection.compact(source, None, CharCounter(), cfg(
        tier1_threshold=0.01, tier2_threshold=0.02, tier3_threshold=0.03
    ), SummaryLLM()))
    assert stats.summarized
    contents = [item.get("content") for item in projection.messages]
    assert "safety" in contents and "acceptance" in contents and "permission=default" in contents


def test_c10_current_pointer_recovers_from_old_immutable_version(tmp_path):
    state = SqliteContextState(tmp_path / "context.sqlite3", "c10")
    epoch, parent = state.acquire("writer", ttl_seconds=30)
    first = state.commit(
        owner_id="writer", epoch=epoch, expected_parent=parent, compaction_key="v1",
        tier="snip", source_digest="sha256:v1", source_count=1, summary_version=None,
        payload={"projection_messages": [{"role": "system", "content": "v1"}]},
    )
    with sqlite3.connect(tmp_path / "context.sqlite3") as db:
        db.execute("UPDATE context_current_compaction SET version=999 WHERE context_id='c10'")
        db.commit()
    # Current pointer is not trusted by itself; candidates still expose the
    # immutable version so the projection can choose a valid parent.
    assert state.current() is None
    candidates = state.candidates()
    assert any(item.version == first.version for item in candidates)


@pytest.mark.asyncio
async def test_c10_large_offload_writes_exact_bytes_and_manifest(tmp_path):
    text = "line\n" * 10000
    result = await maybe_offload(
        text, "Read", {}, 1, tmp_path / "refs", None, TokenCounter(),
        manifest_path=tmp_path / "nodes.jsonl", session_id="c10-large",
        state_db_path=tmp_path / "runtime.sqlite3",
    )
    assert result is not None
    assert Path(result.refs_path).read_text(encoding="utf-8") == text
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as db:
        row = db.execute(
            "SELECT content_digest FROM context_offload_node WHERE context_id='c10-large'"
        ).fetchone()
    assert row == (result.content_digest,)
