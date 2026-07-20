"""TTL 过期清理: staleness >= threshold 删除。"""
from __future__ import annotations
import json
import time
from pathlib import Path
from cc_harness.memory.store import MemoryStore


async def purge_stale(store: MemoryStore, *,
                      staleness_threshold: float = 0.85,
                      limit: int = 100) -> list[str]:
    """删 staleness >= threshold 的记忆, 限 limit 条, 审计写 logs/memory_maintenance.jsonl。

    threshold 默认 0.85, 绝不 < 0.7。
    """
    if staleness_threshold < 0.7:
        staleness_threshold = 0.7
    mems = await store.list_with_staleness(
        staleness_min=staleness_threshold, staleness_max=1.0, limit=limit
    )
    deleted_ids: list[str] = []
    for m in mems:
        if await store.delete(m.id):
            deleted_ids.append(m.id)
    if deleted_ids:
        _audit(deleted_ids, staleness_threshold)
    return deleted_ids


def _audit(deleted_ids: list[str], threshold: float) -> None:
    log_path = Path("logs") / "memory_maintenance.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": time.time(), "op": "ttl",
            "deleted_ids": deleted_ids, "threshold": threshold,
        }, ensure_ascii=False) + "\n")
