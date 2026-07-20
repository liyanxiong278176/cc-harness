"""Consolidation: cluster 相似的, merge/update/noop。"""
from __future__ import annotations
import json
import math
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cc_harness.memory.store import MemoryStore


def _greedy_cluster(mems: list, threshold: float) -> list[list]:
    """O(N²) 贪心 cluster, 按向量欧氏距离。距离 < threshold 归一簇。"""
    n = len(mems)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for i in range(n):
        for j in range(i + 1, n):
            if not mems[i].embedding or not mems[j].embedding:
                continue
            d = math.sqrt(sum((a - b) ** 2 for a, b in zip(mems[i].embedding, mems[j].embedding)))
            if d < threshold:
                union(i, j)

    groups: dict[int, list] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(mems[i])
    return list(groups.values())


async def consolidate(store: "MemoryStore", embedder, llm=None, *,
                      similarity_threshold: float = 0.15,
                      max_cluster_size: int = 5) -> int:
    """全库扫一次, cluster 相似, merge/update/noop。返回受影响条数。"""
    cur = await store._db.execute(
        "SELECT id, text, embedding, created_at, updated_at, source, layer, session_id "
        "FROM memories LIMIT 500"
    )
    rows = await cur.fetchall()
    if not rows:
        return 0
    from cc_harness.memory.store import Memory, _blob_to_vec
    mems = [
        Memory(
            id=r[0], text=r[1], embedding=_blob_to_vec(r[2]),
            created_at=r[3], updated_at=r[4], source=r[5],
            layer=r[6], session_id=r[7],
        )
        for r in rows
    ]
    clusters = _greedy_cluster(mems, similarity_threshold)
    affected = 0
    for cluster in clusters:
        if len(cluster) < 2 or len(cluster) > max_cluster_size:
            continue
        cluster.sort(key=lambda m: m.created_at)
        if llm is None:
            keep = cluster[0]
            for m in cluster[1:]:
                if await store.delete(m.id):
                    affected += 1
            continue
        try:
            action = await _ask_llm_action(cluster, llm)
        except Exception:
            action = "noop"
        if action == "noop":
            continue
        if action == "merge":
            try:
                merged_text = await _ask_llm_merge(cluster, llm)
            except Exception:
                continue
            if not merged_text:
                continue
            try:
                new_emb = await embedder.embed(merged_text)
            except Exception:
                continue
            cluster_id = f"cluster-{cluster[0].id[:6]}"
            merged_from = json.dumps([m.id for m in cluster])
            for m in cluster:
                if await store.delete(m.id):
                    pass
            new_mem = await store.add(merged_text, new_emb, "consolidation", session_id=None)
            await store._db.execute(
                "UPDATE memories SET cluster_id = ?, merged_from = ? WHERE id = ?",
                (cluster_id, merged_from, new_mem.id),
            )
            await store._db.commit()
            affected += len(cluster)
        elif action == "update":
            keep = cluster[-1]
            try:
                new_text = await _ask_llm_merge(cluster, llm)
            except Exception:
                continue
            if not new_text:
                continue
            try:
                new_emb = await embedder.embed(new_text)
            except Exception:
                continue
            await store.update(keep.id, new_text, new_emb)
            for m in cluster[:-1]:
                if await store.delete(m.id):
                    affected += 1
    return affected


async def _ask_llm_action(cluster: list, llm) -> str:
    items = [{"id": m.id, "text": m.text, "created_at": m.created_at} for m in cluster]
    prompt = (
        "Decide action: merge (replace all with one new), update (merge into newest), or noop. "
        "Reply JSON {\"action\": \"merge\"|\"update\"|\"noop\"}\n\n"
        + json.dumps(items, ensure_ascii=False)
    )
    content_parts: list[str] = []
    async for ev in llm.chat(
        [{"role": "user", "content": prompt}], tools=None
    ):
        if ev.kind == "content":
            content_parts.append(ev.text)
        elif ev.kind == "done" and ev.content:
            content_parts = [ev.content]
    full = "".join(content_parts).strip()
    m = re.search(r"\{.*\}", full, re.DOTALL)
    if not m:
        return "noop"
    try:
        data = json.loads(m.group(0))
    except Exception:
        return "noop"
    a = data.get("action", "noop")
    return a if a in ("merge", "update", "noop") else "noop"


async def _ask_llm_merge(cluster: list, llm) -> str:
    items = [{"id": m.id, "text": m.text} for m in cluster]
    prompt = (
        "Merge these into a single concise memory, preserving all unique facts. "
        "Reply JSON {\"merged_text\": \"...\"}\n\n"
        + json.dumps(items, ensure_ascii=False)
    )
    content_parts: list[str] = []
    async for ev in llm.chat(
        [{"role": "user", "content": prompt}], tools=None
    ):
        if ev.kind == "content":
            content_parts.append(ev.text)
        elif ev.kind == "done" and ev.content:
            content_parts = [ev.content]
    full = "".join(content_parts).strip()
    m = re.search(r"\{.*\}", full, re.DOTALL)
    if not m:
        return ""
    try:
        data = json.loads(m.group(0))
    except Exception:
        return ""
    return str(data.get("merged_text", "")).strip()
