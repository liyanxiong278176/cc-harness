"""Per-query top-k retrieval + injection-block formatting."""
from __future__ import annotations
import time


def _format_age(ts: float) -> str:
    delta = time.time() - ts
    if delta < 3600:
        return f"{int(delta / 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta / 3600)} 小时前"
    return f"{int(delta / 86400)} 天前"


class MemoryRetriever:
    def __init__(self, store, embedder, top_k: int = 5, token_budget: int = 800):
        self._store = store
        self._embedder = embedder
        self.top_k = top_k
        self.token_budget = token_budget

    async def search(self, query: str, top_k: int = 5) -> list:
        embedding = await self._embedder.embed(query)
        return await self._store.search_similar(embedding, k=top_k)

    async def build_injection_block(self, query: str) -> str:
        if not (query or "").strip():
            return ""
        try:
            results = await self.search(query, top_k=self.top_k)
        except Exception:
            return ""
        if not results:
            return ""

        header = "## 相关记忆(本轮检索)"
        lines = [header]
        char_used = len(header)
        for mem, _distance in results:
            age = _format_age(mem.updated_at)
            line = f"- {mem.text}  [源: {mem.source}, {age}]"
            if char_used + len(line) + 1 > self.token_budget * 2:
                break
            lines.append(line)
            char_used += len(line) + 1

        if len(lines) == 1:  # only header
            return ""
        return "\n".join(lines)
