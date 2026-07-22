"""DriftDetector — 中心化引擎,写时+读时双检 (E5, round 2 算法修正)。

E5 round 1 实施被 final review 抓到 6 bug,本版本修正:
- F4: 移除 _memory_service 形参(spec 决策与实施均不需要)
- F5: consistency judge 失败返 (None, reason),不发假阳性 drift event
- F6: 实现 m5 风格多组 ratio 算法,drift_rate = inconsistent_groups / total_groups
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
        reflection_engine: "ReflectionEngine",
        judge_llm,
        l5_engine,
        project_root: Path,
        audit_path: Path | None = None,
        every_n_turns: int = 5,
        enabled: bool = True,
        local_llm=None,  # F3: 真 local LLM fallback (主 LLM, 同 E2 _ask_judge_with_fallback)
    ):
        # F4: 不再接受 memory_service(从未用过)
        self._reflection_engine = reflection_engine
        self._judge_llm = judge_llm
        self._l5 = l5_engine
        self._project_root = Path(project_root)
        self._audit_path = audit_path or (self._project_root / "logs" / "drift.jsonl")
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._every_n_turns = every_n_turns
        self._enabled = enabled
        self._local_llm = local_llm

    # ---------------- 公共 API ----------------

    async def check_after_write(self, *, session_id, turn_idx, new_memory, similar):
        if not self._enabled or not self._should_run(turn_idx) or len(similar) < 2:
            return []
        return await self._check_groups(
            session_id=session_id, turn_idx=turn_idx,
            records=[new_memory] + similar,
        )

    async def check_after_read(self, *, session_id, turn_idx, results):
        if not self._enabled or not self._should_run(turn_idx) or len(results) < 2:
            return []
        return await self._check_groups(
            session_id=session_id, turn_idx=turn_idx, records=results,
        )

    # ---------------- 内部 ----------------

    def _should_run(self, turn_idx):
        if self._every_n_turns <= 0:
            return True
        return (turn_idx % self._every_n_turns) == 0

    async def _check_groups(self, *, session_id, turn_idx, records):
        """F6 多组 ratio 算法:
        1. 对每个 record 抽 entity
        2. 对每个 entity:
           - 按 text.strip().lower() 分组(simplified m5 group_key)
           - 每组跑 consistency judge
           - drift_rate = inconsistent_groups / total_groups
        3. F5:consistency 失败(None)不计入 inconsistent,fall 到一致性 judge 全挂审计
        """
        entity_to_records: dict[str, list] = {}
        for mem in records:
            entities = await self._judge_entities(mem.text)
            for ent in entities:
                key = ent.strip().lower()
                if not key or len(key) < 2:
                    continue
                entity_to_records.setdefault(key, []).append(mem)

        verdicts: list[DriftVerdict] = []
        consistency_judge_failed = False
        all_entities_failed = True  # F5 追踪:整轮是否一个 consistent verdict 都没产出

        for entity, mems in entity_to_records.items():
            if len(mems) < 2:
                continue

            # F6: 按 text.strip().lower() 分组
            groups: dict[str, list] = {}
            for mem in mems:
                gkey = mem.text.strip().lower()
                groups.setdefault(gkey, []).append(mem)

            total_groups = len(groups)
            inconsistent_groups = 0
            group_reasons: list[str] = []

            for gkey, grecs in groups.items():
                # F6: 每组都跑 consistency judge(含单 record 组,以支持多组 ratio)
                consistent, reason = await self._judge_group_consistency(entity, grecs)
                if consistent is None:
                    # F5: judge fail → 不计为 inconsistent,标记
                    consistency_judge_failed = True
                    group_reasons.append(f"[group_fail:{reason}]")
                    continue
                if not consistent:
                    inconsistent_groups += 1
                    group_reasons.append(f"[inconsistent:{reason}]")
                else:
                    group_reasons.append(f"[consistent:{reason}]")

            if consistency_judge_failed and inconsistent_groups == 0:
                # 整 entity 全 consistency judge fail → 不发 verdict
                continue

            if total_groups == 0:
                continue

            drift_rate = inconsistent_groups / total_groups
            all_entities_failed = False

            verdict = DriftVerdict(
                entity=entity,
                drift_rate=drift_rate,
                total_groups=total_groups,
                inconsistent_groups=inconsistent_groups,
                # M3: 文本过 L5 sanitize(spec §错误处理:drift 证据文本被 [REDACTED:...] 替换)
                sample_records=[
                    {"id": m.id, "text": self._l5.sanitize(m.text)}
                    for m in mems[:10]
                ],
                reason="; ".join(group_reasons)[:500],
            )
            verdicts.append(verdict)
            await self._emit_drift(
                session_id=session_id, turn_idx=turn_idx, verdict=verdict,
            )

        # F5 审计
        if consistency_judge_failed and all_entities_failed:
            self._audit_noop(
                session_id=session_id, turn_idx=turn_idx,
                reason="consistency_judge_failed",
                record_count=len(records),
            )

        return verdicts

    async def _judge_entities(self, text):
        resp = await self._ask_judge(JUDGE_ENTITIES, text)
        if resp is None:
            return []
        try:
            m = re.search(r"\{.*\}", resp, re.DOTALL)
            if m:
                return json.loads(m.group(0)).get("entities", [])
        except (json.JSONDecodeError, ValueError):
            pass
        return []

    async def _judge_group_consistency(self, entity, records):
        pred_block = "\n".join(f"- {m.text}" for m in records)
        user = f"entity: {entity}\n{pred_block}"
        resp = await self._ask_judge(JUDGE_GROUP_CONSIST, user)
        if resp is None:
            return None, "all_llm_unavailable"  # F5: None not True
        try:
            m = re.search(r"\{.*\}", resp, re.DOTALL)
            if m:
                data = json.loads(m.group(0))
                return bool(data.get("consistent", True)), str(data.get("reason", ""))
        except (json.JSONDecodeError, ValueError):
            pass
        return None, "parse_error"  # F5: None not True

    async def _ask_judge(self, system, user):
        """F3: JUDGE → local LLM → None (audit noop)。"""

        async def _try_llm(llm, label):
            if llm is None:
                return None
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
                return None

        primary = await _try_llm(self._judge_llm, "judge")
        if primary is not None:
            return primary
        return await _try_llm(self._local_llm, "local")  # F3: 真 local fallback

    async def _emit_drift(self, *, session_id, turn_idx, verdict):
        try:
            from cc_harness.reflection.events import drift_detected
            event = drift_detected(
                session_id=session_id, turn_idx=turn_idx,
                entity=verdict.entity, drift_rate=verdict.drift_rate,
                total_groups=verdict.total_groups,
                inconsistent_groups=verdict.inconsistent_groups,
                records=verdict.sample_records, reason=verdict.reason,
            )
            await self._reflection_engine.emit(event)
        except ImportError:
            log.warning("drift: drift_detected factory missing")
        except Exception as e:
            log.warning("drift: emit failed: %s", e)
        finally:
            self._audit(verdict=verdict, event_type="emit",
                        session_id=session_id, turn_idx=turn_idx)

    def _audit(self, *, verdict, event_type, session_id, turn_idx):
        try:
            with self._audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(), "op": event_type,
                    "event_type": "drift_detected",
                    "severity": self._severity_for(verdict.drift_rate),
                    "entity_hash": hashlib.sha1(verdict.entity.encode("utf-8")).hexdigest()[:8],
                    "drift_rate": verdict.drift_rate,
                    "total_groups": verdict.total_groups,
                    "inconsistent_groups": verdict.inconsistent_groups,
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("drift: audit failed: %s", e)

    def _audit_noop(self, *, session_id, turn_idx, reason, record_count):
        try:
            with self._audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(), "op": "noop", "reason": reason,
                    "session_id": session_id, "turn_idx": turn_idx,
                    "record_count": record_count,
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("drift: noop audit failed: %s", e)

    @staticmethod
    def _severity_for(drift_rate):
        if drift_rate < 0.2:
            return "pos"
        if drift_rate < 0.5:
            return "ambig"
        return "neg"

    async def _drain(self, *, timeout_s=5.0):
        pass
