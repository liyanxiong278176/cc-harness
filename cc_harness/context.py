"""Current-context compaction for cc-harness.

``ContextProjection`` is invoked before each LLM call in the ReAct loop. It
keeps the original transcript append-only while ``maybe_compact`` selects one
mutually-exclusive tier from the current token pressure:

- Tier 1 Snip  (ratio >= tier1): truncate long tool outputs / user code blocks (head/tail).
- Tier 2 Prune (ratio >= tier2): tool content -> placeholder; assistant text -> first sentence.
- Tier 3 Summarize (ratio >= tier3): append a summary of the authoritative delta.

A **protect zone** (the most recent ~``protect_zone_tokens`` plus the last user
message) is never touched. Tiers mutate only projection messages. Failures are
reported in ``CompactionStats`` so the caller can enforce fail-closed window
protection.

Design spec: ``docs/superpowers/specs/2026-06-12-context-compaction-design.md``.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any

from cc_harness.atomic import atomic_write_text
from cc_harness.config import ContextConfig
from cc_harness.context_refs import (
    context_message_ref,
    context_scope_ref,
    message_digest as authoritative_message_digest,
    messages_digest as authoritative_messages_digest,
)
from cc_harness.context_state import (
    ContextLeaseConflict,
    SqliteContextState,
)
from cc_harness.prompts import (
    SUMMARY_SYSTEM_PROMPT,
    _render_messages_for_summary,
    summary_user_prompt,
)
from cc_harness.tokens import SUMMARY_MARKER_KEY, TokenCounter

# Public constants -----------------------------------------------------------

TIER2_TOOL_PLACEHOLDER = "[Old tool result content cleared]"
TRUNCATED_MARKER = " [truncated]"
OMITTED_TEMPLATE = "... ({n} lines omitted) ..."

# Tier 2 assistant fallback when no sentence boundary is present.
_FALLBACK_CHARS = 200

# 3-group fence regex for user ```` ``` ```` code blocks (no nested-fence support).
_CODE_FENCE_RE = re.compile(r"```([^\n]*)\n(.*?)\n```", re.DOTALL)

# Sentence boundary: lookbehind after CJK/Latin punctuation or newline.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.。!?！？\n])\s*")


# Data model -----------------------------------------------------------------

class CompactionTier(IntEnum):
    """Compaction tier reached by ``maybe_compact`` (higher = more aggressive)."""

    NONE = 0
    SNIP = 1
    PRUNE = 2
    SUMMARIZE = 3


@dataclass
class CompactionStats:
    """Outcome of one ``maybe_compact`` invocation.

    ``before_snapshot`` is populated only on the exception path (deep copy is
    otherwise avoided to keep the hot path zero-cost).
    """

    tier: CompactionTier
    before_tokens: int
    after_tokens: int
    ratio_before: float
    ratio_after: float
    messages_snip: int = 0                    # tool outputs snipped (Tier 1)
    messages_prune: int = 0                   # tool outputs pruned (Tier 2)
    messages_assistant_truncated: int = 0     # assistant texts truncated (Tier 2)
    summarized: bool = False                  # Tier 3 produced a new summary
    summary_index: int | None = None          # insert index of the new summary
    error: str | None = None                  # exception message (if any)
    before_snapshot: list[dict] | None = None  # debug snapshot (exception path only)
    summary_version: int | None = None
    artifact_path: str | None = None
    manifest_path: str | None = None
    compaction_key: str | None = None
    source_range: tuple[int, int] | None = None
    delta_range: tuple[int, int] | None = None
    queued_messages: int = 0


class ContextProjection:
    """Model-facing, rebuildable view over an append-only message transcript."""

    def __init__(
        self,
        source_messages: list[dict],
        *,
        artifact_dir: Path | None = None,
        state_db_path: Path | None = None,
        context_id: str | None = None,
    ) -> None:
        self.messages = copy.deepcopy(source_messages)
        self.source_count = len(source_messages)
        self.summary_version = 0
        self.compaction_version = 0
        self.cumulative_entries: list[dict[str, Any]] = []
        self.current_version: int | None = None
        self.current_artifact: str | None = None
        self.artifact_dir = Path(artifact_dir) if artifact_dir is not None else None
        self.context_id = str(
            context_id
            or (self.artifact_dir.name if self.artifact_dir is not None else "default")
        )
        db_path = Path(state_db_path) if state_db_path is not None else (
            self.artifact_dir / "context.sqlite3" if self.artifact_dir is not None else None
        )
        self.state_store = (
            SqliteContextState(db_path, self.context_id) if db_path is not None else None
        )
        self._restore_latest(source_messages)
        self.messages = _repair_tool_result_pairing(self.messages)

    def _restore_latest(self, source_messages: list[dict]) -> None:
        """Restore the one current projection while keeping source authoritative."""
        if self.state_store is not None:
            for stored in self.state_store.candidates():
                payload = stored.payload
                projection = payload.get("projection_messages")
                if (
                    isinstance(projection, list)
                    and 0 <= stored.source_count <= len(source_messages)
                    and stored.source_digest == _messages_digest(source_messages[: stored.source_count])
                ):
                    self.messages = copy.deepcopy(projection)
                    self.messages.extend(copy.deepcopy(source_messages[stored.source_count:]))
                    self.source_count = len(source_messages)
                    self.compaction_version = stored.version
                    self.current_version = stored.version
                    self.summary_version = int(stored.summary_version or 0)
                    self.cumulative_entries = _cumulative_entries_from_payload(
                        payload, version=stored.version, tier=stored.tier
                    )
                    self.current_artifact = self.state_store.manifest_uri(stored.version)
                    return
        if self.artifact_dir is None or not self.artifact_dir.is_dir():
            return
        candidates: list[Path] = []
        pointed_path: Path | None = None
        pointer = self.artifact_dir / "current.json"
        if pointer.is_file():
            try:
                pointed = json.loads(pointer.read_text(encoding="utf-8")).get("artifact")
                if pointed:
                    candidate = (self.artifact_dir / str(pointed)).resolve()
                    candidate.relative_to(self.artifact_dir.resolve())
                    candidates.append(candidate)
                    pointed_path = candidate
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        candidates.extend([*self.artifact_dir.glob("compaction-v*.json"), *self.artifact_dir.glob("summary-v*.json")])
        seen: set[Path] = set()
        loaded: list[tuple[int, Path, dict[str, Any]]] = []
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                version = int(payload["version"])
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            loaded.append((version, path, payload))
        ordered = sorted(
            loaded,
            key=lambda item: (item[1] != pointed_path, -item[0], item[1].name),
        )
        for version, path, payload in ordered:
            self.compaction_version = max(self.compaction_version, version)
            if payload.get("tier") == CompactionTier.SUMMARIZE.name.lower() or path.name.startswith("summary-"):
                try:
                    summary_version = int(payload.get("summary_version", version))
                except (TypeError, ValueError):
                    summary_version = version
                self.summary_version = max(self.summary_version, summary_version)
            try:
                source_count = int(payload.get("source_count", payload.get("source_head", 0)) or 0)
            except (TypeError, ValueError):
                continue
            projection = payload.get("projection_messages")
            if not isinstance(projection, list) or not 0 <= source_count <= len(source_messages):
                continue
            if payload.get("source_digest") != _messages_digest(source_messages[:source_count]):
                continue
            self.messages = copy.deepcopy(projection)
            self.messages.extend(copy.deepcopy(source_messages[source_count:]))
            self.source_count = len(source_messages)
            self.current_artifact = path.name
            self.current_version = version
            self.cumulative_entries = _cumulative_entries_from_payload(
                payload, version=version, tier=str(payload.get("tier") or "legacy")
            )
            return

    def sync(self, source_messages: list[dict]) -> None:
        if len(source_messages) < self.source_count:
            self.messages = copy.deepcopy(source_messages)
            self.source_count = len(source_messages)
            self.messages = _repair_tool_result_pairing(self.messages)
            return
        if len(source_messages) > self.source_count:
            self.messages.extend(copy.deepcopy(source_messages[self.source_count:]))
            self.source_count = len(source_messages)
        self.messages = _repair_tool_result_pairing(self.messages)

    def _current_payload(self) -> dict[str, Any] | None:
        if self.state_store is not None:
            stored = self.state_store.current()
            return stored.payload if stored is not None else None
        if self.artifact_dir is None or not self.current_artifact:
            return None
        path = self.artifact_dir / self.current_artifact
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def record_call_manifest(
        self,
        stats: CompactionStats,
        tool_specs: list[dict] | None,
        counter: TokenCounter,
        config: ContextConfig,
    ) -> str | None:
        """Persist why the one current projection is safe for a model call."""
        if self.state_store is None:
            return None
        categories = counter.categorize(self.messages, tool_specs)
        payload = {
            "schema_version": "cc-harness.context-call.v1",
            "current_compaction": stats.manifest_path or self.current_artifact,
            "compaction_key": stats.compaction_key,
            "tier": stats.tier.name.lower(),
            "error": stats.error,
            "message_count": len(self.messages),
            "token_categories": categories,
            "before_tokens": stats.before_tokens,
            "after_tokens": stats.after_tokens,
            "ratio_before": stats.ratio_before,
            "ratio_after": stats.ratio_after,
            "context_limit": config.context_window,
            "output_reserve": config.output_reserve_tokens,
            "tool_schema_reserve": max(
                config.tool_schema_reserve_tokens,
                int(categories.get("tool_definitions", 0)),
            ),
            "retention_priority": [
                "system_and_safety",
                "current_instruction",
                "working_state",
                "recent_messages",
                "tool_and_output_reserves",
                "project_memory",
                "historical_summary_and_refs",
            ],
            "mandatory_messages": sum(
                1 for message in self.messages
                if message.get("role") == "system" or _is_mandatory(message)
            ),
            "protected_tail_tokens": config.protect_zone_tokens,
        }
        return self.state_store.record_call(payload)

    async def compact(
        self,
        source_messages: list[dict],
        tool_specs: list[dict] | None,
        counter: TokenCounter,
        config: ContextConfig,
        llm: Any,
    ) -> CompactionStats:
        self.sync(source_messages)
        total_before, _usable_before, ratio_before = usable_input_budget(
            self.messages, tool_specs, counter, config
        )
        selected_tier = select_compaction_tier(ratio_before, config)
        digest = _messages_digest(source_messages[: self.source_count])
        expected_key = _compaction_key(
            digest, selected_tier.name.lower(), config, llm=llm
        )
        if selected_tier != CompactionTier.NONE:
            if self.state_store is not None:
                stored = self.state_store.by_key(expected_key)
                if stored is not None:
                    projection = stored.payload.get("projection_messages")
                    if isinstance(projection, list):
                        self.messages = copy.deepcopy(projection)
                        self.messages.extend(copy.deepcopy(source_messages[stored.source_count:]))
                        self.source_count = len(source_messages)
                        self.compaction_version = stored.version
                        self.current_version = stored.version
                        self.summary_version = int(stored.summary_version or 0)
                        self.cumulative_entries = _cumulative_entries_from_payload(
                            stored.payload, version=stored.version, tier=stored.tier
                        )
                        self.current_artifact = self.state_store.manifest_uri(stored.version)
                    return CompactionStats(
                        tier=selected_tier,
                        before_tokens=total_before,
                        after_tokens=int(stored.payload.get("after_tokens", total_before)),
                        ratio_before=ratio_before,
                        ratio_after=float(stored.payload.get("ratio_after", ratio_before)),
                        summarized=selected_tier == CompactionTier.SUMMARIZE,
                        summary_version=stored.summary_version,
                        manifest_path=self.state_store.manifest_uri(stored.version),
                        compaction_key=expected_key,
                    )
            elif self.current_artifact:
                payload = self._current_payload()
                try:
                    payload_source_count = int(payload.get("source_count", -1)) if payload else -1
                except (TypeError, ValueError):
                    payload_source_count = -1
                if (
                    payload is not None
                    and payload.get("compaction_key") == expected_key
                    and payload_source_count == self.source_count
                ):
                    return CompactionStats(
                        tier=selected_tier,
                        before_tokens=total_before,
                        after_tokens=total_before,
                        ratio_before=ratio_before,
                        ratio_after=ratio_before,
                        summarized=selected_tier == CompactionTier.SUMMARIZE,
                        summary_version=self.summary_version or None,
                        artifact_path=str(self.artifact_dir / self.current_artifact),
                        manifest_path=str(self.artifact_dir / self.current_artifact),
                        compaction_key=expected_key,
                    )
        lease_owner: str | None = None
        lease_epoch: int | None = None
        expected_parent = self.current_version
        if selected_tier != CompactionTier.NONE and self.state_store is not None:
            lease_owner = uuid.uuid4().hex
            try:
                lease_epoch, expected_parent = self.state_store.acquire(
                    lease_owner,
                    ttl_seconds=config.compaction_lease_ttl_seconds,
                )
            except ContextLeaseConflict as exc:
                return CompactionStats(
                    tier=selected_tier,
                    before_tokens=total_before,
                    after_tokens=total_before,
                    ratio_before=ratio_before,
                    ratio_after=ratio_before,
                    error=str(exc),
                    compaction_key=expected_key,
                )
        before_messages = copy.deepcopy(self.messages)
        before_source_count = self.source_count
        before_summary_version = self.summary_version
        before_compaction_version = self.compaction_version
        before_cumulative_entries = copy.deepcopy(self.cumulative_entries)
        before_current_version = self.current_version
        before_current_artifact = self.current_artifact
        stats = await maybe_compact(
            self.messages,
            tool_specs,
            counter,
            config,
            llm,
            authoritative_messages=source_messages,
            context_id=self.context_id,
        )
        self.messages = _repair_tool_result_pairing(self.messages)
        if stats.error:
            self.messages = before_messages
            self.source_count = before_source_count
            self.summary_version = before_summary_version
            self.compaction_version = before_compaction_version
            self.cumulative_entries = before_cumulative_entries
            self.current_version = before_current_version
            self.current_artifact = before_current_artifact
            if self.state_store is not None and lease_owner is not None and lease_epoch is not None:
                self.state_store.release(lease_owner, lease_epoch)
            return stats
        if stats.tier == CompactionTier.NONE:
            if self.state_store is not None and lease_owner is not None and lease_epoch is not None:
                self.state_store.release(lease_owner, lease_epoch)
            return stats

        self.compaction_version = (expected_parent or self.compaction_version) + 1
        if stats.source_range is None:
            source_protect = find_protect_boundary(
                source_messages, counter, config.protect_zone_tokens
            )
            stats.source_range = (0, source_protect)
            stats.delta_range = (0, source_protect)
        if not any(message.get("_compaction_pointer") for message in self.messages):
            pointer = _source_pointer_message(
                source_messages,
                stats.source_range[0],
                stats.source_range[1],
                context_id=self.context_id,
            )
            if pointer is not None:
                insert_at = 0
                while insert_at < len(self.messages) and (
                    self.messages[insert_at].get("role") == "system"
                    or self.messages[insert_at].get("_context_mandatory")
                    or self.messages[insert_at].get(SUMMARY_MARKER_KEY)
                ):
                    insert_at += 1
                self.messages.insert(insert_at, pointer)
                after, _usable_after, ratio_after = usable_input_budget(
                    self.messages, tool_specs, counter, config
                )
                stats.after_tokens = after
                stats.ratio_after = ratio_after
        stats.compaction_key = _compaction_key(
            digest, stats.tier.name.lower(), config, llm=llm
        )
        if stats.summarized:
            self.summary_version += 1
            summary_index = stats.summary_index
            if summary_index is None or not 0 <= summary_index < len(self.messages):
                summary_index = next(
                    (
                        i for i in range(len(self.messages) - 1, -1, -1)
                        if self.messages[i].get(SUMMARY_MARKER_KEY)
                    ),
                    None,
                )
            if summary_index is not None:
                stats.summary_index = summary_index
                summary = self.messages[summary_index]
                summary["_compaction_summary_version"] = self.summary_version
                summary["_compaction_source_count"] = self.source_count
                summary["_compaction_source_digest"] = digest
                if stats.source_range is not None:
                    summary["_compaction_coverage_end"] = stats.source_range[1]
                if stats.delta_range is not None:
                    summary["_compaction_coverage_start"] = stats.delta_range[0]
                    summary["_compaction_delta_digest"] = authoritative_messages_digest(
                        source_messages[stats.delta_range[0]:stats.delta_range[1]]
                    )
                stats.summary_version = self.summary_version
        current_entries = _cumulative_entries_for_compaction(
            source_messages,
            source_end=stats.source_range[1] if stats.source_range is not None else self.source_count,
            tier=stats.tier,
            config=config,
            projection_messages=self.messages,
            context_id=self.context_id,
            version=self.compaction_version,
        )
        self.cumulative_entries = _merge_cumulative_entries(
            self.cumulative_entries, current_entries
        )
        committed = False
        try:
            payload = self._compaction_payload(
                source_messages=source_messages,
                tool_specs=tool_specs,
                counter=counter,
                config=config,
                stats=stats,
                source_digest=digest,
                llm=llm,
            )
            if self.state_store is not None:
                assert lease_owner is not None and lease_epoch is not None
                stored = self.state_store.commit(
                    owner_id=lease_owner,
                    epoch=lease_epoch,
                    expected_parent=expected_parent,
                    compaction_key=stats.compaction_key,
                    tier=stats.tier.name.lower(),
                    source_digest=digest,
                    source_count=self.source_count,
                    summary_version=self.summary_version or None,
                    payload=payload,
                )
                self.compaction_version = stored.version
                self.current_version = stored.version
                self.current_artifact = self.state_store.manifest_uri(stored.version)
                stats.manifest_path = self.current_artifact
                committed = True
            if self.artifact_dir is not None:
                path = self._write_compaction_artifact(
                    source_messages=source_messages,
                    tool_specs=tool_specs,
                    counter=counter,
                    config=config,
                    stats=stats,
                    source_digest=digest,
                    llm=llm,
                    payload=payload,
                )
                # Every tier has a durable artifact. Summary artifacts carry
                # the LLM output; Snip/Prune artifacts carry the exact current
                # projection plus source references for audit/recovery.
                stats.artifact_path = str(path)
                if self.state_store is None:
                    stats.manifest_path = str(path)
                    self.current_artifact = path.name
                    self.current_version = self.compaction_version
        except Exception as exc:  # noqa: BLE001
            if committed:
                # SQLite already atomically published the authoritative state.
                # A compatibility JSON mirror is never allowed to roll it back.
                stats.artifact_path = None
                return stats
            self.messages = before_messages
            self.source_count = before_source_count
            self.summary_version = before_summary_version
            self.compaction_version = before_compaction_version
            self.cumulative_entries = before_cumulative_entries
            self.current_version = before_current_version
            self.current_artifact = before_current_artifact
            stats.error = f"atomic compaction commit failed: {exc}"
            stats.before_snapshot = before_messages
            if self.state_store is not None and lease_owner is not None and lease_epoch is not None:
                self.state_store.release(lease_owner, lease_epoch)
        return stats

    def _compaction_payload(
        self,
        *,
        source_messages: list[dict],
        tool_specs: list[dict] | None,
        counter: TokenCounter,
        config: ContextConfig,
        stats: CompactionStats,
        source_digest: str,
        llm: Any = None,
    ) -> dict[str, Any]:
        tier_name = stats.tier.name.lower()
        source_end = stats.source_range[1] if stats.source_range is not None else self.source_count
        delta_start = stats.delta_range[0] if stats.delta_range is not None else 0
        delta_end = stats.delta_range[1] if stats.delta_range is not None else source_end
        source_scope = context_scope_ref(
            self.context_id,
            0,
            source_end,
            authoritative_messages_digest(source_messages[:source_end]),
        ) if source_end > 0 else None
        return {
            "schema_version": "cc-harness.context-compaction.v4",
            "version": self.compaction_version,
            "summary_version": self.summary_version or None,
            "summary_identity": (
                f"{self.context_id}:summary:{self.summary_version}"
                if self.summary_version else None
            ),
            "parent_summary_version": (
                max(0, self.summary_version - 1)
                if stats.summarized
                else (self.summary_version or None)
            ),
            "tier": tier_name,
            "created_at": time.time(),
            "parent_artifact": self.current_artifact,
            "parent_version": self.current_version,
            "source_count": self.source_count,
            "source_digest": source_digest,
            "coverage_range": [0, source_end],
            "delta_range": [delta_start, delta_end],
            "source_refs": _source_refs(
                source_messages, 0, source_end, context_id=self.context_id
            ),
            "source_scope": source_scope,
            "compaction_key": _compaction_key(source_digest, tier_name, config, llm=llm),
            "summary_prompt_digest": "sha256:" + hashlib.sha256(
                (SUMMARY_SYSTEM_PROMPT + "\n" + summary_user_prompt(None, "<delta>")).encode("utf-8")
            ).hexdigest(),
            "model": getattr(llm, "resolved_model", None) or getattr(llm, "model", None),
            "summary": "\n\n".join(
                str(m.get("content", ""))
                for m in self.messages
                if m.get(SUMMARY_MARKER_KEY)
            ),
            "summaries": [
                copy.deepcopy(m) for m in self.messages if m.get(SUMMARY_MARKER_KEY)
            ],
            "cumulative_entries": copy.deepcopy(self.cumulative_entries),
            "projection_messages": self.messages,
            "mandatory_state": [
                copy.deepcopy(message)
                for message in source_messages
                if message.get("role") == "system" or _is_mandatory(message)
            ],
            "before_tokens": stats.before_tokens,
            "after_tokens": stats.after_tokens,
            "ratio_before": stats.ratio_before,
            "ratio_after": stats.ratio_after,
            "usable_input_budget": usable_input_budget(
                self.messages, tool_specs, counter, config
            )[1],
        }

    def _write_compaction_artifact(
        self,
        *,
        source_messages: list[dict],
        tool_specs: list[dict] | None,
        counter: TokenCounter,
        config: ContextConfig,
        stats: CompactionStats,
        source_digest: str,
        llm: Any = None,
        payload: dict[str, Any] | None = None,
    ) -> Path:
        assert self.artifact_dir is not None
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        tier_name = stats.tier.name.lower()
        path = (
            self.artifact_dir / f"summary-v{self.summary_version:04d}.json"
            if stats.summarized
            else self.artifact_dir / f"compaction-v{self.compaction_version:04d}.json"
        )
        payload = payload or self._compaction_payload(
            source_messages=source_messages,
            tool_specs=tool_specs,
            counter=counter,
            config=config,
            stats=stats,
            source_digest=source_digest,
            llm=llm,
        )
        atomic_write_text(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        pointer = self.artifact_dir / "current.json"
        atomic_write_text(
            pointer,
            json.dumps(
                {
                    "schema_version": "cc-harness.context-current.v1",
                    "artifact": path.name,
                    "version": self.compaction_version,
                    "tier": tier_name,
                    "authoritative_manifest": (
                        self.state_store.manifest_uri(self.compaction_version)
                        if self.state_store is not None else None
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )
        return path


def _messages_digest(messages: list[dict]) -> str:
    encoded = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _source_refs(
    messages: list[dict], start: int, end: int, *, context_id: str = "default"
) -> list[dict[str, Any]]:
    """Build deterministic atomic references to authoritative source messages."""
    refs: list[dict[str, Any]] = []
    for index in range(max(0, start), min(len(messages), max(start, end))):
        message = messages[index]
        content = message.get("content")
        pointer_ref = None
        if isinstance(content, str):
            match = re.search(r"\bsource_ref=([^\s\]]+)", content)
            if match:
                pointer_ref = match.group(1)
        digest = authoritative_message_digest(message)
        refs.append(
            {
                "source_index": index,
                "role": message.get("role"),
                "message_digest": digest,
                "source_ref": (
                    message.get("_context_source_ref")
                    or message.get("source_ref")
                    or pointer_ref
                    or context_message_ref(context_id, index, digest)
                ),
                "artifact_ref": (
                    message.get("artifact_ref")
                    or message.get("source_ref")
                    or pointer_ref
                ),
            }
        )
    return refs


def _normalize_cumulative_entries(value: object) -> list[dict[str, Any]]:
    """Return valid cumulative entries from a persisted v4 payload."""
    if not isinstance(value, list):
        return []
    return [copy.deepcopy(item) for item in value if isinstance(item, dict)]


def _cumulative_entries_from_payload(
    payload: dict[str, Any], *, version: int, tier: str
) -> list[dict[str, Any]]:
    """Load v4 entries or preserve one legacy projection as a flat entry."""
    entries = _normalize_cumulative_entries(payload.get("cumulative_entries"))
    if entries:
        return entries
    projection = payload.get("projection_messages")
    if not isinstance(projection, list) or not projection:
        return []
    digest = _representation_digest({"projection_messages": projection})
    source_ref = str(payload.get("source_scope") or "")
    return [
        {
            "entry_id": f"legacy-projection:{version}:{digest}",
            "entry_type": "legacy_projection",
            "source_ref": source_ref or None,
            "source_range": copy.deepcopy(payload.get("coverage_range")),
            "last_version": version,
            "representations": [
                {
                    "tier": tier,
                    "version": version,
                    "representation_digest": digest,
                    "projection_messages": copy.deepcopy(projection),
                }
            ],
        }
    ]


def _representation_digest(message: dict[str, Any]) -> str:
    encoded = json.dumps(message, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _merge_cumulative_entries(
    previous: list[dict[str, Any]], current: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Copy-forward all entries and de-duplicate representations by digest.

    Entries are flat, self-contained records.  A child version never embeds a
    parent payload and never needs parent traversal to recover earlier compacted
    content.  Multiple lossy representations of the same authoritative source
    are retained, while byte-identical representations are stored once.
    """
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for candidate in [*previous, *current]:
        key = str(candidate.get("entry_id") or candidate.get("source_ref") or "")
        if not key:
            continue
        if key not in merged:
            item = copy.deepcopy(candidate)
            item["entry_id"] = key
            item["representations"] = []
            merged[key] = item
            order.append(key)
        target = merged[key]
        target["last_version"] = max(
            int(target.get("last_version", 0) or 0),
            int(candidate.get("last_version", 0) or 0),
        )
        known = {
            str(rep.get("representation_digest") or "")
            for rep in target.get("representations", [])
            if isinstance(rep, dict)
        }
        for representation in candidate.get("representations", []):
            if not isinstance(representation, dict):
                continue
            digest = str(representation.get("representation_digest") or "")
            if not digest or digest in known:
                continue
            target["representations"].append(copy.deepcopy(representation))
            known.add(digest)
    return [merged[key] for key in order]


