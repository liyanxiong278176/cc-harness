"""staleness 算子 + LLM 复检(中间区 0.4-0.7)。"""
from __future__ import annotations
import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cc_harness.memory.store import Memory


def compute_staleness(mem: "Memory", *, now: float,
                      recall_count: int = 0,
                      last_recalled_at: float | None = None,
                      half_life_days: float = 30.0) -> float:
    """0.0 (新且活跃) ~ 1.0 (极老且从未被召)。

    age_score   = 1 - 0.5 ** (age_days / half_life_days)
    usage_score = 1 - exp(-recall_count / 5)
    base        = 0.6 * age_score + 0.4 * usage_score
    """
    if half_life_days <= 0:
        half_life_days = 30.0
    age_days = max(0.0, (now - mem.updated_at) / 86400.0)
    age_score = 1.0 - 0.5 ** (age_days / half_life_days)
    usage_score = 1.0 - (2.71828 ** (-recall_count / 5.0))
    base = 0.6 * age_score + 0.4 * usage_score
    return max(0.0, min(1.0, base))


class LLMRechecker:
    def __init__(self, llm, *, batch_size: int = 20):
        self._llm = llm
        self.batch_size = batch_size

    async def recheck_midrange(self, mids_staleness: list[tuple[str, float, str]]
                               ) -> dict[str, float]:
        """mids_staleness: [(id, staleness, text), ...], 仅处理 0.4-0.7 中间区。
        失败保留算子结果(返回空 dict 或 partial)。"""
        midrange = [(i, s, t) for i, s, t in mids_staleness if 0.4 <= s < 0.7]
        if not midrange or self._llm is None:
            return {}
        out: dict[str, float] = {}
        for chunk_start in range(0, len(midrange), self.batch_size):
            chunk = midrange[chunk_start:chunk_start + self.batch_size]
            try:
                scores = await self._ask_llm(chunk)
                out.update(scores)
            except Exception:
                continue
        return out

    async def _ask_llm(self, chunk: list[tuple[str, float, str]]) -> dict[str, float]:
        items = [{"id": i, "staleness": s, "text": t} for i, s, t in chunk]
        prompt = (
            "Rate each memory's continued usefulness on 0-1. "
            "Reply JSON {\"scores\": [{\"id\": \"...\", \"score\": 0.5}, ...]}\n\n"
            + json.dumps(items, ensure_ascii=False)
        )
        content_parts: list[str] = []
        async for ev in self._llm.chat(
            [{"role": "user", "content": prompt}], tools=None
        ):
            if ev.kind == "content":
                content_parts.append(ev.text)
            elif ev.kind == "done" and ev.content:
                content_parts = [ev.content]
        full = "".join(content_parts).strip()
        m = re.search(r"\{.*\}", full, re.DOTALL)
        if not m:
            return {}
        data = json.loads(m.group(0))
        return {x["id"]: float(x["score"]) for x in data.get("scores", [])}
