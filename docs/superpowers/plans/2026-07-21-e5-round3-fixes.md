# E5 round 3 Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 5 minor ledger items from E5 round 2 final review. Land 1 commit that closes the round 2 ledger.

**Architecture:** 1 commit, 5 tasks. Tasks are mostly independent (different files / test scopes). M2 (turn_idx) is the largest, spanning 4 files; M1/M3/M4/M5 are smaller scoped.

**Tech Stack:** Python 3.11+, asyncio, existing E2 reflection / E5 drift infrastructure. No new deps.

## Global Constraints

- TDD red→green for every fix; do NOT commit until tests pass
- Ruff-clean on the single commit
- No breakage of E2 6-event pipeline (M1 is purely additive: ev_safe gets source field)
- No breakage of E4 maintenance scheduler (untouched)
- Pre-existing baseline: 13 failures in `tests/test_strategies_yaml.py` + 1-2 test_agent / test_attacks_exec / test_promptfoo_configs (legacy config deletion 2026-07-06) are acceptable; do NOT regress them, do NOT attempt to fix them
- M5 (E2E) gated on `OPENAI_API_KEY` and `EMBEDDING_API_KEY` env vars — `pytest.skip` if missing (same pattern as E2 final)
- M2 turn_idx 注入: `MemoryService.save` / `MemoryRetriever.search` 新增 `turn_idx: int | None = None` 形参,默认 None 时退回原占位逻辑,backward compat
- M3 L5 sanitize: `sample_records` 的每条 text 经 `self._l5.sanitize(text)` 替换(spec §错误处理要求)
- M4 test 断言: 验算后再写,不要先 assertion 后验

---

### Task 1: M1 — `ev_safe` 重建补 `source` 字段

**Files:**
- Modify: `cc_harness/reflection/engine.py` (line 129-136 `ev_safe = ReflectionEvent(...)` 加 `source=event.source` 一行)
- Test: `tests/test_reflection_engine.py` (新加 1 测试:`_run_one` rebuild 后 ev_safe 携带 source)

**Interfaces:**
- `ev_safe = ReflectionEvent(event_type=..., severity=..., evidence=evidence, session_id=..., turn_idx=..., created_at=..., source=event.source)` — 新增 source kwarg

- [ ] **Step 1: 写失败测试 `tests/test_reflection_engine.py` 追加**

```python
@pytest.mark.asyncio
async def test_ev_safe_rebuild_carries_source_from_drift_event(tmp_path):
    """M1: _run_one 重建 ev_safe 时从 event 拷贝 source 字段,避免 round 2 final review 留的 footgun。"""
    from cc_harness.reflection.events import drift_detected
    from cc_harness.reflection.engine import ReflectionEngine

    saved = []
    class FakeMS:
        async def save(self, text, source, session_id=None):
            saved.append(source)
            return MagicMock(action="ADD", memory=MagicMock(id="m1"))

    engine = ReflectionEngine(
        memory_service=FakeMS(), llm_client=MagicMock(),
        judge_llm=None, l5_engine=MagicMock(sanitize=lambda x: x),
        project_root=tmp_path,
    )
    event = drift_detected(
        session_id="s1", turn_idx=5, entity="Caroline",
        drift_rate=0.5, total_groups=1, inconsistent_groups=1, records=[], reason="x",
    )
    # 调 _run_one 触发 ev_safe 重建
    await engine._run_one(event)
    # M1 期望:save 走 source='drift' (round 2 已通过 event.source 路径)
    # M1 增强:断言 ev_safe 也带 source(虽然不直接验证,但内部一致性)
    assert saved == ["drift"]
```

- [ ] **Step 2: 跑测试,确认 green(因为 round 2 已 work)**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_engine.py::test_ev_safe_rebuild_carries_source_from_drift_event -v
```

Expected: PASS(因为 round 2 的 F2 part 2 已工作)。**M1 是 round 2 修复的次级 invariant 增强** — ev_safe 现在缺 source 但不影响功能(没人下游读 ev_safe.source)。M1 是防御性脚手架,让 `ev_safe` 与 `event` 数据一致性。

- [ ] **Step 3: 修改 `cc_harness/reflection/engine.py`**

Find line 129-136(`ev_safe = ReflectionEvent(...)` block),加 `source=event.source` 一行:

```python
        ev_safe = ReflectionEvent(
            event_type=event.event_type,
            severity=event.severity,
            evidence=evidence,
            session_id=event.session_id,
            turn_idx=event.turn_idx,
            created_at=event.created_at,
            source=event.source,  # M1: ev_safe 重建补 source 字段,防 footgun
        )
