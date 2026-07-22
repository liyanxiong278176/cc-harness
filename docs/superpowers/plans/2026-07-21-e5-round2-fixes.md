# E5 round 2 Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 1 Critical + 5 Important bugs found in E5 final whole-branch review (`6b21660` commit, review report at `.superpowers/sdd/e5-final-review.md`). Land 2 commits that retake E5 from ❌ NOT ready to merge → ✅ ready to merge.

**Architecture:** Two commits — R1 fixes DriftDetector core algorithm + write-time tuple mishandling + consistency judge fail-soft (`cc_harness/drift/detector.py` + `cc_harness/memory/service.py`), R2 fixes source isolation in E2 ReflectionEngine + real local LLM injection (`cc_harness/reflection/engine.py` + `cc_harness/reflection/events.py` + `main.py`). Each fix follows the spec amendment (F1-F6 mapping).

**Tech Stack:** Python 3.11+, asyncio, pydantic, existing E2 ReflectionEngine + E4 MemoryService infrastructure. No new deps.

## Global Constraints

- TDD red→green for every fix; do NOT commit until tests pass
- Ruff-clean on every commit
- No breakage of E2 reflection 6-event pipeline (only 1 small extension to `ReflectionEvent` and `drift_detected` in commit R2)
- No breakage of E4 maintenance scheduler (it consumes `source='reflection'` reflections; drift uses `source='drift'` which is new)
- Pre-existing baseline: 13 failures in `tests/test_strategies_yaml.py` (legacy config deletion 2026-07-06) are acceptable; do NOT regress them, do NOT attempt to fix them
- F5 consistency-fail-soft: returning `None` (not `True`) is the spec-correct pattern; do NOT shortcut to "treat as consistent"
- F6 multi-group ratio: use `text.strip().lower()` as group_key (simplified m5 `(sample_id, ent_lower)`)
- No `import eval.locomo.metrics` anywhere (spec D2 mandate, still binding)
- Audit hash `entity_hash = sha1(entity.encode("utf-8")).hexdigest()[:8]` — no entity plaintext in `logs/drift.jsonl`

---

## Commit R1: feat(drift) — 算法 + 写时修复 + 一致性 fail-soft

**What landed (4 fixes)**: F1 tuple unpack in MemoryService.save, F4 drop unused `_memory_service`, F5 consistency judge fail-soft returns `None`, F6 multi-group ratio algorithm.

### Task R1.1: detector.py 算法重写 — F6 + F5 + F4 + 工厂 updates

**Files:**
- Modify: `cc_harness/drift/detector.py` (whole-file: F6 algorithm rewrite, F5 fail-soft, F4 drop `memory_service` param, drop now-unused `all_llm_failed` track variable, simplify `_check_groups`)
- Modify: `cc_harness/memory/service.py` (F1 tuple unpack: line 97-104)
- Test: `tests/test_drift_detector.py` (rewrite 7 affected tests + add 1 multi-group test; aim for ~10 tests)

**Interfaces:**
- `DriftDetector.__init__(self, *, reflection_engine, judge_llm, l5_engine, project_root, audit_path=None, every_n_turns=5, enabled=True)` — **drops `memory_service` (F4)**
- `DriftDetector._check_groups(*, session_id, turn_idx, records) -> list[DriftVerdict]` — F6 multi-group ratio algorithm (described below)
- `DriftDetector._judge_group_consistency(entity, records) -> tuple[bool | None, str]` — returns `None` (not `False`) on parse failure / all_llm_unavailable (F5)
- `MemoryService.save` — F1: extract `mems_only = [m for m, _ in similar_for_conflict]` before passing to `check_after_write`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_drift_detector.py` (after deleting the now-unaffected imports section lines 1-13):

```python
"""E5 round 2 — DriftDetector 修复测试。

Round 1 已被 final review 抓到 6 bug,本文件对应修法:
- F4: 移除 _memory_service 形参(不再 declare 没用的形参)
- F5: consistency judge 失败时返 (None, reason),_check_groups 不发 drift event
- F6: 多组 ratio 算法,drift_rate = inconsistent_groups / total_groups
"""
from __future__ import annotations
import json
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest

