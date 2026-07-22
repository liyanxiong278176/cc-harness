"""Per-query top-k retrieval + injection-block formatting."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cc_harness.memory.store import Memory


def _format_age(ts: float) -> str:
    delta = time.time() - ts
    if delta < 3600:
        return f"{int(delta / 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta / 3600)} 小时前"
    return f"{int(delta / 86400)} 天前"


class MemoryRetriever:
    def __init__(self, store, embedder, top_k: int = 5, token_budget: int = 800, drift_detector=None):  # E5
        self._store = store
        self._embedder = embedder
        self.top_k = top_k
        self.token_budget = token_budget
        self.drift_detector = drift_detector

    async def search(self, query: str, top_k: int = 5) -> list:
        embedding = await self._embedder.embed(query)
        results = await self._store.search_similar(embedding, k=top_k * 2)
        if results:
            ids = [m.id for m, _ in results]
            try:
                await self._store.touch_recall(ids)
            except Exception:
                pass
        from cc_harness.memory.maintenance.recall_weight import RecallWeighter
        weighter = RecallWeighter()
        weighted = weighter.apply(results)

        # E5 drift 检测(召出后, ≥2 同 entity 才判)
        if self.drift_detector is not None and weighted:
            try:
                # weighted 是 list[(Memory, distance)],DriftDetector 要 list[Memory],拆 tuple
                results_list = [m for m, _ in weighted]
                await self.drift_detector.check_after_read(
                    session_id=results_list[0].session_id or "default",
                    turn_idx=0,  # 占位,真实 turn_idx 由 T2.2 注入
                    results=results_list,
                )
            except Exception:
                pass  # E5 fail-soft 不阻塞主 search

        return weighted[:top_k]

    async def search_hybrid(
        self, query: str, top_k: int = 5, alpha: float = 0.5, rrf_k: int = 60,
    ) -> list:
        """混合召回:vector + FTS5 → RRF 合并(Phase 4)。

        alpha: vec vs fts 权重(0=纯 FTS,1=纯 vec,默认 0.5=平衡)。
        rrf_k: RRF 平滑常数(论文 60)。

        算法:
        1. 并发跑 vector search + FTS5 BM25 search
        2. 每个 hit 算 RRF score = alpha/(vec_rank+rrf_k) + (1-alpha)/(fts_rank+rrf_k)
           (未在某路召回的 → 那一路分数为 0,等价于 1/rrf_k)
        3. 按 RRF 降序取 top_k

        FTS5 不可用时 → 退化为纯 vector search(向后兼容)。
        """
        import asyncio
        # 并行查两个
        vec_task = asyncio.create_task(self._search_vec_only(query, top_k * 2))
        fts_task = asyncio.create_task(self._search_fts_only(query, top_k * 2))
        vec_results, fts_results = await asyncio.gather(vec_task, fts_task)

        # 建 (id → (mem, vec_rank, fts_rank))
        scores: dict[str, tuple[Memory, float, float]] = {}
        for rank, (mem, _dist) in enumerate(vec_results, 1):
            scores[mem.id] = (mem, 1.0 / (rank + rrf_k), 0.0)
        for rank, (mem, _bm25) in enumerate(fts_results, 1):
            fts_score = 1.0 / (rank + rrf_k)
            if mem.id in scores:
                mem, vec_s, _ = scores[mem.id]
                scores[mem.id] = (mem, vec_s, fts_score)
            else:
                scores[mem.id] = (mem, 0.0, fts_score)

        # RRF 加权合并
        merged = []
        for mem, vec_s, fts_s in scores.values():
            rrf = alpha * vec_s + (1 - alpha) * fts_s
            merged.append((mem, rrf))
        merged.sort(key=lambda x: -x[1])
        return merged[:top_k]

    async def _search_vec_only(self, query: str, k: int) -> list:
        try:
            return await self.search(query, top_k=k)
        except Exception:
            return []

    async def _search_fts_only(self, query: str, k: int) -> list:
        try:
            return await self._store.search_fts(query, k=k)
        except Exception:
            return []

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