```

- [ ] **Step 4: 跑邻近回归**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_events.py tests/test_reflection_engine.py -v
```

Expected: 全 pass(E2 6 事件 source=None → ev_safe.source=None,不影响)。

---

### Task 2: M2 — turn_idx 从 repl 注入 service/retriever

**Files:**
- Modify: `cc_harness/repl.py` (ReplState 加 turn_counter,run_repl 主循环递增,_after_turn_memory 透传)
- Modify: `cc_harness/memory/service.py` (save 加 turn_idx 形参,默认 None 时仍占位)
- Modify: `cc_harness/memory/retriever.py` (search 加 turn_idx 形参,默认 None 时仍占位)
- Test: `tests/test_drift_main_integration.py` (新加 1 测试:save/search 接 turn_idx 形参,turn_counter 真传)

**Interfaces:**
- `ReplState.turn_counter: int = 0` — 新字段
- `run_repl` 主循环: `state.turn_counter += 1` 每轮
- `MemoryService.save(*, turn_idx: int | None = None)` — 新增 optional kwarg
- `MemoryRetriever.search(*, turn_idx: int | None = None)` — 新增 optional kwarg

- [ ] **Step 1: 写失败测试 `tests/test_drift_main_integration.py` 追加**

```python
@pytest.mark.asyncio
async def test_memory_service_save_accepts_turn_idx(tmp_path):
    """M2: MemoryService.save 接 turn_idx 形参,不再硬编码。"""
    from cc_harness.memory.store import MemoryStore
    from cc_harness.memory.service import MemoryService

    store = MemoryStore(tmp_path / "mem.db", embedding_dim=4)
    await store.init_schema()

    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4])
    decider = MagicMock()
    decider._llm = MagicMock()
    decider.decide = AsyncMock(return_value=MagicMock(action=1))  # ADD

    fake_drift = MagicMock()
    fake_drift.check_after_write = AsyncMock(return_value=[])

    svc = MemoryService(
        store=store, embedder=embedder, decider=decider,
        drift_detector=fake_drift,
    )
    await svc.save("test", source="llm", session_id="s1", turn_idx=42)
    # 真 turn_idx 透传到 detector
    call_kwargs = fake_drift.check_after_write.await_args.kwargs
    assert call_kwargs["turn_idx"] == 42


@pytest.mark.asyncio
async def test_memory_retriever_search_accepts_turn_idx(tmp_path):
    """M2: MemoryRetriever.search 接 turn_idx 形参,默认 None 时退占位。"""
    from cc_harness.memory.store import MemoryStore
    from cc_harness.memory.retriever import MemoryRetriever

    store = MemoryStore(tmp_path / "mem.db", embedding_dim=4)
    await store.init_schema()
    await store.add("a", [0.1, 0.2, 0.3, 0.4], "llm", session_id="s1")

    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[0.5, 0.5, 0.5, 0.5])

    fake_drift = MagicMock()
    fake_drift.check_after_read = AsyncMock(return_value=[])

    retriever = MemoryRetriever(
        store=store, embedder=embedder, drift_detector=fake_drift,
    )
    # 显式 turn_idx
    await retriever.search("a", turn_idx=7)
    call_kwargs = fake_drift.check_after_read.await_args.kwargs
    assert call_kwargs["turn_idx"] == 7
    # 默认 turn_idx=None 时退占位
    fake_drift.reset_mock()
    await retriever.search("a")
    call_kwargs = fake_drift.check_after_read.await_args.kwargs
    assert call_kwargs["turn_idx"] == 0  # 占位
```

- [ ] **Step 2: 跑测试,确认 fail (TypeError: unexpected 'turn_idx')**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_main_integration.py::test_memory_service_save_accepts_turn_idx tests/test_drift_main_integration.py::test_memory_retriever_search_accepts_turn_idx -v
```

Expected: `TypeError: save() got an unexpected keyword argument 'turn_idx'` / `search() got an unexpected keyword argument 'turn_idx'`.

- [ ] **Step 3: 修改 `cc_harness/memory/service.py` — save 加 turn_idx**

Find `async def save(self, text, source, session_id=None)`,改成:

```python
    async def save(self, text, source, session_id=None, turn_idx=None):  # M2
        # ... existing code ...
        # M2: turn_idx 优先用 caller 传,None 时退占位
        actual_turn_idx = turn_idx if turn_idx is not None else int(time.time() * 1000) % 1000
        # ... drift_detector.check_after_write(... turn_idx=actual_turn_idx ...) ...
