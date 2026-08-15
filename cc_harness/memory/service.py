"""Orchestration layer: single entry point for save() that ties together
EmbeddingClient, MemoryStore, and LLMDecider. The 4-step flow is:
    embed → search_similar → decide → apply
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass

from cc_harness.memory.decider import Decision, DecisionResult
from cc_harness.memory.embedding import EmbeddingError
from cc_harness.memory.temporal import extract_temporal_metadata

logger = logging.getLogger(__name__)


@dataclass
class SaveResult:
    action: str   # 'ADD' | 'UPDATE' | 'DELETE_THEN_ADD' | 'NOOP' | 'ERROR'
    memory: object | None = None      # Memory | None
    previous: object | None = None    # old Memory before UPDATE/DELETE
    deleted_id: str | None = None     # for DELETE_THEN_ADD
    duration_ms: int = 0
    error: str | None = None


class MemoryService:
    def __init__(self, store, embedder, decider, drift_detector=None):  # E5
        self.store = store
        self.embedder = embedder
        self.decider = decider
        self.drift_detector = drift_detector

    async def recall(self, query: str, top_k: int = 5) -> list:
        embedding = await self.embedder.embed(query)
        return await self.store.search_similar(embedding, k=top_k)

    async def save_preserved_facts(
        self,
        text: str,
        *,
        session_id: str,
        provenance: dict | None = None,
        source: str = "locomo-history",
    ) -> list[SaveResult]:
        """Persist benchmark history without product conflict resolution.

        LoCoMo contains time-local facts that are semantically similar across
        sessions.  The normal ``save`` path intentionally resolves such
        similarities for interactive memory, but that policy would erase the
        very history this benchmark asks us to retrieve.  This path only
        removes exact duplicates within the same session and scope.
        """

        facts = split_history_facts(text)
        results: list[SaveResult] = []
        base_provenance = dict(provenance or {})
        for index, fact in enumerate(facts, 1):
            started = time.time()
            try:
                duplicate = await self.store.find_active_exact(
                    fact, session_id=session_id
                )
                if duplicate is not None:
                    results.append(
                        SaveResult(
                            action="NOOP",
                            memory=duplicate,
                            duration_ms=_ms(started),
                        )
                    )
                    continue
                embedding = await self.embedder.embed(fact)
                fact_provenance = {
                    **base_provenance,
                    "session_id": session_id,
                    "fact_index": index,
                    "fact_count": len(facts),
                    "retention": "session-scoped-preserve",
                    "temporal": extract_temporal_metadata(
                        fact,
                        session_timestamp=str(base_provenance.get("session_timestamp") or ""),
                    ),
                }
                memory = await self.store.add(
                    fact,
                    embedding,
                    source,
                    session_id=session_id,
                    provenance_json=json.dumps(
                        fact_provenance, ensure_ascii=False, sort_keys=True
                    ),
                )
                results.append(
                    SaveResult(
                        action="HISTORY_ADD",
                        memory=memory,
                        duration_ms=_ms(started),
                    )
                )
            except EmbeddingError as exc:
                results.append(
                    SaveResult(
                        action="ERROR",
                        error=f"embedding: {exc}",
                        duration_ms=_ms(started),
                    )
                )
            except sqlite3.Error as exc:
                results.append(
                    SaveResult(
                        action="ERROR",
                        error=f"db: {exc}",
                        duration_ms=_ms(started),
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive tool boundary
                results.append(
                    SaveResult(
                        action="ERROR",
                        error=f"{type(exc).__name__}: {exc}",
                        duration_ms=_ms(started),
                    )
                )
        return results

    async def save(self, text: str, source: str, session_id: str | None = None, *, turn_idx: int | None = None) -> SaveResult:
        # M2: turn_idx 优先用 caller 传,None 时退占位(向后兼容)
        actual_turn_idx = turn_idx if turn_idx is not None else int(time.time() * 1000) % 1000
        t0 = time.time()
        result_action_mem = None
        try:
            embedding = await self.embedder.embed(text)
            similar = await self.store.search_similar(embedding, k=5)
            # 冲突检测用的 embedding:ADD/DELETE 用原文 embedding;UPDATE 用 merged 后的
            # new_embedding(与写盘行一致),否则会用错邻域漏检真实矛盾。
            conflict_embedding = embedding
            if not similar:
                decision = DecisionResult(action=Decision.ADD)
            else:
                # E2 T3.1: 召过去 24h 反思注入 decider,帮 LLM 参考历史反思做去重/更新
                recent_reflections = await self.store.search_reflections(
                    limit=5, lookback_h=24
                )
                decision = await self.decider.decide(
                    text, similar, recent_reflections=recent_reflections
                )

            if decision.action == Decision.ADD:
                mem = await self.store.add(text, embedding, source, session_id=session_id)
                result_action_mem = mem
                result = SaveResult(action="ADD", memory=mem, duration_ms=_ms(t0))

            elif decision.action == Decision.UPDATE:
                # UPDATE 走 store.update(改 text+embedding),不改 session_id(保持原归属)
                old = await self.store.get(decision.target_id)
                new_embedding = await self.embedder.embed(decision.merged_text)
                mem = await self.store.supersede(
                    decision.target_id,
                    decision.merged_text,
                    new_embedding,
                    source=source,
                    session_id=session_id,
                )
                conflict_embedding = new_embedding
                result_action_mem = mem
                result = SaveResult(action="UPDATE", memory=mem, previous=old, duration_ms=_ms(t0))

            elif decision.action == Decision.DELETE:
                old = await self.store.get(decision.target_id)
                await self.store.delete(decision.target_id)
                mem = await self.store.add(text, embedding, source, session_id=session_id)
                result_action_mem = mem
                result = SaveResult(action="DELETE_THEN_ADD", memory=mem, previous=old,
                                    deleted_id=decision.target_id, duration_ms=_ms(t0))

            else:
                result = SaveResult(action="NOOP", duration_ms=_ms(t0))

            # E4 write-time 矛盾检测(写盘后, 仅 ADD/UPDATE/DELETE_THEN_ADD 触发)
            similar_for_conflict: list = []
            if self.decider is not None and result_action_mem is not None:
                try:
                    from cc_harness.memory.maintenance.conflict import ConflictDetector
                    det = ConflictDetector(self.decider._llm)
                    similar_for_conflict = await self.store.search_similar(conflict_embedding, k=5)
                    verdicts = await det.check(result_action_mem, similar_for_conflict)
                    # 取 mem_id 时兼容 dataclass / dict / tuple 三种返回形态
                    # (v 是 ConflictVerdict dataclass,但 result_action_mem 路径上
                    # 历史上偶发被替换为 tuple,这里做 defensive 兜底)
                    def _mem_id(m):
                        if m is None:
                            return None
                        if hasattr(m, "id"):
                            return m.id
                        if isinstance(m, dict):
                            return m.get("id")
                        if isinstance(m, tuple):
                            for x in m:
                                if hasattr(x, "id"):
                                    return x.id
                                if isinstance(x, dict) and "id" in x:
                                    return x["id"]
                        return None
                    new_id = _mem_id(result_action_mem)
                    for v in verdicts:
                        try:
                            action = getattr(v, "action", None) or (v.get("action") if isinstance(v, dict) else None)
                            other_id = getattr(v, "other_id", None) or (v.get("other_id") if isinstance(v, dict) else None)
                            if action == "delete_old" and other_id:
                                await self.store.delete(other_id)
                            elif action == "delete_new" and new_id:
                                await self.store.delete(new_id)
                                return SaveResult(action="ROLLBACK",
                                                  error=f"conflict:{getattr(v, 'verdict', None) or (v.get('verdict') if isinstance(v, dict) else '?')}",
                                                  duration_ms=_ms(t0))
                        except Exception as e:
                            logger.warning("conflict action failed (non-fatal): %s", e)
                except Exception as e:
                    logger.warning("memory conflict check failed: %s", e)

            # E5 drift 检测(写盘后, 复用 E2 reflection engine 写 source='drift')
            # similar_for_conflict 来自上面 search_similar,直接复用不重查
            if self.drift_detector is not None and result_action_mem is not None:
                try:
                    # F1: search_similar 返 list[tuple[Memory, float]],detector 要 list[Memory]
                    # 原 round 1 把 tuples 原样传 → detector mem.text 失败被静默吞
                    similar_mems = [m for m, _ in similar_for_conflict]
                    await self.drift_detector.check_after_write(
                        session_id=session_id or "default",
                        turn_idx=actual_turn_idx,
                        new_memory=result_action_mem,
                        similar=similar_mems,
                    )
                except Exception as e:
                    logger.warning("memory drift check failed: %s", e)

            return result

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
        try:
            del_cur = await self.store._db.execute(
                f"DELETE FROM memories WHERE id IN ({placeholders})", ids
            )
            await self.store._db.execute(
                f"DELETE FROM vec_memories WHERE id IN ({placeholders})", ids
            )
            await self.store._db.commit()
        except Exception:
            # 两条 DELETE 同一事务;任一失败回滚,避免 memories/vec 不一致
            await self.store._db.rollback()
            raise
        return del_cur.rowcount


_HISTORY_FACT_SPLIT_RE = re.compile(
    r"(?<=[.!?。！？])\s+|;\s+|\n+|(?<=\])\s+(?=\[FACT\])"
)


def split_history_facts(text: str, *, max_facts: int = 8) -> list[str]:
    """Split a model-produced history note into retrievable fact claims.

    The model is encouraged to emit one claim per ``memory_save`` call, but
    this deterministic fallback protects the benchmark when it compresses a
    session into one tool argument.  It never merges facts from another
    session; at most the tail of this one note is grouped into the final fact.
    """

    normalized = re.sub(r"\s+", " ", str(text or "").strip())
    if not normalized:
        return []
    pieces = []
    for raw in _HISTORY_FACT_SPLIT_RE.split(normalized):
        fact = re.sub(r"^\s*(?:[-*•]|\[FACT\])\s*", "", raw).strip()
        fact = re.sub(r"\s+", " ", fact)
        if fact:
            pieces.append(fact)
    if not pieces:
        return [normalized]
    if len(pieces) <= max_facts:
        return pieces
    head = pieces[: max_facts - 1]
    head.append(" ".join(pieces[max_facts - 1 :]))
    return head


def _ms(t0: float) -> int:
    return int((time.time() - t0) * 1000)
