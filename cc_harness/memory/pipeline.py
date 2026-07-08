"""Context-threshold-triggered auto-extraction pipeline.

Reads recent messages, calls LLM to extract 1-3 candidate memories,
then calls MemoryService.save() for each (which runs the full
embed → search → decide → apply flow including ADD/UPDATE/DELETE/NOOP).
"""
from __future__ import annotations
import json
import re
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
    ):
        self._llm = llm
        self._service = service
        self.threshold = threshold
        self.recent_turns = recent_turns
        self.max_delta_tokens = max_delta_tokens

    async def maybe_run(
        self, messages: list[dict], counter: TokenCounter, context_window: int,
    ) -> PipelineResult | None:
        if context_window <= 0:
            return None
        cats = counter.categorize(messages, tools=None)
        total = sum(cats.values())
        ratio = total / context_window
        if ratio < self.threshold:
            return None

        delta = self._recent_turns(messages)
        delta_text = self._render_delta(delta)
        delta_text = self._truncate_to_tokens(delta_text, counter, self.max_delta_tokens)

        try:
            candidates = await self._extract(delta_text)
        except Exception as e:
            return PipelineResult(results=[], error=f"{type(e).__name__}: {e}", ratio=ratio)

        results = []
        for text in candidates:
            try:
                r = await self._service.save(text, source="pipeline")
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
        return filtered[-self.recent_turns * 2:]  # 1 turn ≈ 2 条 (user+assistant)

    def _render_delta(self, delta: list[dict]) -> str:
        out = []
        for m in delta:
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, list):
                content = "<multimodal>"
            out.append(f"[{role}] {content}")
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
        return [str(t).strip() for t in data.get("memories", []) if str(t).strip()]