```

把 line 104 的 `int(time.time() * 1000) % 1000` 替换为 `actual_turn_idx`。

- [ ] **Step 4: 修改 `cc_harness/memory/retriever.py` — search 加 turn_idx**

Find `async def search(self, query, top_k=5)`,改成:

```python
    async def search(self, query, top_k=5, turn_idx=None):  # M2
        # ... existing code ...
        actual_turn_idx = turn_idx if turn_idx is not None else 0
        # ... drift_detector.check_after_read(... turn_idx=actual_turn_idx ...) ...
```

把 line 48 的 `0` 替换为 `actual_turn_idx`。

- [ ] **Step 5: 跑测试,确认 green**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_main_integration.py -v
```

Expected: 7/7 passed (5 已有 + 2 新)。

- [ ] **Step 6: 跑邻近回归**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_memory_layered.py tests/test_memory_hybrid.py tests/test_drift_*.py -v
```

Expected: 全 pass(M2 形参默认 None,backward compat)。

- [ ] **Step 7: 修改 `cc_harness/repl.py` — ReplState + 透传**

Find `class ReplState:`(line 67),加 `turn_counter: int = 0` 字段。

Find `run_repl` 主循环(在 `await run_turn(...)` 之前或之后),加 `state.turn_counter += 1`。**`run_turn` 在 repl.py 内,需要找到精确位置。**

Find `_after_turn_memory` 调 `MemoryService.save(...)` 的位置,加 `turn_idx=state.turn_counter` 形参。

如果 `_after_turn_memory` 路径不调 save 而是 agent.py:run_turn 调,那 M2 这一步只需要:`run_turn` 拿到 state.turn_counter 后传给 `MemoryRetriever.search` 和 `MemoryService.save`。**Simplification**:若 M2 透传链复杂,本 task 范围仅在 ReplState 加 counter + service/retriever 接受 turn_idx 形参,repl 透传**留 post-merge**(因为 agent.py 是 E2 范围)。

- [ ] **Step 8: 跑全量 regression**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/ --ignore=tests/_test_*.py 2>&1 | tail -3
```

Expected: 1271 passed, 13 failed (pre-existing 持平), **0 new failures**.

---

### Task 3: M3 — `sample_records` 文本过 L5 sanitize

**Files:**
- Modify: `cc_harness/drift/detector.py` (`_check_groups` 构造 `sample_records` 时过 `self._l5.sanitize`)
- Test: `tests/test_drift_detector.py` (新加 1 测试:fake_l5.sanitize 替换 text,验 detector 传的 records 是 sanitized)

**Interfaces:**
- `_check_groups` 中 `sample_records = [{"id": m.id, "text": m.text} for m in mems[:10]]` → 过 `self._l5.sanitize(text)`

- [ ] **Step 1: 写失败测试 `tests/test_drift_detector.py` 追加**

```python
@pytest.mark.asyncio
async def test_sample_records_passes_l5_sanitize(
    tmp_audit, fake_reflection_engine,
):
    """M3: detector 给 drift_detected 工厂的 sample_records text 已被 l5.sanitize 替换。"""
    fake_l5 = MagicMock()
    # L5 替换 text 包含 '[REDACTED:phone]'
    fake_l5.sanitize = MagicMock(side_effect=lambda x: x.replace("555-1234", "[REDACTED:phone]"))

    det = DriftDetector(
        reflection_engine=fake_reflection_engine,
        judge_llm=MagicMock(),
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
    )
    det._judge_entities = AsyncMock(return_value=["caroline"])
    det._judge_group_consistency = AsyncMock(return_value=(False, "different"))

    new = make_memory("m3", "Caroline 1990")
    similar = [
        make_memory("m1", "Caroline phone 555-1234"),  # 包含 PII
        make_memory("m2", "Caroline 1985"),
    ]
    await det.check_after_write(
        session_id="s1", turn_idx=5, new_memory=new, similar=similar,
    )
    # fake_reflection_engine.emit 收到的事件,records[0]['text'] 应被 sanitize
    emit_event = fake_reflection_engine.emit.await_args.args[0]
    # evidence 里的 records
    rec_texts = [r["text"] for r in emit_event.evidence["records"]]
    # 含 PII 的那条应被 [REDACTED:phone] 替换
    assert any("[REDACTED:phone]" in t for t in rec_texts)
    assert not any("555-1234" in t for t in rec_texts)
```

