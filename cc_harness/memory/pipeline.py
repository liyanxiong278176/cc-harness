"""Context-threshold-triggered auto-extraction pipeline.

Reads recent messages, calls LLM to extract 1-3 candidate memories,
then calls MemoryService.save() for each (which runs the full
embed → search → decide → apply flow including ADD/UPDATE/DELETE/NOOP).
"""
from __future__ import annotations
import json
import re
import hashlib
import time
from dataclasses import dataclass
from cc_harness.prompts import MEMORY_EXTRACT_SYSTEM_PROMPT, memory_extract_user_prompt
from cc_harness.tokens import TokenCounter


@dataclass
class PipelineResult:
    results: list   # list[SaveResult]
    error: str | None = None
    ratio: float = 0.0


class MemoryPipeline:
    def __init__(
        self, llm, service,
        threshold: float = 0.55,
        recent_turns: int = 10,
        max_delta_tokens: int = 4000,
        verified_sources: bool = False,
    ):
        self._llm = llm
        self._service = service
        self.threshold = threshold
        self.recent_turns = recent_turns
        self.max_delta_tokens = max_delta_tokens
        self.verified_sources = verified_sources

    async def maybe_run(
        self, messages: list[dict], counter: TokenCounter, context_window: int,
        *, session_id: str | None = None, turn_idx: int | None = None,
        every_n: int | None = None,
    ) -> PipelineResult | None:
        # 双触发:every-N(turn_idx % every_n == 0) OR ratio(>=threshold)。
        # every-N 不依赖 context_window,故 context_window<=0 时仍可由 every-N 命中。
        every_n_hit = (turn_idx is not None and every_n is not None and every_n > 0
                       and turn_idx % every_n == 0)
        cats = counter.categorize(messages, tools=None)
        total = sum(cats.values())
        ratio = (total / context_window) if context_window > 0 else 0.0
        ratio_hit = context_window > 0 and ratio >= self.threshold
        if not (every_n_hit or ratio_hit):
            return None

        delta = self._recent_turns(messages)
        delta_text = self._render_delta(delta)
        delta_text = self._truncate_to_tokens(delta_text, counter, self.max_delta_tokens)

        try:
            candidates = await self._extract(delta_text)
        except Exception as e:
            return PipelineResult(results=[], error=f"{type(e).__name__}: {e}", ratio=ratio)

        results = []
        for candidate in candidates:
            try:
                if self.verified_sources:
                    if not isinstance(candidate, dict):
                        continue
                    index = candidate.get("source_index")
                    if not isinstance(index, int) or not 0 <= index < len(delta):
                        continue
                    message = delta[index]
                    quote = candidate.get("quote")
                    if not isinstance(quote, str) or not quote.strip() or quote not in str(message.get("content", "")):
                        continue
                    kind = candidate.get("kind")
                    if kind not in {"preference", "project_fact", "decision"}:
                        continue
                    role = message.get("role")
                    if role == "user":
                        if kind not in {"preference", "decision"}:
                            continue
                        if message.get("_cc_harness_untrusted") or (message.get("_context_mandatory")
                            and not message.get("_memory_explicit_user") and not message.get("_cc_harness_resume_reason")):
                            continue
                    elif role == "tool" and kind == "project_fact":
                        observation = json.loads(message.get("content", "{}"))
                        if observation.get("status") != "succeeded" or not observation.get("observation_id"):
                            continue
                        if quote not in str(observation.get("content", "")):
                            continue
                    else:
                        continue
                    from cc_harness.l5 import KeyRegexLayer
                    if KeyRegexLayer().find(quote):
                        continue
                    source_digest = hashlib.sha256(f"{role}\x00{message.get('content', '')}".encode()).hexdigest()
                    if await self._service.store.retracted("source", source_digest):
                        continue
                    # Store the verified excerpt, never an unsupported paraphrase.
                    provenance = {"source_digest": source_digest, "source_index": index, "kind": kind,
                                  "verified_at": time.time(), "authority": "advisory",
                                  "memory_epoch": await self._service.store.generation()}
                    r = await self._service.save(quote.strip(), source="pipeline", session_id=session_id,
                                                 provenance=provenance)
                else:
                    text = candidate.get("text", candidate.get("quote", "")) if isinstance(candidate, dict) else candidate
                    r = await self._service.save(text, source="pipeline", session_id=session_id)
                results.append(r)
            except Exception as e:
                from cc_harness.memory.service import SaveResult
                results.append(SaveResult(action="ERROR", error=f"{type(e).__name__}: {e}"))

        return PipelineResult(results=results, ratio=ratio)

    def _recent_turns(self, messages: list[dict]) -> list[dict]:
        # 跳过 system + _compaction_summary + _memory_block
        filtered = [
            m for m in messages
            if m.get("role") not in ("system",)
            and not m.get("_compaction_summary")
            and not m.get("_memory_block")
        ]
        recent = filtered[-self.recent_turns * 2:]
        return [{**m, "content": m["_memory_user_source"], "_memory_explicit_user": True}
                if isinstance(m.get("_memory_user_source"), str) else m for m in recent]

    def _render_delta(self, delta: list[dict]) -> str:
        out = []
        for index, m in enumerate(delta):
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, list):
                content = "<multimodal>"
            out.append(f"[source_index={index} role={role}] {content}")
        return "\n\n".join(out)

    def _truncate_to_tokens(self, text: str, counter: TokenCounter, max_tokens: int) -> str:
        if counter.count_text(text) <= max_tokens:
            return text
        # 简单截断(粗略 4 chars/token)
        max_chars = max_tokens * 4
        return text[:max_chars] + "\n... (delta truncated)"

    async def _extract(self, delta_text: str) -> list[str]:
        msgs = [
            {"role": "system", "content": MEMORY_EXTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": memory_extract_user_prompt(delta_text)},
        ]
        content_parts: list[str] = []
        async for ev in self._llm.chat(msgs, tools=None):
            if ev.kind == "content":
                content_parts.append(ev.text)
            elif ev.kind == "done" and ev.content:
                content_parts = [ev.content]
        full = "".join(content_parts).strip()
        m = re.search(r"\{.*\}", full, re.DOTALL)
        if not m:
            return []
        data = json.loads(m.group(0))
        return [t if isinstance(t, dict) else str(t).strip() for t in data.get("memories", []) if t]