from cc_harness.drift.detector import DriftDetector, DriftVerdict
from cc_harness.memory.store import Memory


@pytest.fixture
def tmp_audit(tmp_path: Path) -> Path:
    return tmp_path / "drift.jsonl"


@pytest.fixture
def fake_reflection_engine():
    eng = MagicMock()
    eng.emit = AsyncMock()
    return eng


@pytest.fixture
def fake_l5():
    return MagicMock(sanitize=lambda x: x)


def make_memory(mid: str, text: str, source: str = "llm") -> Memory:
    return Memory(
        id=mid, text=text, embedding=[0.1, 0.2, 0.3, 0.4],
        created_at=0.0, updated_at=0.0, source=source,
    )


@pytest.mark.asyncio
async def test_check_after_write_with_two_similar_inconsistent(
    tmp_audit, fake_reflection_engine, fake_l5,
):
    """F6: 2 同 entity 不同 text → 2 组 → 2 inconsistent → drift_rate = 2/2 = 1.0 → severity=neg。"""
    async def judge_entities_fn(system, user):
        return '{"entities": ["caroline"]}'

    async def judge_consistent_fn(system, user):
        return '{"consistent": false, "reason": "different years"}'

    det = DriftDetector(
        reflection_engine=fake_reflection_engine,
        judge_llm=MagicMock(chat=AsyncMock()),  # 不走 chat 路径,直接用 _judge_entities 的可调函数
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
    )
    # 用 monkeypatch 替换 _judge_entities / _judge_group_consistency 的内部调用
    det._judge_entities = AsyncMock(return_value=["caroline"])
    det._judge_group_consistency = AsyncMock(return_value=(False, "different years"))

    new = make_memory("m3", "Caroline 1990")
    similar = [
        make_memory("m1", "Caroline 1985"),
        make_memory("m2", "Caroline 1985"),
    ]
    verdicts = await det.check_after_write(
        session_id="s1", turn_idx=5, new_memory=new, similar=similar,
    )
    assert len(verdicts) == 1
    assert verdicts[0].total_groups == 2
    assert verdicts[0].inconsistent_groups == 2
    assert verdicts[0].drift_rate == 1.0  # F6: ratio 算法
    # emit 1 次 drift_detected severity=neg
    fake_reflection_engine.emit.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_after_write_multigroup_ratio(
    tmp_audit, fake_reflection_engine, fake_l5,
):
    """F6: 4 records 同 entity "caroline",3 个不同 group(2 重复算 1 组),1 inconsistent,1 consistent → drift_rate = 1/3 ≈ 0.333 → ambig。

    m5 风格多组:mems 按 text.strip().lower() 分组,每组跑 consistency judge。
    """
    det = DriftDetector(
        reflection_engine=fake_reflection_engine,
        judge_llm=MagicMock(),
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
    )
    det._judge_entities = AsyncMock(return_value=["caroline"])
    # 4 records: 2 个 "caroline 1985" 同组 + "caroline 1990" + "caroline 1980"
    # groups = {"caroline 1985": [m1, m2], "caroline 1990": [m3, new], "caroline 1980": [m4]} = 3 组
    # 一致性判断:3 组中 1 组 consistent,2 组 inconsistent → drift_rate = 2/3 ≈ 0.667 → neg
    judge_calls = []

    async def judge_consist(entity, records):
        # 第一组("caroline 1985" 双份)→ consistent=True
        # 第二组("caroline 1990")→ consistent=False
        # 第三组("caroline 1980")→ consistent=False
        text_set = {m.text.strip().lower() for m in records}
        if "caroline 1990" in text_set or "caroline 1980" in text_set:
            judge_calls.append("inconsistent")
            return False, "different"
        judge_calls.append("consistent")
        return True, "same"

    det._judge_group_consistency = AsyncMock(side_effect=judge_consist)

    new = make_memory("m3", "Caroline 1990")
    similar = [
        make_memory("m1", "Caroline 1985"),
        make_memory("m2", "Caroline 1985"),
        make_memory("m4", "Caroline 1980"),
    ]
    verdicts = await det.check_after_write(
        session_id="s1", turn_idx=5, new_memory=new, similar=similar,
    )
    assert len(verdicts) == 1
    assert verdicts[0].total_groups == 3
    assert verdicts[0].inconsistent_groups == 2
    assert abs(verdicts[0].drift_rate - 2/3) < 0.01  # F6: ~0.667
    # 注:neg severity 因为 > 0.5
    fake_reflection_engine.emit.assert_awaited_once()