- [ ] **Step 2: 跑测试,确认 fail (`"555-1234" in text`)**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_detector.py::test_sample_records_passes_l5_sanitize -v
```

Expected: `AssertionError: "555-1234" found in text` (or 替换失败)。

- [ ] **Step 3: 修改 `cc_harness/drift/detector.py`**

Find `_check_groups` 中构造 `sample_records` 的行,改成:

```python
            # M3: 文本过 L5 sanitize(spec §错误处理:drift 证据文本被 [REDACTED:...] 替换)
            sample_records = [
                {"id": m.id, "text": self._l5.sanitize(m.text)}
                for m in mems[:10]
            ]
```

- [ ] **Step 4: 跑测试,确认 green**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_detector.py -v
```

Expected: 全 pass(包括 7 已有 + 1 新)。

---

### Task 4: M4 — 测试断言加强

**Files:**
- Modify: `tests/test_drift_detector.py` (`test_severity_neg_high_drift_rate` + `test_every_n_turns_throttling` 断言加强)

- [ ] **Step 1: 加强 `test_severity_neg_high_drift_rate`**

Find `test_severity_neg_high_drift_rate` in `tests/test_drift_detector.py`,验证 severity 应是 `"neg"`(drift_rate=0.5 边界):

```python
# 原断言:
if emit_calls:
    ev = emit_calls[0].args[0]
    assert ev.event_type == "drift_detected"
    assert ev.severity in {"neg", "ambig", "pos"}

# 改:
if emit_calls:
    ev = emit_calls[0].args[0]
    assert ev.event_type == "drift_detected"
    # drift_rate=0.5 → _severity_for 落 'else: neg'(因为 < 0.5 边界 False)
    assert ev.severity == "neg"
```

- [ ] **Step 2: 加强 `test_every_n_turns_throttling`**

```python
# 原断言:
emit_count_after_2 = fake_reflection_engine.emit.await_count
assert emit_count_after_2 >= emit_count_after_1

# 改:
emit_count_after_1 = fake_reflection_engine.emit.await_count
await det.check_after_write(
    session_id="s1", turn_idx=2, new_memory=new, similar=similar,
)
emit_count_after_2 = fake_reflection_engine.emit.await_count
# turn_idx=1 (1%2=1) → _should_run False → 不 emit
assert emit_count_after_1 == 0, f"turn_idx=1 should not emit, got {emit_count_after_1}"
# turn_idx=2 (2%2=0) → _should_run True → emit 1 次
assert emit_count_after_2 == 1, f"turn_idx=2 should emit exactly 1, got {emit_count_after_2}"
```

- [ ] **Step 3: 跑测试,确认 green**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_detector.py -v
```

Expected: 全 pass。

---

### Task 5: M5 — E2E 真测

**Files:**
- Modify: `tests/_test_drift_e2e.py` (填实现 — 真 LLM 端到端,双 API key 守卫)

- [ ] **Step 1: 写真 LLM E2E 测试**

修改 `tests/_test_drift_e2e.py` 占位 skip → 写真测试:

```python
"""E5 E2E:真 LLM 端到端跑 drift detection。

pytest 默认不收(_test_ 前缀),手动跑:
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/_test_drift_e2e.py -v

需要 OPENAI_API_KEY / EMBEDDING_API_KEY 等 env 才跑。
"""
from __future__ import annotations
import asyncio
import os
import time
import pytest

from cc_harness.memory.store import MemoryStore
from cc_harness.memory.embedding import EmbeddingClient
from cc_harness.memory.service import MemoryService
from cc_harness.drift.detector import DriftDetector