def _cumulative_entries_for_compaction(
    source_messages: list[dict],
    *,
    source_end: int,
    tier: CompactionTier,
    config: ContextConfig,
    projection_messages: list[dict],
    context_id: str,
    version: int,
) -> list[dict[str, Any]]:
    """Build stable entries produced by one Snip, Prune, or Summary pass."""
    entries: list[dict[str, Any]] = []
    capped_end = min(len(source_messages), max(0, source_end))
    if tier in (CompactionTier.SNIP, CompactionTier.PRUNE):
        for index, source in enumerate(source_messages[:capped_end]):
            transformed = copy.deepcopy(source)
            one = [transformed]
            if tier == CompactionTier.SNIP:
                changed = apply_tier1_snip(one, 1, config) > 0
            else:
                pruned, assistants = apply_tier2_prune(one, 1, config)
                changed = pruned + assistants > 0 or one[0] != source
            if not changed:
                continue
            source_digest = authoritative_message_digest(source)
            source_ref = context_message_ref(context_id, index, source_digest)
            representation = copy.deepcopy(one[0])
            entries.append(
                {
                    "entry_id": source_ref,
                    "entry_type": "message",
                    "source_ref": source_ref,
                    "source_index": index,
                    "source_digest": source_digest,
                    "role": source.get("role"),
                    "last_version": version,
                    "representations": [
                        {
                            "tier": tier.name.lower(),
                            "version": version,
                            "representation_digest": _representation_digest(representation),
                            "message": representation,
                        }
                    ],
                }
            )
        return entries

    if tier != CompactionTier.SUMMARIZE:
        return entries
    for message in projection_messages:
        if not message.get(SUMMARY_MARKER_KEY):
            continue
        start = int(message.get("_compaction_coverage_start", 0) or 0)
        end = int(message.get("_compaction_coverage_end", 0) or 0)
        if end <= start or end > len(source_messages):
            continue
        source_digest = str(message.get("_compaction_delta_digest") or "")
        if not source_digest:
            source_digest = authoritative_messages_digest(source_messages[start:end])
        source_ref = context_scope_ref(context_id, start, end, source_digest)
        representation = copy.deepcopy(message)
        digest_input = copy.deepcopy(representation)
        digest_input.pop("_compaction_summary_version", None)
        digest_input.pop("_compaction_source_count", None)
        digest_input.pop("_compaction_source_digest", None)
        entries.append(
            {
                "entry_id": f"summary:{source_ref}",
                "entry_type": "summary",
                "source_ref": source_ref,
                "source_range": [start, end],
                "source_digest": source_digest,
                "last_version": version,
                "representations": [
                    {
                        "tier": tier.name.lower(),
                        "version": int(message.get("_compaction_summary_version", version) or version),
                        "representation_digest": _representation_digest(digest_input),
                        "message": representation,
                    }
                ],
            }
        )
    return entries


