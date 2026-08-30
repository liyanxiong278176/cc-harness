"""L0 对话录制 — after-turn 把 messages 写 conversation 表(幂等)。

Phase 3 Q1 uplift: 每条 message 同步调 extract 抽 dates/entities/keywords,
存为独立列(用 US `\x1f` 分隔多值,SQLite FTS/grep 友好)。
"""
from __future__ import annotations
import hashlib
import time

# Unit Separator — 不会出现在正常文本里,适合做 list→str 序列化分隔符
_US = "\x1f"


def _join(values: list[str]) -> str:
    """list[str] → US 分隔字符串。空 list → 空串。"""
    return _US.join(v for v in values if v) if values else ""


async def capture(store, session_id: str, messages: list[dict], turn_idx: int) -> int:
    """录 messages(非 system)到 conversation 表。

    幂等:先删同 session+turn_idx 再插(重录不翻倍)。跳 system(role=="system")。
    multimodal content(list)→ "<multimodal>" 占位。

    Phase 3: 同时抽 dates/entities/keywords 存到独立列,后续 recall 可用做过滤。
    """
    # 局部 import 避免 capture 模块顶依赖 cc_harness.memory.extract(可能循环)
    from cc_harness.memory.extract import extract_dates, extract_entities, extract_keywords

    assert store._db is not None
    # The capture operation is a multi-row write.  Serialize it with the
    # other services sharing this connection, while retaining a SAVEPOINT so
    # callers that already opened a transaction can still use capture.
    async with store.write_lock:
        await store._db.execute("SAVEPOINT capture_sp")
        try:
            ts = time.time()
            inserted = 0
            for message_idx, m in enumerate(messages):
                role = m.get("role", "?")
                if role == "system":
                    continue
                content = m.get("content", "")
                if isinstance(content, list):
                    content = "<multimodal>"
                text = str(content)
                dates = _join(extract_dates(text))
                entities = _join(extract_entities(text))
                keywords = _join(extract_keywords(text, n=5))
                digest = hashlib.sha256(
                    f"{role}\x00{text}".encode("utf-8", errors="replace")
                ).hexdigest()
                cur = await store._db.execute(
                    "INSERT INTO conversation(session_id,turn_idx,role,content,ts,"
                    "dates,entities,keywords,message_idx,content_digest) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(session_id,message_idx) WHERE message_idx IS NOT NULL DO NOTHING",
                    (session_id, turn_idx, role, text, ts, dates, entities, keywords,
                     message_idx, digest))
                inserted += max(cur.rowcount, 0)
            await store._db.execute("RELEASE SAVEPOINT capture_sp")
            await store._db.commit()
            return inserted
        except BaseException:
            try:
                await store._db.execute("ROLLBACK TO SAVEPOINT capture_sp")
                await store._db.execute("RELEASE SAVEPOINT capture_sp")
                await store._db.commit()
            except Exception:
                pass
            raise