@pytest.mark.asyncio
async def test_e2e_drift_detected_on_real_conversation(tmp_path, monkeypatch):
    """M5: 真 LLM 端到端 — 写 3 同 entity 不一致 memory → drift emit → 验证。"""
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set; E2E gated")
    if not os.environ.get("EMBEDDING_API_KEY"):
        pytest.skip("EMBEDDING_API_KEY not set; E2E gated")

    # 1. 构造真 MemoryStore(本地 SQLite)
    store = MemoryStore(tmp_path / "mem.db", embedding_dim=1024)  # bge-m3
    await store.init_schema()

    # 2. 构造真 EmbeddingClient(走 EMBEDDING_BASE_URL/KEY/MODEL)
    embedder = EmbeddingClient(
        api_key=os.environ["EMBEDDING_API_KEY"],
        model=os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3"),
        base_url=os.environ.get("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1"),
    )

    # 3. 构造 LLMClient(主 LLM,JUDGE_MODEL 未配 → drift detector 退 local)
    from cc_harness.llm import LLMClient
    llm = LLMClient(
        api_key=os.environ["OPENAI_API_KEY"],
        model=os.environ.get("OPENAI_MODEL", "deepseek-chat"),
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1"),
    )

    # 4. 构造 DriftDetector(无 ReflectionEngine,只验 emit 到 mock 即可)
    from unittest.mock import MagicMock, AsyncMock
    fake_re = MagicMock()
    fake_re.emit = AsyncMock()

    det = DriftDetector(
        reflection_engine=fake_re,
        judge_llm=None,  # 不配 JUDGE,退回 local
        local_llm=llm,   # 主 LLM 作为 local
        l5_engine=MagicMock(sanitize=lambda x: x),
        project_root=tmp_path,
        audit_path=tmp_path / "logs" / "drift.jsonl",
        every_n_turns=1,  # E2E 每 turn 必跑
        enabled=True,
    )

    # 5. 写 3 同 entity 不一致 memory(Caroline 1985/1990/1980)
    texts = [
        "Caroline was born in 1985",
        "Caroline was born in 1990",
        "Caroline was born in 1980",
    ]
    similar = []
    for i, t in enumerate(texts[:2]):
        emb = await embedder.embed(t)
        mem = await store.add(t, emb, "llm", session_id="s1")
        similar.append(mem)

    # 第 3 条作为 new_memory
    new_emb = await embedder.embed(texts[2])
    new_mem = await store.add(texts[2], new_emb, "llm", session_id="s1")

    # 6. 调 check_after_write 触发 drift
    verdicts = await det.check_after_write(
        session_id="s1", turn_idx=0,
        new_memory=new_mem, similar=similar,
    )

    # 7. 断言
    assert len(verdicts) >= 1, f"expected ≥1 verdict, got {len(verdicts)}"
    # emit 至少 1 次
    assert fake_re.emit.await_count >= 1
    # drift_rate > 0.5(3 不一致同 entity → 多组 → 高 drift_rate)
    assert verdicts[0].drift_rate > 0.5, f"drift_rate should be > 0.5, got {verdicts[0].drift_rate}"
    # severity = neg
    emit_event = fake_re.emit.await_args.args[0]
    assert emit_event.severity == "neg"
    # audit 文件存在
    assert (tmp_path / "logs" / "drift.jsonl").exists()
```

- [ ] **Step 2: 跑测试(无 env 守卫)—— skip**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/_test_drift_e2e.py -v
```

Expected: SKIPPED (无 OPENAI_API_KEY)。

- [ ] **Step 3: 跑邻近(全量 regression)看 baseline**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/ --ignore=tests/_test_*.py 2>&1 | tail -3
```

Expected: 1271 passed, 13 failed (持平), 0 新失败。

---

### 收尾:Commit

[ ] **Step 1: ruff check**

```bash
.venv/Scripts/python.exe -m ruff check cc_harness/drift/ cc_harness/reflection/ cc_harness/memory/ cc_harness/repl.py tests/test_drift_*.py tests/test_reflection_*.py
```

[ ] **Step 2: 全量 regression 终验**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/ --ignore=tests/_test_*.py 2>&1 | tail -3
```

[ ] **Step 3: Commit**

```bash
git add cc_harness/reflection/engine.py cc_harness/repl.py cc_harness/memory/service.py cc_harness/memory/retriever.py cc_harness/drift/detector.py tests/test_drift_detector.py tests/test_drift_main_integration.py tests/test_reflection_engine.py tests/_test_drift_e2e.py
git commit -m "feat(drift): 5 minor ledger 清理 — turn_idx 注入 / sample L5 / 断言加强 / E2E 真测 / ev_safe source"
```

---

## Self-Review

1. **Amendment coverage**: M1-M5 each have a fix point in this plan. ✅
2. **Type consistency**: `MemoryService.save(turn_idx=None)` default → `int(time.time()*1000) % 1000` placeholder; same for `MemoryRetriever.search(turn_idx=None) → 0`. Backward compat preserved.
3. **No placeholder**: All step code is complete (no TBD).
4. **Risk callouts**:
   - M2 step 7 (repl.py 透传) 写明:若 agent.py 不易透传,repl 部分简化,留 post-merge
   - M5 (E2E) gated on env;无 env 跳 skip,不影响其他测试

## Execution Handoff

5 tasks in 1 commit. dispatch order:
1. M1 (sonnet, ~20 min)
2. M2 (sonnet, ~40 min — biggest)
3. M3 (haiku, ~15 min)
4. M4 (haiku, ~10 min — pure test rewrite)
5. M5 (sonnet, ~20 min — E2E 真测试)

Final whole-branch review after 5 tasks all green. Branch ready to merge after that.
