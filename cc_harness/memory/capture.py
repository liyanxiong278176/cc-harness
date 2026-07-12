"""L0 对话录制 — after-turn 把 messages 写 conversation 表(幂等)。"""
from __future__ import annotations
import time


async def capture(store, session_id: str, messages: list[dict], turn_idx: int) -> None:
    """录 messages(非 system)到 conversation 表。

    幂等:先删同 session+turn_idx 再插(重录不翻倍)。跳 system(role=="system")。
    multimodal content(list)→ "<multimodal>" 占位。
    """
    assert store._db is not None
    await store._db.execute(
        "DELETE FROM conversation WHERE session_id=? AND turn_idx=?",
        (session_id, turn_idx))
    ts = time.time()
    for m in messages:
        role = m.get("role", "?")
        if role == "system":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            content = "<multimodal>"
        await store._db.execute(
            "INSERT INTO conversation(session_id,turn_idx,role,content,ts) VALUES(?,?,?,?,?)",
            (session_id, turn_idx, role, str(content), ts))
    await store._db.commit()