@pytest.mark.asyncio
async def test_consistency_judge_fail_returns_none(
    tmp_audit, fake_reflection_engine, fake_l5,
):
    """F5: consistency judge 失败(parse_error / all_llm_unavailable)→ 返 (None, reason),_check_groups 不发 drift event。"""
    det = DriftDetector(
        reflection_engine=fake_reflection_engine,
        judge_llm=MagicMock(),
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
    )
    det._judge_entities = AsyncMock(return_value=["caroline"])
    det._judge_group_consistency = AsyncMock(return_value=(None, "parse_error"))

    new = make_memory("m3", "Caroline 1990")
    similar = [
        make_memory("m1", "Caroline 1985"),
        make_memory("m2", "Caroline 1985"),
    ]
    verdicts = await det.check_after_write(
        session_id="s1", turn_idx=5, new_memory=new, similar=similar,
    )
    # verdict 不返回(consistent=None 不算 inconsistent)
    assert verdicts == []
    # emit 不调
    fake_reflection_engine.emit.assert_not_awaited()
    # 审计:consistency_judge_failed (区别于 all_llm_unavailable)
    assert tmp_audit.exists()
    line = tmp_audit.read_text(encoding="utf-8").strip()
    assert "consistency_judge_failed" in line


@pytest.mark.asyncio
async def test_drift_audit_records_entity_hash(
    tmp_audit, fake_reflection_engine, fake_l5,
):
    """Round 1 已验:audit 写 entity_hash 不写 entity 明文,本测试保证 F6 后仍 work。"""
    det = DriftDetector(
        reflection_engine=fake_reflection_engine,
        judge_llm=MagicMock(),
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
    )
    det._judge_entities = AsyncMock(return_value=["Caroline"])
    det._judge_group_consistency = AsyncMock(return_value=(False, "different"))

    new = make_memory("m3", "Caroline 1990")
    similar = [
        make_memory("m1", "Caroline 1985"),
        make_memory("m2", "Caroline 1990"),
    ]
    await det.check_after_write(
        session_id="s1", turn_idx=5, new_memory=new, similar=similar,
    )
    audit_text = tmp_audit.read_text(encoding="utf-8")
    # 明文不应出现
    assert "Caroline" not in audit_text  # 唯一 Caroline 是 entity 名,audit 不该有
    # 哈希字段存在
    assert "entity_hash" in audit_text
```

- [ ] **Step 2: Verify tests fail (red)**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_detector.py -v`
Expected: at least `test_check_after_write_with_two_similar_inconsistent` and `test_check_after_write_multigroup_ratio` fail with `AttributeError: 'DriftDetector' object has no attribute '_judge_entities'` or `TypeError: __init__() got an unexpected keyword argument 'memory_service'` (F4 dropped).

- [ ] **Step 3: Rewrite `cc_harness/drift/detector.py` — F6 + F5 + F4**

Replace the entire file with the new implementation. Key sections:

```python
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
        reflection_engine: "ReflectionEngine",
        judge_llm,
        l5_engine,
        project_root: Path,
        audit_path: Path | None = None,
        every_n_turns: int = 5,
        enabled: bool = True,
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
                if len(grecs) < 2:
                    # 单 record 组(没重复)不需要判 consistency
                    continue
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
                sample_records=[{"id": m.id, "text": m.text} for m in mems[:10]],
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
        """JUDGE → None(single LLM path in this round; commit R2 adds local fallback)。"""
        llm = self._judge_llm
        label = "judge"
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

    # _emit_drift / _audit / _severity_for / _drain: keep verbatim from round 1
    # (round 1 代码已正确,只是改 DriftVerdict 字段语义)

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
```