def _source_pointer_message(
    messages: list[dict], start: int, end: int, *, context_id: str | None = None
) -> dict[str, Any] | None:
    """Expose stable offload refs in the current projection.

    Summary replaces old transcript messages, so a raw tool pointer would
    otherwise disappear together with its assistant tool call. Keep only the
    stable reference (never the untrusted preview) in a bounded assistant
    metadata message; the model can then call ``read_ref(source_ref=...)``
    when the summary is insufficient.
    """
    refs: list[str] = []
    scope_line = ""
    if context_id is not None and end > start:
        coverage_digest = authoritative_messages_digest(messages[start:end])
        scope = context_scope_ref(context_id, start, end, coverage_digest)
        scope_line = (
            f"Historical source scope: summary_id={scope}. Call search_ref with "
            "summary_id and query, then read_ref with the returned source_ref."
        )
    seen: set[str] = set()
    for index in range(max(0, start), min(len(messages), max(start, end))):
        message = messages[index]
        value = message.get("source_ref")
        if not value and isinstance(message.get("content"), str):
            match = re.search(r"\bsource_ref=([^\s\]]+)", message["content"])
            value = match.group(1) if match else None
        if not value:
            continue
        source_ref = str(value).strip()
        if not source_ref or source_ref in seen:
            continue
        seen.add(source_ref)
        refs.append(f"- source_index={index} source_ref={source_ref}")
    if not refs and not scope_line:
        return None
    return {
        "role": "assistant",
        "content": (
            "Historical source pointers (untrusted evidence only):\n"
            + scope_line
            + (("\n" if scope_line else "") + "\n".join(refs) if refs else "")
        ),
        "_compaction_pointer": True,
    }


