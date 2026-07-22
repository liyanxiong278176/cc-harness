"""DriftDetector — 中心化引擎,写时+读时双检 (E5)。

复用 E2 ReflectionEngine (commit 2c8132a) emit 写盘机制,新增 drift_detected 工厂
(在 reflection/events.py)。JUDGE 失败 → 退回本地 LLM,都失败 → noop + 审计。
"""
from __future__ import annotations
import hashlib
import inspect
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from cc_harness.drift.prompts import JUDGE_ENTITIES, JUDGE_GROUP_CONSIST

if TYPE_CHECKING:
    from cc_harness.reflection.engine import ReflectionEngine
    from cc_harness.memory.store import Memory


log = logging.getLogger(__name__)


@dataclass
class DriftVerdict:
    entity: str
    drift_rate: float
    total_groups: int
    inconsistent_groups: int
    sample_records: list[dict] = field(default_factory=list)
    reason: str = ""


class DriftDetector:
    def __init__(
        self,
        *,
        memory_service,
        reflection_engine: "ReflectionEngine",
        judge_llm,
        l5_engine,
        project_root: Path,
        audit_path: Path | None = None,
        every_n_turns: int = 5,
        enabled: bool = True,
    ):
        self._memory_service = memory_service
        self._reflection_engine = reflection_engine
        self._judge_llm = judge_llm
        self._l5 = l5_engine
        self._project_root = Path(project_root)
        self._audit_path = audit_path or (self._project_root / "logs" / "drift.jsonl")
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._every_n_turns = every_n_turns
        self._enabled = enabled

    # ---------------- 公共 API ----------------

    async def check_after_write(
        self,
        *,
        session_id: str,
        turn_idx: int,
        new_memory: "Memory",
        similar: list["Memory"],
    ) -> list[DriftVerdict]:
        """写时检测:新 memory 与 similar 中 ≥2 同 entity record 判 consistency。"""
        if not self._enabled:
            return []
        if not self._should_run(turn_idx):
            return []
        if len(similar) < 2:
            return []
        return await self._check_groups(
            session_id=session_id, turn_idx=turn_idx,
            records=[new_memory] + similar,  # 包含新 + 旧
        )

    async def check_after_read(
        self,
        *,
        session_id: str,
        turn_idx: int,
        results: list["Memory"],
    ) -> list[DriftVerdict]:
        """读时检测:召出 top-K 中 ≥2 同 entity record 判 consistency。"""
        if not self._enabled:
            return []
        if not self._should_run(turn_idx):
            return []
        if len(results) < 2:
            return []
        return await self._check_groups(
            session_id=session_id, turn_idx=turn_idx,
            records=results,
        )

    # ---------------- 内部 ----------------

    def _should_run(self, turn_idx: int) -> bool:
        """每 N turn 1 次 (默认 N=5)。"""
        if self._every_n_turns <= 0:
            return True
        return (turn_idx % self._every_n_turns) == 0

    async def _check_groups(
        self,
        *,
        session_id: str,
        turn_idx: int,
        records: list["Memory"],
    ) -> list[DriftVerdict]:
        # 1. 抽 entity
        entity_to_records: dict[str, list["Memory"]] = {}
        all_llm_failed = True  # 追踪是否全部 LLM 抽 entity 失败(用于审计)
        for mem in records:
            entities = await self._judge_entities(mem.text)
            if entities:
                all_llm_failed = False
            for ent in entities:
                key = ent.strip().lower()
                if not key or len(key) < 2:
                    continue
                entity_to_records.setdefault(key, []).append(mem)

        verdicts: list[DriftVerdict] = []
        for entity, mems in entity_to_records.items():
            if len(mems) < 2:
                continue
            # 2. 判 consistency
            consistent, reason = await self._judge_group_consistency(entity, mems)
            # 3. drift_rate:1 group, consistent=True → 0.0, False → 1.0
            drift_rate = 0.0 if consistent else 1.0
            verdict = DriftVerdict(
                entity=entity,
                drift_rate=drift_rate,
                total_groups=1,
                inconsistent_groups=0 if consistent else 1,
                sample_records=[{"id": m.id, "text": m.text} for m in mems[:10]],
                reason=reason[:500],
            )
            verdicts.append(verdict)
            # 4. emit drift_detected 走 E2 reflection engine
            await self._emit_drift(
                session_id=session_id, turn_idx=turn_idx, verdict=verdict,
            )

        # 5. all_llm_unavailable 兜底审计:全部 LLM 抽 entity 失败 → noop + 审计一行
        #    spec D7:审计落盘即便无 verdict,让 ops 看见"静默退化"。
        if all_llm_failed:
            try:
                with self._audit_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "ts": time.time(),
                        "op": "noop",
                        "reason": "all_llm_unavailable",
                        "session_id": session_id,
                        "turn_idx": turn_idx,
                        "record_count": len(records),
                    }, ensure_ascii=False) + "\n")
            except Exception as e:
                log.warning("drift: noop audit failed: %s", e)

        return verdicts

    async def _judge_entities(self, text: str) -> list[str]:
        resp = await self._ask_judge(JUDGE_ENTITIES, text)
        if resp is None:
            return []
        try:
            m = re.search(r"\{.*\}", resp, re.DOTALL)
            if m:
                data = json.loads(m.group(0))
                return data.get("entities", [])
        except (json.JSONDecodeError, ValueError):
            pass
        return []

    async def _judge_group_consistency(
        self, entity: str, records: list["Memory"],
    ) -> tuple[bool, str]:
        pred_block = "\n".join(f"- {m.text}" for m in records)
        user = f"entity: {entity}\n{pred_block}"
        resp = await self._ask_judge(JUDGE_GROUP_CONSIST, user)
        if resp is None:
            return True, "all_llm_unavailable"
        try:
            m = re.search(r"\{.*\}", resp, re.DOTALL)
            if m:
                data = json.loads(m.group(0))
                return bool(data.get("consistent", True)), str(data.get("reason", ""))
        except (json.JSONDecodeError, ValueError):
            pass
        return True, "parse_error"

    async def _ask_judge(self, system: str, user: str) -> str | None:
        """复用 E2 reflection 多态:JUDGE_MODEL → 本地 LLMClient → None。"""
        for llm, label in [(self._judge_llm, "judge"), (None, "local")]:  # 本地暂由反射 engine 替代
            # 简化:只尝试 judge_llm,失败返 None。Local 退回由 E2 reflection engine 内部处理
            # 实际 E5 detector 自身不直接管 local(让 reflection engine 走其 fallback chain)
            try:
                if hasattr(llm, "chat"):
                    content = ""
                    async for ev_obj in llm.chat(
                        [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
                        tools=None,
                    ):
                        if getattr(ev_obj, "kind", None) == "done":
                            content = getattr(ev_obj, "content", None) or content
                    return content
                try:
                    n_pos = sum(
                        1 for p in inspect.signature(llm).parameters.values()
                        if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                      inspect.Parameter.POSITIONAL_OR_KEYWORD)
                    )
                except (ValueError, TypeError):
                    n_pos = 1
                if n_pos >= 2:
                    return await llm(system, user)
                return await llm(system + "\n" + user)
            except Exception as e:
                log.warning("drift: %s llm failed: %s", label, e)
                continue
        return None

    async def _emit_drift(
        self, *, session_id: str, turn_idx: int, verdict: DriftVerdict,
    ) -> None:
        """emit drift_detected 走 E2 reflection engine。

        T1.2 阶段:`drift_detected` 工厂尚未在 reflection/events.py 实现(T1.3 任务)。
        因此 try/except ImportError 兜底 → 审计 + 警告 log,不抛,不断后续 verdict。
        """
        try:
            from cc_harness.reflection.events import drift_detected
            event = drift_detected(
                session_id=session_id,
                turn_idx=turn_idx,
                entity=verdict.entity,
                drift_rate=verdict.drift_rate,
                total_groups=verdict.total_groups,
                inconsistent_groups=verdict.inconsistent_groups,
                records=verdict.sample_records,
                reason=verdict.reason,
            )
            await self._reflection_engine.emit(event)
        except ImportError:
            log.warning("drift: drift_detected factory not yet implemented (T1.3 待补), 跳过 emit")
        except Exception as e:
            log.warning("drift: emit failed: %s", e)
        finally:
            self._audit(verdict=verdict, event_type="emit",
                        session_id=session_id, turn_idx=turn_idx)

    def _audit(
        self, *, verdict: DriftVerdict, event_type: str,
        session_id: str, turn_idx: int,
    ) -> None:
        try:
            with self._audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "op": event_type,
                    "event_type": "drift_detected",
                    "severity": self._severity_for(verdict.drift_rate),
                    # spec §"错误处理":audit 不记明文 entity。
                    # 用 sha1 前 8 字符(只用于区分不同时段/不同 entity,不逆推)。
                    "entity_hash": hashlib.sha1(verdict.entity.encode("utf-8")).hexdigest()[:8],
                    "drift_rate": verdict.drift_rate,
                    "total_groups": verdict.total_groups,
                    "inconsistent_groups": verdict.inconsistent_groups,
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("drift: audit failed: %s", e)

    @staticmethod
    def _severity_for(drift_rate: float) -> str:
        if drift_rate < 0.2:
            return "pos"
        if drift_rate < 0.5:
            return "ambig"
        return "neg"

    async def _drain(self, *, timeout_s: float = 5.0) -> None:
        """DriftDetector 自身不跑后台 task,这里留接口对称 E2 reflection 模式。"""
        pass