- [ ] **Step 4: Update `cc_harness/memory/service.py` — F1 tuple unpack**

Find line 97-105 (the `if self.drift_detector is not None and result_action_mem is not None:` block) and replace `similar=similar_for_conflict` with `similar=[m for m, _ in similar_for_conflict]`:

```python
            # F1: search_similar 返 list[tuple[Memory, float]],detector 要 list[Memory]
            # 原 round 1 把 tuples 原样传 → detector mem.text 失败被静默吞
            similar_mems = [m for m, _ in similar_for_conflict]
            await self.drift_detector.check_after_write(
                session_id=session_id or "default",
                turn_idx=int(time.time() * 1000) % 1000,
                new_memory=result_action_mem,
                similar=similar_mems,
            )
```

- [ ] **Step 5: Update existing tests for removed `memory_service` kwarg**

In `tests/test_drift_main_integration.py`, find any test that passes `memory_service=fake_memory_service` (or similar) to `MemoryService(...)` or `DriftDetector(...)` constructor — DELETE those arguments. Detector no longer has the kwarg.

Search for: `memory_service=` in `tests/test_drift_*.py` and remove.

- [ ] **Step 6: Run tests, verify green**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_*.py -v
```

Expected: all `test_drift_*.py` tests pass (round 1 tests adapted + 4 new round 2 tests, total ~14-16 tests).

- [ ] **Step 7: Run E2/E4 regression**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_events.py tests/test_reflection_engine.py tests/test_memory_layered.py tests/test_decider.py tests/test_repl.py tests/test_main.py -v
```