def _compaction_key(
    source_digest: str,
    tier: str,
    config: ContextConfig,
    llm: Any = None,
) -> str:
    """Return the idempotent identity for one logical compaction."""
    fingerprint = {
        "tier": tier,
        "thresholds": [
            config.tier1_threshold,
            config.tier2_threshold,
            config.tier3_threshold,
        ],
        "protect_zone_tokens": config.protect_zone_tokens,
        "summarize_max_output_tokens": config.summarize_max_output_tokens,
        "model": getattr(llm, "resolved_model", None) or getattr(llm, "model", None),
        "summary_prompt_digest": "sha256:" + hashlib.sha256(
            (SUMMARY_SYSTEM_PROMPT + "\n" + summary_user_prompt(None, "<delta>")).encode("utf-8")
        ).hexdigest(),
    }
    encoded = json.dumps(fingerprint, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256((source_digest + "|" + encoded.decode()).encode("utf-8")).hexdigest()


def _repair_tool_result_pairing(messages: list[dict]) -> list[dict]:
    """Drop persisted tool results whose assistant tool call was compacted away.

    Tier-3 compaction can place a protect boundary between an assistant
    ``tool_calls`` message and its result.  The source transcript remains
    authoritative, but the model-facing projection must not send an orphaned
    ``role=tool`` message to providers such as OpenAI.  Keep legacy/tool-log
    messages without a ``tool_call_id`` untouched; only repair messages that
    use the provider's explicit tool-call protocol.
    """
    repaired: list[dict] = []
    active_tool_ids: set[str] | None = None
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            tool_calls = message.get("tool_calls") or []
            ids = {
                str(tool_call.get("id"))
                for tool_call in tool_calls
                if isinstance(tool_call, dict) and tool_call.get("id")
            }
            active_tool_ids = ids or None
            repaired.append(message)
            continue

        if role == "tool" and message.get("tool_call_id"):
            call_id = str(message["tool_call_id"])
            if active_tool_ids is None or call_id not in active_tool_ids:
                continue
            repaired.append(message)
            active_tool_ids.discard(call_id)
            continue

        if role != "tool":
            active_tool_ids = None
        repaired.append(message)
    return repaired


# Helpers --------------------------------------------------------------------


def _count_msg_tokens(message: dict, counter: TokenCounter) -> int:
    """Approximate token count of a single message (content + tool_calls json)."""
    content = message.get("content")
    total = counter.count_text(content)
    for tc in (message.get("tool_calls") or []):
        total += counter.count_text(json.dumps(tc, ensure_ascii=False))
    return total


def usable_input_budget(
    messages: list[dict],
    tool_specs: list[dict] | None,
    counter: TokenCounter,
    config: ContextConfig,
) -> tuple[int, int, float]:
    """Return ``(total_tokens, usable_budget, utilization)``.

    Tool definitions are part of the request but are reserved separately from
    the historical projection.  This keeps the 60/80/95 thresholds honest when
    a provider has a large tool schema or when output headroom is required.
    """
    categories = counter.categorize(messages, tool_specs)
    total = sum(categories.values())
    actual_tool_tokens = int(categories.get("tool_definitions", 0))
    reserved_tools = max(config.tool_schema_reserve_tokens, actual_tool_tokens)
    usable = max(1, config.context_window - config.output_reserve_tokens - reserved_tools)
    projection_tokens = max(0, total - actual_tool_tokens)
    return total, usable, projection_tokens / usable


def select_compaction_tier(ratio: float, config: ContextConfig) -> CompactionTier:
    """Select the single tier for a pre-compaction utilization ratio."""
    if ratio < config.tier1_threshold:
        return CompactionTier.NONE
    if ratio < config.tier2_threshold:
        return CompactionTier.SNIP
    if ratio < config.tier3_threshold:
        return CompactionTier.PRUNE
    return CompactionTier.SUMMARIZE


def _last_user_idx(messages: list[dict]) -> int | None:
    """Index of the last ``role == user`` message, or None if absent."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            return i
    return None


def _compile_protected_patterns(config: ContextConfig) -> list[re.Pattern]:
    """Compile ``protected_tool_patterns`` regex strings (empty list is OK)."""
    compiled: list[re.Pattern] = []
    for pat in config.protected_tool_patterns:
        try:
            compiled.append(re.compile(pat))
        except re.error:
            # Skip un-compilable patterns rather than crashing compaction.
            continue
    return compiled


def _is_protected_tool(message: dict, compiled: list[re.Pattern]) -> bool:
    """True if the message is a ``role == tool`` whose name matches any pattern."""
    if message.get("role") != "tool":
        return False
    name = message.get("name") or ""
    return any(p.search(name) for p in compiled)


def _is_mandatory(message: dict) -> bool:
    """Deterministically preserve current instructions and working state."""
    return bool(
        message.get("_context_mandatory")
        or message.get("_working_state")
        or message.get("_permission_state")
        or message.get("_unknown_side_effect")
    )


def _snip_lines(text: str, head: int, tail: int) -> str | None:
    """Snip multi-line ``text`` to ``head`` first + omission marker + ``tail`` last.

    Returns ``None`` when the content is too short to snip (``len(lines) <=
    head + tail + 1``), so the caller can treat it as a no-op. A leading newline
    is stripped first (tool outputs sometimes begin with one).
    """
    stripped = text.lstrip("\n")
    lines = stripped.splitlines()
    if len(lines) <= head + tail + 1:
        return None
    skipped = len(lines) - head - tail
    out = lines[:head] + [OMITTED_TEMPLATE.format(n=skipped)]
    if tail > 0:
        out += lines[-tail:]
    return "\n".join(out)


def _snip_code_body(body: str, head: int, tail: int, *, force: bool = False) -> str:
    """Snip the body of a ```` ``` ```` code block.

    When ``force`` is False (Tier 1), content shorter than ``head + tail + 1``
    lines is returned unchanged. When ``force`` is True (Tier 2), the threshold
    check is skipped — any block with more lines than ``head + tail`` is cut.
    Returns the (possibly snipped) body string.
    """
    lines = body.splitlines()
    threshold = (head + tail) if force else (head + tail + 1)
    if len(lines) <= threshold:
        return body
    skipped = len(lines) - head - tail
    out = lines[:head] + [OMITTED_TEMPLATE.format(n=skipped)]
    if tail > 0:
        out += lines[-tail:]
    return "\n".join(out)


# Protect boundary -----------------------------------------------------------


def find_protect_boundary(
    messages: list[dict], counter: TokenCounter, budget_tokens: int
) -> int:
    """Return the slice index ``b`` such that ``messages[b:]`` is the protect zone.

    Walks from the tail accumulating tokens; once the running total reaches
    ``budget_tokens``, returns ``position + 1``. Clamp: the boundary never
    crosses the last ``role == user`` message (so the most recent user input is
    always protected). Returns 0 when the whole list fits the budget (all
    protected).
    """
    if not messages:
        return 0
    cumulative = 0
    boundary = 0
    for i in range(len(messages) - 1, -1, -1):
        if cumulative >= budget_tokens:
            boundary = i + 1
            break
        cumulative += _count_msg_tokens(messages[i], counter)
    # Clamp: never move the boundary past the last user message index.
    last_user = _last_user_idx(messages)
    if last_user is not None and boundary > last_user:
        boundary = last_user
    return boundary


# Tier 1: Snip ---------------------------------------------------------------


def apply_tier1_snip(
    messages: list[dict], protect_until: int, config: ContextConfig
) -> int:
    """Truncate long tool outputs and user code blocks (string-level, zero LLM cost).

    Mutates ``messages[:protect_until]`` in place. Skips: the protect zone,
    ``role == assistant`` content, protected-tool-pattern matches, and content
    shorter than ``head + tail + 1`` lines. Returns the count of modified
    messages (tool outputs + user code blocks).
    """
    compiled = _compile_protected_patterns(config)
    head, tail = config.snip_head_lines, config.snip_tail_lines
    snipped = 0

    for i in range(min(protect_until, len(messages))):
        m = messages[i]
        if _is_mandatory(m) or m.get("role") == "system":
            continue
        role = m.get("role")

        if role == "tool":
            if _is_protected_tool(m, compiled):
                continue
            content = m.get("content")
            if not isinstance(content, str):
                continue
            new = _snip_lines(content, head, tail)
            if new is not None:
                m["content"] = new
                snipped += 1

        elif role == "user":
            content = m.get("content")
            if not isinstance(content, str):
                continue
            new_content = _CODE_FENCE_RE.sub(
                lambda match: _rebuild_fence(match, head, tail, force=False),
                content,
            )
            if new_content != content:
                m["content"] = new_content
                snipped += 1
    return snipped


def _rebuild_fence(match: re.Match, head: int, tail: int, *, force: bool) -> str:
    """Rebuild a ```` ``` ```` fence with a (possibly) snipped body."""
    lang = match.group(1)
    body = match.group(2)
    new_body = _snip_code_body(body, head, tail, force=force)
    if new_body == body:
        return match.group(0)
    return f"```{lang}\n{new_body}\n```"


# Tier 2: Prune --------------------------------------------------------------


def apply_tier2_prune(
    messages: list[dict], protect_until: int, config: ContextConfig
) -> tuple[int, int]:
    """Replace tool outputs with a placeholder and truncate assistant text.

    Mutates ``messages[:protect_until]`` in place. Tool messages keep their
    slot (only ``content`` changes) to preserve the ``tool_use``/``tool_result``
    pairing. Assistant messages keep ``tool_calls`` and are never deleted.
    Summary-marked assistants, protected tools, and the protect zone are
    skipped. Returns ``(tool_pruned, assistant_truncated)`` counts.
    """
    compiled = _compile_protected_patterns(config)
    head, tail = 1, 0  # Tier 2 user-code-block aggressiveness (spec 「Tier 2」).
    pruned_tool = 0
    truncated_asst = 0

    for i in range(min(protect_until, len(messages))):
        m = messages[i]
        if _is_mandatory(m) or m.get("role") == "system":
            continue
        role = m.get("role")

        if role == "tool":
            if _is_protected_tool(m, compiled):
                continue
            content = m.get("content")
            if isinstance(content, str):
                m["content"] = TIER2_TOOL_PLACEHOLDER
                pruned_tool += 1

        elif role == "assistant":
            # Never touch a Tier-3 summary message (self-destruction guard).
            if m.get(SUMMARY_MARKER_KEY) or m.get("_compaction_pointer"):
                continue
            content = m.get("content")
            if not isinstance(content, str) or not content:
                continue
            new = _truncate_assistant(content)
            if new is not None:
                m["content"] = new
                truncated_asst += 1

        elif role == "user":
            content = m.get("content")
            if not isinstance(content, str):
                continue
            new_content = _CODE_FENCE_RE.sub(
                lambda match: _rebuild_fence(match, head, tail, force=True),
                content,
            )
            if new_content != content:
                m["content"] = new_content

    return pruned_tool, truncated_asst


def _truncate_assistant(content: str) -> str | None:
    """Reduce an assistant text to its first sentence (+ truncation marker).

    Returns ``None`` when nothing was actually shortened (no boundary and the
    content is already under the fallback length). Falls back to the first
    ``_FALLBACK_CHARS`` characters when no sentence punctuation is found.
    """
    parts = _SENTENCE_SPLIT_RE.split(content, maxsplit=1)
    if len(parts) > 1:
        return parts[0] + TRUNCATED_MARKER
    if len(content) > _FALLBACK_CHARS:
        return content[:_FALLBACK_CHARS] + TRUNCATED_MARKER
    return None


# Delta size cap (spec 2026-06-12 「Delta 大小上限」 L236-237) ----------------

# Marker prefixed when a Tier-3 delta is truncated to fit the summary budget.
_DELTA_TRUNCATED_TEMPLATE = "... (delta truncated, {n} earlier messages omitted) ..."


def _cap_delta_size(
    rendered_delta: str,
    delta_messages: list[dict],
    config: ContextConfig,
    counter: TokenCounter | None,
) -> str:
    """Enforce the Tier-3 delta size cap (spec L236-237).

    If the serialized delta exceeds ``summarize_max_output_tokens * 4`` tokens
    (default ≈ 8K), truncate to 70% of the budget — keeping the most recent
    messages and dropping earlier ones — and prefix a truncation marker naming
    how many earlier messages were omitted. Prevents the Tier-3 summary LLM call
    from itself overflowing the context window or being silently truncated by
    the provider. When under the cap, ``rendered_delta`` is returned unchanged.
    """
    token_counter = counter if counter is not None else TokenCounter()
    budget = config.summarize_max_output_tokens
    cap = budget * 4
    if token_counter.count_text(rendered_delta) <= cap:
        return rendered_delta

    # Over the cap: keep the most recent messages that fit in 70% of the budget.
    keep_budget = int(budget * 0.7)
    kept: list[dict] = []
    running = 0
    for m in reversed(delta_messages):
        t = token_counter.count_text(_render_messages_for_summary([m]))
        # Always keep at least the most recent message, even if it alone exceeds
        # the budget (an empty delta would starve the summarizer).
        if kept and running + t > keep_budget:
            break
        kept.append(m)
        running += t
    kept.reverse()
    omitted = len(delta_messages) - len(kept)
    body = _render_messages_for_summary(kept)
    return f"{_DELTA_TRUNCATED_TEMPLATE.format(n=omitted)}\n\n{body}"


def _summary_chunks(
    delta_messages: list[dict], config: ContextConfig, counter: TokenCounter | None
) -> list[list[dict]]:
    """Split a delta without gaps; never discard an authoritative message."""
    if not delta_messages:
        return [[]]
    token_counter = counter if counter is not None else TokenCounter()
    cap = max(1, config.summarize_max_output_tokens * 4)
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_tokens = 0
    expanded: list[dict] = []
    for message in delta_messages:
        message_tokens = token_counter.count_text(_render_messages_for_summary([message]))
        if message_tokens > cap:
            content = message.get("content")
            if not isinstance(content, str) or not content:
                raise ValueError("one non-text authoritative message exceeds the summary input budget")
            cursor = 0
            while cursor < len(content):
                end = min(len(content), cursor + max(1, cap * 3))
                part = dict(message)
                part["content"] = content[cursor:end]
                while end > cursor + 1 and token_counter.count_text(
                    _render_messages_for_summary([part])
                ) > cap:
                    end = cursor + max(1, (end - cursor) // 2)
                    part["content"] = content[cursor:end]
                expanded.append(part)
                cursor = end
        else:
            expanded.append(message)
    for message in expanded:
        message_tokens = token_counter.count_text(_render_messages_for_summary([message]))
        if current and current_tokens + message_tokens > cap:
            chunks.append(current)
            current = []
            current_tokens = 0
        current.append(message)
        current_tokens += message_tokens
    if current:
        chunks.append(current)
    return chunks


# Tier 3: Summarize ----------------------------------------------------------


def _find_previous_summary(messages: list[dict]) -> tuple[int, str] | None:
    """Reverse-scan for the most recent compaction summary message.

    Returns ``(index, content)`` for the last ``role == assistant`` message
    carrying the ``_compaction_summary`` marker, or ``None`` if none exists.
    """
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get(SUMMARY_MARKER_KEY):
            content = m.get("content")
            if isinstance(content, str):
                return (i, content)
    return None


async def apply_tier3_summarize(
    messages: list[dict],
    protect_until: int,
    config: ContextConfig,
    llm: Any,
    counter: TokenCounter | None = None,
    *,
    authoritative_messages: list[dict] | None = None,
    authoritative_start: int | None = None,
    authoritative_protect_until: int | None = None,
    context_id: str | None = None,
) -> CompactionStats:
    """Tier 3: LLM-powered incremental summarization.

    Direct callers retain the legacy ``previous summary + delta`` merge. Durable
    ``ContextProjection`` callers instead summarize only the newly covered
    authoritative delta and carry every prior summary fragment forward exactly.
    The projection is rebuilt from the cumulative fragments plus the protected
    source tail; Snip/Prune output is never used as Summary input.

    The serialized delta is capped at ``summarize_max_output_tokens * 4`` tokens
    (spec L236-237): over the cap it is truncated to 70% of the budget with a
    truncation marker, so the summary LLM call cannot itself overflow the
    context window or be silently truncated by the provider.

    Errors are caught and surfaced via ``CompactionStats.error`` — this
    function never raises (Tier 3 failure must not kill the cascade).
    """
    try:
        # 1. Find previous summary
        prev = _find_previous_summary(messages)
        if prev is not None:
            prev_idx, prev_content = prev
            delta_start = prev_idx + 1
        else:
            prev_content = None
            delta_start = 1 if (messages and messages[0].get("role") == "system") else 0
        prior_summaries = [
            copy.deepcopy(message)
            for message in messages
            if message.get(SUMMARY_MARKER_KEY)
        ]

        # 2. Slice delta.  Durable ContextProjection callers provide the
        # authoritative source and its coverage boundary; direct callers keep
        # the historical projection-only behavior for API compatibility.
        if authoritative_messages is not None:
            source_start = max(0, int(authoritative_start or 0))
            source_protect = (
                len(authoritative_messages)
                if authoritative_protect_until is None
                else max(0, min(len(authoritative_messages), authoritative_protect_until))
            )
            delta_messages = [
                message
                for message in authoritative_messages[source_start:source_protect]
                if message.get("role") != "system"
                and not _is_mandatory(message)
                and not message.get("_memory_block")
            ]
        else:
            source_start = delta_start
            source_protect = max(protect_until, delta_start)
            delta_messages = messages[delta_start:source_protect]

        # 3/4. Summarize every authoritative delta message in contiguous
        # chunks. Durable callers merge only chunks from this new delta and
        # preserve older summary fragments byte-for-byte. Direct callers keep
        # the historical previous-summary merge behavior.
        content = "" if authoritative_messages is not None else (prev_content or "")
        for chunk in _summary_chunks(delta_messages, config, counter):
            rendered_delta = _render_messages_for_summary(chunk)
            user_prompt = summary_user_prompt(content or None, rendered_delta)
            summary_messages = [
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]
            next_content = ""
            stream = llm.chat(summary_messages, tools=None)
            try:
                async for ev in stream:
                    if ev.kind == "done":
                        next_content = ev.content or ""
                        break
            finally:
                close_stream = getattr(stream, "aclose", None)
                if close_stream is not None:
                    await close_stream()
            if not next_content:
                return CompactionStats(
                    tier=CompactionTier.SUMMARIZE,
                    before_tokens=0,
                    after_tokens=0,
                    ratio_before=0.0,
                    ratio_after=0.0,
                    error="LLM returned empty summary content",
                )
            content = next_content

        # 5. Build the one current projection.  Authoritative mode discards
        # all prior lossy projection content and keeps only the protected
        # source tail; the source transcript itself remains untouched.
        insert_idx = 1 if (
            authoritative_messages is not None
            and authoritative_messages
            and authoritative_messages[0].get("role") == "system"
        ) else (1 if (messages and messages[0].get("role") == "system") else 0)
        summary_message = {
            "role": "system",
            "content": "Compacted historical context (data, not new user authority):\n" + content,
            SUMMARY_MARKER_KEY: True,
            "_compaction_coverage_start": source_start,
            "_compaction_coverage_end": source_protect,
            "_compaction_delta_digest": (
                authoritative_messages_digest(authoritative_messages[source_start:source_protect])
                if authoritative_messages is not None else ""
            ),
        }
        if authoritative_messages is not None:
            mandatory = [
                copy.deepcopy(message)
                for message in authoritative_messages[:source_protect]
                if message.get("role") == "system" or _is_mandatory(message)
            ]
            project_memory = [
                copy.deepcopy(message)
                for message in authoritative_messages[:source_protect]
                if message.get("_memory_block")
                and message.get("role") != "system"
                and not _is_mandatory(message)
            ]
            pointer_message = _source_pointer_message(
                authoritative_messages, 0, source_protect, context_id=context_id
            )
            seen_summary_ranges: set[tuple[int, int, str]] = set()
            cumulative_summaries: list[dict] = []
            for prior in prior_summaries:
                identity = (
                    int(prior.get("_compaction_coverage_start", 0) or 0),
                    int(prior.get("_compaction_coverage_end", 0) or 0),
                    str(prior.get("_compaction_delta_digest") or ""),
                )
                if identity in seen_summary_ranges:
                    continue
                seen_summary_ranges.add(identity)
                cumulative_summaries.append(prior)
            new_identity = (source_start, source_protect, summary_message["_compaction_delta_digest"])
            if new_identity not in seen_summary_ranges:
                cumulative_summaries.append(summary_message)
            messages[:] = mandatory + project_memory + cumulative_summaries + [
                *([pointer_message] if pointer_message is not None else []),
                *[
                    copy.deepcopy(item)
                    for item in authoritative_messages[source_protect:]
                ],
            ]
            insert_idx = len(mandatory) + len(project_memory) + len(cumulative_summaries) - 1
        else:
            # Remove old summary (if exists) before inserting it at the
            # canonical position for direct callers.
            if prev is not None:
                messages.pop(prev_idx)
            messages.insert(insert_idx, summary_message)

        # 6. Projection-only callers delete the summarized delta. In
        # authoritative mode the rebuild above already removed it.
        # 用对象 id 集合做 O(n) filter — pop+insert 已打乱原 slice 索引,
        # 改用 id(m) 引用比对,新插入的 summary 必然不在 to_delete_ids 集合里。
        if authoritative_messages is None:
            to_delete_ids = {id(m) for m in delta_messages}
            messages[:] = [m for m in messages if id(m) not in to_delete_ids]

        return CompactionStats(
            tier=CompactionTier.SUMMARIZE,
            before_tokens=0,    # filled by maybe_compact
            after_tokens=0,
            ratio_before=0.0,   # filled by maybe_compact
            ratio_after=0.0,
            summarized=True,
            summary_index=insert_idx,
            source_range=(0, source_protect) if authoritative_messages is not None else None,
            delta_range=(source_start, source_protect) if authoritative_messages is not None else None,
        )
    except Exception as e:  # noqa: BLE001 — Tier 3 fail-soft
        return CompactionStats(
            tier=CompactionTier.SUMMARIZE,
            before_tokens=0,
            after_tokens=0,
            ratio_before=0.0,
            ratio_after=0.0,
            error=str(e),
        )


# Orchestrator ---------------------------------------------------------------


def _noop_stats(
    messages: list[dict],
    counter: TokenCounter,
    tool_specs: list[dict] | None,
    config: ContextConfig,
) -> CompactionStats:
    total, _usable, ratio = usable_input_budget(messages, tool_specs, counter, config)
    return CompactionStats(
        tier=CompactionTier.NONE,
        before_tokens=total,
        after_tokens=total,
        ratio_before=ratio,
        ratio_after=ratio,
    )


async def maybe_compact(
    messages: list[dict],
    tool_specs: list[dict] | None,
    counter: TokenCounter,
    config: ContextConfig,
    llm: Any = None,
    *,
    authoritative_messages: list[dict] | None = None,
    context_id: str | None = None,
) -> CompactionStats:
    """Apply exactly one compaction tier in place on ``messages``.

    The tier is selected from the pre-compaction utilization: Snip for
    ``tier1 <= ratio < tier2``, Prune for ``tier2 <= ratio < tier3`` and
    Summary for ``ratio >= tier3``.  Lower-tier output is never fed into a
    higher tier. Any exception is caught and surfaced via
    ``CompactionStats.error`` — this function never raises.
    """
    if not config.enabled:
        return _noop_stats(messages, counter, tool_specs, config)

    before = after = 0
    ratio = 0.0
    snapshot: list[dict] | None = None
    try:
        before, _usable, ratio = usable_input_budget(messages, tool_specs, counter, config)

        if ratio < config.tier1_threshold:
            return CompactionStats(
                tier=CompactionTier.NONE,
                before_tokens=before,
                after_tokens=before,
                ratio_before=ratio,
                ratio_after=ratio,
            )

        protect_until = find_protect_boundary(
            messages, counter, config.protect_zone_tokens
        )
        if protect_until == 0 or protect_until >= len(messages):
            return CompactionStats(
                tier=CompactionTier.NONE,
                before_tokens=before,
                after_tokens=before,
                ratio_before=ratio,
                ratio_after=ratio,
            )

        # Select one mutually-exclusive tier from the original pressure.
        if ratio < config.tier2_threshold:
            snipped = apply_tier1_snip(messages, protect_until, config)
            after, _usable_after, ratio_after = usable_input_budget(
                messages, tool_specs, counter, config
            )
            stats = CompactionStats(
                tier=CompactionTier.SNIP,
                before_tokens=before,
                after_tokens=after,
                ratio_before=ratio,
            ratio_after=ratio_after,
            messages_snip=snipped,
        )
            return stats

        if ratio < config.tier3_threshold:
            pruned_tool, truncated_asst = apply_tier2_prune(messages, protect_until, config)
            after, _usable_after, ratio_after = usable_input_budget(
                messages, tool_specs, counter, config
            )
            stats = CompactionStats(
                tier=CompactionTier.PRUNE,
                before_tokens=before,
                after_tokens=after,
                ratio_before=ratio,
                ratio_after=ratio_after,
                messages_prune=pruned_tool,
                messages_assistant_truncated=truncated_asst,
            )
            return stats

        # Tier 3: Summary from authoritative source facts only.
        authoritative_start = 0
        if authoritative_messages:
            previous = _find_previous_summary(messages)
            if previous is not None:
                marker = messages[previous[0]]
                authoritative_start = int(marker.get("_compaction_coverage_end", 0) or 0)
            if authoritative_start <= 0:
                authoritative_start = (
                    1 if authoritative_messages[0].get("role") == "system" else 0
                )
            authoritative_protect = find_protect_boundary(
                authoritative_messages, counter, config.protect_zone_tokens
            )
        else:
            authoritative_protect = None
        stats = await apply_tier3_summarize(
            messages,
            protect_until,
            config,
            llm,
            counter=counter,
            authoritative_messages=authoritative_messages,
            authoritative_start=authoritative_start,
            authoritative_protect_until=authoritative_protect,
            context_id=context_id,
        )
        attempts = 0
        while stats.error and not stats.summarized and attempts < config.summary_retry_limit:
            attempts += 1
            stats = await apply_tier3_summarize(
                messages,
                protect_until,
                config,
                llm,
                counter=counter,
                authoritative_messages=authoritative_messages,
                authoritative_start=authoritative_start,
                authoritative_protect_until=authoritative_protect,
                context_id=context_id,
            )
        after, _usable_after, ratio_after = usable_input_budget(
            messages, tool_specs, counter, config
        )
        stats.before_tokens = before
        stats.after_tokens = after
        stats.ratio_before = ratio
        stats.ratio_after = ratio_after
        return stats

    except Exception as e:  # noqa: BLE001 — spec mandates fail-soft
        if snapshot is None:
            snapshot = [dict(m) for m in messages]
        return CompactionStats(
            tier=CompactionTier.NONE,
            before_tokens=before,
            after_tokens=after,
            ratio_before=ratio,
            ratio_after=ratio,
            error=str(e),
            before_snapshot=snapshot,
        )
