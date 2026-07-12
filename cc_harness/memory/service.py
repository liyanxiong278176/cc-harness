"""Orchestration layer: single entry point for save() that ties together
EmbeddingClient, MemoryStore, and LLMDecider. The 4-step flow is:
    embed → search_similar → decide → apply
"""
from __future__ import annotations
import sqlite3
import time
from dataclasses import dataclass

from cc_harness.memory.embedding import EmbeddingError
from cc_harness.memory.decider import Decision, DecisionResult


@dataclass
class SaveResult:
    action: str   # 'ADD' | 'UPDATE' | 'DELETE_THEN_ADD' | 'NOOP' | 'ERROR'
    memory: object | None = None      # Memory | None
    previous: object | None = None    # old Memory before UPDATE/DELETE
    deleted_id: str | None = None     # for DELETE_THEN_ADD
    duration_ms: int = 0
    error: str | None = None


class MemoryService:
    def __init__(self, store, embedder, decider):
        self.store = store
        self.embedder = embedder
        self.decider = decider

    async def recall(self, query: str, top_k: int = 5) -> list:
        embedding = await self.embedder.embed(query)
        return await self.store.search_similar(embedding, k=top_k)

    async def save(self, text: str, source: str, session_id: str | None = None) -> SaveResult:
        t0 = time.time()
        try:
            embedding = await self.embedder.embed(text)
            similar = await self.store.search_similar(embedding, k=5)
            if not similar:
                decision = DecisionResult(action=Decision.ADD)
            else:
                decision = await self.decider.decide(text, similar)

            if decision.action == Decision.ADD:
                mem = await self.store.add(text, embedding, source, session_id=session_id)
                return SaveResult(action="ADD", memory=mem, duration_ms=_ms(t0))

            if decision.action == Decision.UPDATE:
                # UPDATE 走 store.update(改 text+embedding),不改 session_id(保持原归属)
                old = await self.store.get(decision.target_id)
                new_embedding = await self.embedder.embed(decision.merged_text)
                mem = await self.store.update(decision.target_id, decision.merged_text, new_embedding)
                return SaveResult(action="UPDATE", memory=mem, previous=old, duration_ms=_ms(t0))

            if decision.action == Decision.DELETE:
                old = await self.store.get(decision.target_id)
                await self.store.delete(decision.target_id)
                mem = await self.store.add(text, embedding, source, session_id=session_id)
                return SaveResult(action="DELETE_THEN_ADD", memory=mem, previous=old,
                                  deleted_id=decision.target_id, duration_ms=_ms(t0))

            return SaveResult(action="NOOP", duration_ms=_ms(t0))

        except EmbeddingError as e:
            return SaveResult(action="ERROR", error=f"embedding: {e}", duration_ms=_ms(t0))
        except sqlite3.Error as e:
            return SaveResult(action="ERROR", error=f"db: {e}", duration_ms=_ms(t0))
        except Exception as e:
            return SaveResult(action="ERROR", error=f"{type(e).__name__}: {e}", duration_ms=_ms(t0))

    async def delete_by_tag(self, tag_pattern: str) -> int:
        """Delete all memories whose ``source`` matches the LIKE pattern.

        The f3141b6 schema has no dedicated ``tags`` column; ``source``
        is the only string field suitable for pattern-based isolation.
        Callers (e.g. locomo runner) should save with a ``source`` value
        that doubles as a tag prefix (e.g. ``"locomo/<sample_id>"``)
        and pass ``"locomo/%"`` here.

        Also removes the corresponding rows from the ``vec_memories``
        virtual table so search_similar stops returning them.
        Returns the number of deleted rows from ``memories``.
        """
        assert self.store._db is not None, "store.init_schema first"
        cur = await self.store._db.execute(
            "SELECT id FROM memories WHERE source LIKE ?", (tag_pattern,)
        )
        rows = await cur.fetchall()
        if not rows:
            return 0
        ids = [r[0] for r in rows]
        placeholders = ",".join("?" * len(ids))
        del_cur = await self.store._db.execute(
            f"DELETE FROM memories WHERE id IN ({placeholders})", ids
        )
        await self.store._db.execute(
            f"DELETE FROM vec_memories WHERE id IN ({placeholders})", ids
        )
        await self.store._db.commit()
        return del_cur.rowcount


def _ms(t0: float) -> int:
    return int((time.time() - t0) * 1000)