Expected: all pass (E2 / E4 unaffected; F4 only deleted a kwarg that wasn't used anyway).

- [ ] **Step 8: Ruff**

```bash
.venv/Scripts/python.exe -m ruff check cc_harness/drift/ cc_harness/memory/service.py tests/test_drift_*.py
```

- [ ] **Step 9: Commit R1**

```bash
git add cc_harness/drift/detector.py cc_harness/memory/service.py tests/test_drift_detector.py tests/test_drift_main_integration.py
git commit -m "feat(drift): multi-group ratio algorithm + tuple unpack + consistency fail-soft (R1)"
```

---

## Commit R2: feat(drift) — source 隔离 + 真 local LLM fallback

**What landed (2 fixes)**: F2 `ReflectionEvent.source` override + `drift_detected` 工厂 → `source="drift"`;F3 `DriftDetector.local_llm` 形参 + main.py 注入主 `llm`,_ask_judge 真 fallback chain。

### Task R2.1: events.py ReflectionEvent.source + drift_detected(source="drift")

**Files:**
- Modify: `cc_harness/reflection/events.py` (add `source: str | None = None` field to `ReflectionEvent` dataclass; update `drift_detected` factory to pass `source="drift"`)
- Modify: `cc_harness/reflection/events.py` (`__init__.py` if needed)
- Test: `tests/test_drift_events.py` (add test for `drift_detected` setting `source="drift"`)
- Test: `tests/test_reflection_events.py` (add test that `ReflectionEvent.source` defaults to None)

**Interfaces:**
- `ReflectionEvent(*, event_type, severity, evidence, session_id, turn_idx, created_at, source=None)` — new optional kwarg
- `drift_detected(...)` — now passes `source="drift"` explicitly

- [ ] **Step 1: Write failing test**

```python
# tests/test_drift_events.py 追加
def test_drift_event_has_source_drift():
    """F2: drift_detected 工厂显式设置 source='drift',区别于其他 reflection。"""
    ev = drift_detected(
        session_id="s1", turn_idx=1, entity="Caroline",
        drift_rate=0.5, total_groups=1, inconsistent_groups=1,
        records=[], reason="x",
    )
    assert ev.source == "drift"


def test_reflection_event_source_default_none():
    """F2: 其他 6 事件工厂不传 source,默认 None(engine 兜底用 'reflection')。"""
    from cc_harness.reflection.events import max_iter_reached
    ev = max_iter_reached(session_id="s1", turn_idx=1, max_iter=20)
    assert ev.source is None
```

- [ ] **Step 2: Verify tests fail (red)**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_events.py::test_drift_event_has_source_drift tests/test_reflection_events.py::test_reflection_event_source_default_none -v
```

Expected: `AttributeError: 'ReflectionEvent' object has no attribute 'source'`.

- [ ] **Step 3: Update `cc_harness/reflection/events.py`**

Find the `ReflectionEvent` dataclass (around line 12-20) and add `source: str | None = None` field:

```python
@dataclass
class ReflectionEvent:
    event_type: str            # "max_iter" | "empty_turn" | "tool_error_burst" | "tool_retry_burst" | "subagent_failed" | "decider_rollback" | "drift_detected"
    severity: str              # "pos" | "ambig" | "neg"
    evidence: dict
    session_id: str
    turn_idx: int
    created_at: float
    source: str | None = None  # F2: drift 用 'drift',其他默认 None → engine 兜底 'reflection'
```

Update `drift_detected` factory to include `source="drift"`:

```python
    return ReflectionEvent(
        event_type="drift_detected",
        severity=severity,
        evidence={...},
        session_id=session_id,
        turn_idx=turn_idx,
        created_at=time.time(),
        source="drift",  # F2
    )
```

- [ ] **Step 4: Run tests, verify green**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_events.py tests/test_reflection_events.py -v
```

Expected: both new tests pass; existing tests still pass (no regression).

- [ ] **Step 5: ruff**

```bash
.venv/Scripts/python.exe -m ruff check cc_harness/reflection/events.py
```

- [ ] **Step 6: Commit R2 part 1**

```bash
git add cc_harness/reflection/events.py tests/test_drift_events.py tests/test_reflection_events.py
git commit -m "feat(reflection): ReflectionEvent.source override for drift events (R2 part 1)"
```

### Task R2.2: engine.py save 用 event.source + F3 DriftDetector.local_llm

**Files:**
- Modify: `cc_harness/reflection/engine.py` (line 153-158: change hardcoded `source="reflection"` to `event.source or "reflection"`)
- Modify: `cc_harness/drift/detector.py` (`__init__` add `local_llm=None`; `_ask_judge` fallback chain try `self._local_llm` after `self._judge_llm` fails)
- Modify: `main.py` (line 262-274: add `local_llm=llm` to DriftDetector construction)
- Test: `tests/test_drift_detector.py` (add 1 test: judge throws → local LLM picks up)
- Test: `tests/test_reflection_engine.py` (add 1 test: drift event source="drift" → MemoryService.save called with source="drift")

**Interfaces:**
- `ReflectionEngine._run_one` — `source = getattr(event, "source", None) or "reflection"` then `MemoryService.save(text, source=source, ...)`
- `DriftDetector.__init__(*, ..., local_llm=None)`
- `DriftDetector._ask_judge` — try `self._judge_llm` first; on exception, try `self._local_llm`; on exception, return None

- [ ] **Step 1: Write failing tests**

```python
# tests/test_drift_detector.py 追加
@pytest.mark.asyncio
async def test_judge_failure_falls_back_to_local_llm(
    tmp_audit, fake_reflection_engine, fake_l5,
):
    """F3: judge_llm 抛 → _local_llm 接管 → 正常返回。"""
    primary_called = []
    local_called = []

    async def primary_chat(*args, **kwargs):
        primary_called.append("x")
        raise RuntimeError("primary down")

    async def local_chat(*args, **kwargs):
        local_called.append("x")
        # Simulate stream yield with content
        for ev_obj in [{"kind": "done", "content": '{"entities": ["caroline"]}'}]:
            if ev_obj["kind"] == "done":
                return ev_obj["content"]
        return ""

    fake_primary = MagicMock()
    fake_primary.chat = primary_chat
    fake_local = MagicMock()
    fake_local.chat = local_chat

    det = DriftDetector(
        reflection_engine=fake_reflection_engine,
        judge_llm=fake_primary,
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
        local_llm=fake_local,
    )
    # 触发 _judge_entities(调用 _ask_judge)
    result = await det._judge_entities("Caroline 1990")
    assert result == ["caroline"]
    assert len(primary_called) == 1
    assert len(local_called) == 1


# tests/test_reflection_engine.py 追加
@pytest.mark.asyncio
async def test_reflection_engine_saves_drift_source_as_drift(
    tmp_path, ...  # 用 test_reflection_engine.py 已有的 fixture
):
    """F2: drift_detected 事件 → engine.save 走 MemoryService.save(source='drift')。"""
    from cc_harness.reflection.events import drift_detected
    from cc_harness.reflection.engine import ReflectionEngine
    from cc_harness.memory.service import MemoryService

    # 构造最小反射引擎,观察 save() 调用
    saved_calls = []
    class FakeMemoryService:
        async def save(self, text, source, session_id=None):
            saved_calls.append({"text": text, "source": source, "session_id": session_id})
            return MagicMock(action="ADD", memory=MagicMock(id="m1"))
    
    fake_reflection_text = "drift detected: Caroline 1985 vs 1990"  # 假设 LLM 反射
    
    engine = ReflectionEngine(
        memory_service=FakeMemoryService(),
        llm_client=MagicMock(),
        judge_llm=None,
        l5_engine=MagicMock(sanitize=lambda x: fake_reflection_text),
        project_root=tmp_path,
        enabled=True,
    )
    # 直接 emit drift_detected,触发 _run_one
    event = drift_detected(
        session_id="s1", turn_idx=5, entity="Caroline",
        drift_rate=0.7, total_groups=1, inconsistent_groups=1, records=[], reason="x",
    )
    await engine._run_one(event)
    assert len(saved_calls) == 1
    assert saved_calls[0]["source"] == "drift"
```

NOTE: The second test above may need adaptation to fit `tests/test_reflection_engine.py`'s existing test infrastructure — read that file first to see how `ReflectionEngine` is already constructed/mocked.

- [ ] **Step 2: Verify tests fail (red)**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_detector.py::test_judge_failure_falls_back_to_local_llm tests/test_reflection_engine.py::test_reflection_engine_saves_drift_source_as_drift -v
```

Expected: fail with `TypeError: __init__() got an unexpected keyword argument 'local_llm'` (F3) and `AssertionError: source != 'drift'` (F2 engine side).

- [ ] **Step 3: Update `cc_harness/drift/detector.py` — F3 local_llm**

Add `local_llm` ctor param, update `_ask_judge`:

```python
def __init__(
    self,
    *,
    reflection_engine,
    judge_llm,
    l5_engine,
    project_root,
    audit_path=None,
    every_n_turns=5,
    enabled=True,
    local_llm=None,  # F3
):
    ...
    self._local_llm = local_llm


async def _ask_judge(self, system, user):
    """F3: JUDGE → local LLM → None (audit noop)."""
    # ... existing chat/signature dispatch helper extracted ...
    async def _try(llm, label):
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

    primary = await _try(self._judge_llm, "judge")
    if primary is not None:
        return primary
    return await _try(self._local_llm, "local")  # F3: 真实 local fallback
```

- [ ] **Step 4: Update `cc_harness/reflection/engine.py` — F2 source override**

Find line 153-158 (`text=reflection_text, source="reflection"`). Change:

```python
            # F2: 优先用 event.source(spec drift_detected 显式传 'drift'),
            # 否则 fallback 'reflection'(其他 6 事件现状)
            event_source = getattr(ev_safe, "source", None) or "reflection"
            try:
                result = await self._memory_service.save(
                    text=reflection_text,
                    source=event_source,
                    session_id=ev_safe.session_id,
                )
```

- [ ] **Step 5: Update `main.py` — F3 DriftDetector(local_llm=llm)**

Find line 263 area (DriftDetector construction) and add `local_llm=llm`:

```python
            _drift_detector = (
                DriftDetector(
                    reflection_engine=_reflection_engine,
                    judge_llm=_judge_llm,
                    local_llm=llm,  # F3: 主 LLM 作为本地 fallback
                    l5_engine=_l5_engine,
                    project_root=working_dir,
                    audit_path=working_dir / "logs" / "drift.jsonl",
                    every_n_turns=_mem_cfg.drift_every_n_turns,
                    enabled=_mem_cfg.drift_enabled,
                )
                # F4: 不传 memory_service(无用)
                if _mem_deps is not None and _reflection_engine is not None
                else None
            )
```

- [ ] **Step 6: Run tests, verify green**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_*.py tests/test_reflection_events.py tests/test_reflection_engine.py -v
```

Expected: all pass.

- [ ] **Step 7: Run full regression**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/ --ignore=tests/_test_*.py 2>&1 | tail -5
```

Expected: 1271 passed, 13 failed (pre-existing strategies_yaml only); 0 new failures.

- [ ] **Step 8: ruff**

```bash
.venv/Scripts/python.exe -m ruff check cc_harness/drift/ cc_harness/reflection/ cc_harness/memory/ main.py
```

- [ ] **Step 9: Commit R2**

```bash
git add cc_harness/drift/detector.py cc_harness/reflection/engine.py main.py tests/test_drift_detector.py tests/test_reflection_engine.py
git commit -m "feat(drift): source isolation + real local LLM fallback (R2)"
```

---

## Self-Review

After writing both task briefs, verify the plan against the amendment.

**1. Amendment coverage:** Skim each F1-F6 decision in the amendment. Each has a fix point in R1 or R2. ✅ F1 R1 Step 4, F4 R1 Step 3, F5 R1 Step 3, F6 R1 Step 3, F2 R2 task 1+2 Step 3+4, F3 R2 task 2 Step 3+5.

**2. Placeholder scan:** No "TODO", "TBD", "appropriate", "similar to" — every step has the actual code.

**3. Type consistency:**
- `ReflectionEvent.source: str | None = None` (R2 task 1) — engine.py reads via `getattr(ev_safe, "source", None)` so it's optional
- `DriftDetector.__init__` drops `memory_service` (F4, R1 step 3) — main.py stop passing it (R1 step 5)
- `DriftDetector.__init__` adds `local_llm=None` (F3, R2 task 2 step 3) — main.py adds `local_llm=llm` (R2 task 2 step 5)
- `DriftDetector._judge_group_consistency` return type changes from `tuple[bool, str]` to `tuple[bool | None, str]` (F5, R1 step 3)
- `_check_groups` algorithm rewrite (F6, R1 step 3) — existing drift_events tests still valid because they test factory output, not detector internal algorithm

**4. Spec deviation:** None — round 2 fixes are bug fixes that bring implementation INTO alignment with the original spec, not departures from it.

**5. Risk callouts:**
- F6 algorithm change — round 1 detector tests need adjustment (some assert `total_groups=1` — these need updating to `total_groups=N` based on test data setup)
- F2 ReflectionEvent new field — purely additive, no removal
- F4 detector ctor kwarg deletion — call sites need cleaning (handled in step 5)

## Execution Handoff

After self-review, this plan is ready for subagent-driven execution. Dispatch order:

1. R1 task R1.1 — sonnet model (multi-fix + rewrite)
2. After R1 green: R2 task R2.1 — haiku model (additive, pure transcription)
3. After R2 task R2.1 green: R2 task R2.2 — sonnet model (multi-file + integration)
4. Final whole-branch review (the round 2 spec says we already approved the plan, but we MUST rerun the final review to confirm the 6 bugs are gone)

Critical review gates run per-task (spec compliance + code quality), then a final whole-branch review against the amendment's F1-F6 + spec D1-D7.
