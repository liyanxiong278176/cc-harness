"""矛盾检测: write-time + maintenance 全库扫。"""
from __future__ import annotations
import json
from dataclasses import dataclass
from unittest.mock import MagicMock


VALID_VERDICTS = {"contradicts", "supersedes", "elaborates", "unrelated"}
VALID_ACTIONS = {"delete_old", "delete_new", "merge", "noop"}


@dataclass
class ConflictVerdict:
    other_id: str
    verdict: str
    action: str


class ConflictDetector:
    def __init__(self, llm):
        self._llm = llm

    async def check(self, new_mem, similar: list) -> list[ConflictVerdict]:
        """similar: list[tuple[Memory, float]] (来自 store.search_similar)。
        返回 0-N 个 verdict。LLM 失败返空。"""
        if not similar or self._llm is None:
            return []
        # 解 (Memory, score) tuple;defensive 兜 tuple / dict / Memory 三种形态
        def _mem_obj(x):
            if hasattr(x, "id") and hasattr(x, "text"):
                return x
            if isinstance(x, tuple):
                for item in x:
                    if hasattr(item, "id") and hasattr(item, "text"):
                        return item
                    if isinstance(item, dict) and "id" in item and "text" in item:
                        return item
            if isinstance(x, dict) and "id" in x and "text" in x:
                return x
            return None
        items = []
        for raw in similar:
            m = _mem_obj(raw)
            if m is not None:
                items.append({"id": m.id, "text": m.text})
        new_obj = _mem_obj(new_mem)
        if new_obj is None:
            return []
        prompt = (
            "Compare the new memory to each existing. For each, classify as "
            "contradicts/supersedes/elaborates/unrelated and pick action "
            "delete_old/delete_new/merge/noop. "
            "Reply JSON {\"verdicts\": [{\"other_id\": \"...\", \"verdict\": \"...\", \"action\": \"...\"}, ...]}\n\n"
            f"NEW: {json.dumps({'id': new_obj.id, 'text': new_obj.text}, ensure_ascii=False)}\n"
            f"EXISTING: {json.dumps(items, ensure_ascii=False)}"
        )
        try:
            content_parts: list[str] = []
            async for ev in self._llm.chat(
                [{"role": "user", "content": prompt}], tools=None
            ):
                if ev.kind == "content":
                    content_parts.append(ev.text)
                elif ev.kind == "done" and ev.content:
                    content_parts = [ev.content]
            full = "".join(content_parts).strip()
        except Exception:
            return []
        # 首个平衡 JSON 对象解析(容忍尾随解释文本 / 多余 })
        start = full.find("{")
        if start == -1:
            return []
        try:
            data, _ = json.JSONDecoder().raw_decode(full[start:])
        except Exception:
            return []
        out: list[ConflictVerdict] = []
        for v in data.get("verdicts", []):
            verdict = v.get("verdict", "unrelated")
            action = v.get("action", "noop")
            if verdict not in VALID_VERDICTS or action not in VALID_ACTIONS:
                continue
            if verdict == "unrelated" or action == "noop":
                continue
            out.append(ConflictVerdict(other_id=v["other_id"], verdict=verdict, action=action))
        return out

    async def scan_all(self, store, embedder) -> int:
        """maintenance 用: 全库扫, 找矛盾对。LLM 不可用返 0。"""
        if self._llm is None or embedder is None:
            return 0
        cur = await store._db.execute("SELECT id, text FROM memories LIMIT 500")
        rows = await cur.fetchall()
        if len(rows) < 2:
            return 0
        affected = 0
        for r in rows:
            mid, text = r[0], r[1]
            try:
                emb = await embedder.embed(text)
                similar = await store.search_similar(emb, k=3)
            except Exception:
                continue
            similar = [m for m in similar if m.id != mid]
            if not similar:
                continue
            new_mock = MagicMock(id=mid, text=text)
            verdicts = await self.check(new_mock, similar)
            for v in verdicts:
                if v.action == "delete_old":
                    if await store.delete(v.other_id):
                        affected += 1
        return affected
