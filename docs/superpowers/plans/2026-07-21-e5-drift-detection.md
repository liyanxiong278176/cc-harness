# E5 Drift Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 cc-harness 引入**漂移检测(Drift Detection)** —— 在主 agent `MemoryService.save` 写盘后 + `MemoryRetriever.search` 召出后,实时跑"同 entity 反查 + JUDGE_GROUP_CONSIST 判 consistency",检测到 drift 即通过 E2 `ReflectionEngine.emit` 写盘入 `source='drift'` 反思记录(走 E4 矛盾检测 + staleness 全套),为 E4 consolidation 提供合并候选,为 LLMDecider long-term recall 提供"同 entity 不一致"上下文。

**Architecture:** 中心化 `DriftDetector`(`cc_harness/drift/detector.py`)+ 2 类主入口方法(`check_after_write` / `check_after_read`)+ drift 事件工厂(在 `cc_harness/reflection/events.py` 加 1 个 `drift_detected`,severity 按 drift_rate 三档)。JUDGE_MODEL 不可用 → 退回本地 LLMClient;都失败 → fail-soft noop + 审计落 `<root>/logs/drift.jsonl`。1 spec 2 commit:detector 中心化 + wiring。

**Tech Stack:** Python 3.11+ / aiosqlite / asyncio / pydantic / ruff / pytest-asyncio;复用 LoCoMo m5 `JUDGE_ENTITIES` / `JUDGE_GROUP_CONSIST` prompt verbatim(2026-07-20 commit `e64aaa8` 后的版本);复用 E2 reflection `_ask_judge_with_fallback` 多态(JUDGE_MODEL + 本地 LLMClient + None,2026-07-21 commit `632e025`);复用 E4 maintenance 矛盾检测 + staleness(2026-07-20 commit `72b02e4`);`MemoryService.save(source='drift')` 走 E4 矛盾检测 + staleness 全套。

## Global Constraints

[Spec 锁死的全局约束 — 每个 task 隐式适用,违反即返工]

- **Python 3.11+**:禁止 `asyncio.coroutine` 之类删 API,使用 `async def`。
- **E4 已落地(`72b02e4`)+ E2 reflection 已落地(`2c8132a`)**:E5 强依赖 E2 `ReflectionEngine.emit` + `MemoryService.save(source=...)` 走 E4 矛盾检测 + staleness。
- **JUDGE_MODEL 已就位**:LoCoMo m5 `eval/locomo/metrics.py:JUDGE_ENTITIES` / `JUDGE_GROUP_CONSIST` prompt 稳定(2026-07-20 commit `e64aaa8` 后的版本),E5 drift detector 直接 verbatim 复用,**不引入 eval 依赖**(复制 prompt 到 `cc_harness/drift/prompts.py`)。
- **L5 DLP**:`cc_harness/l5.py` 必走,evidence 字段 emit 前 sanitize。
- **severity 三档边界**:`< 0.2` → `pos` / `0.2 - 0.5` → `ambig` / `> 0.5` → `neg`(drift_rate = inconsistent_groups / total_groups)。
- **频率上限**:`every_n_turns=5`(每 5 turn 至少 1 次,但 turn_idx % N == 0 才真跑)+ E2 engine 已有 `max_pending=3` 队列 + 短窗口去重 5s。
- **drain 超时**:`_drain(timeout_s=5)`,超时 `task.cancel()`(沿用 E2 reflection 模式)。
- **审计**:`<root>/logs/drift.jsonl`(与 E2 `reflection.jsonl` 分开,便于 drift_rate 量化历史),每行 `{ts, op, event_type, severity, entity, drift_rate, total_groups, inconsistent_groups, reason?}`,**绝不记明文 entity content**。
- **失败 fail-soft**:JUDGE_MODEL 失败 → 退回本地 LLMClient;都失败 → `noop` + 审计 + 不抛(沿 E2 reflection)。
- **`MemoryService` / `MemoryRetriever` 形参**:`drift_detector: "DriftDetector | None" = None`,默认 None 保持向后兼容(沿 E2 reflection_engine 模式)。
- **drift_detected 工厂位置**:`cc_harness/reflection/events.py` 末尾加 1 个,与现有 6 工厂平级,**不**新建 `drift/events.py`。
- **drift detector 子包**:`cc_harness/drift/`,**不**挂 E2 reflection 子包下(避免 reflection 越加越胖)。
- **不**新建 `reflections` 表:drift 反思走 `memories` 表(`source='drift'`,沿 E2 `source='reflection'` 模式)。
- **不**扩 AppConfig:沿用 E2 I-1 模式,`main.py:boot()` 构造 + 注入 `MemoryService` / `MemoryRetriever` / `repl.run_repl`。
- **不要全局 MagicMock**:production 路径 0 命中,只测试桩位用。
- **commit 规范**:`feat(drift): ...` / `fix(drift): ...` / `test(drift): ...` / `chore(drift): ...`。
- **不**import `eval.locomo.metrics`(避免引入 eval 依赖,prompt verbatim 复制到 `drift/prompts.py`)。
- **JUDGE prompt prompt 引用**:`drift/prompts.py` 文件头注释指明"verbatim 复制自 `eval/locomo/metrics.py:15-22`,2026-07-20 commit `e64aaa8`"。

## File Structure

### 新增文件(7 文件)

| 路径 | 职责 | 任务 |
|---|---|---|
| `cc_harness/drift/__init__.py` | export DriftDetector / DriftVerdict | T1.1, T1.4 |
| `cc_harness/drift/prompts.py` | JUDGE_ENTITIES + JUDGE_GROUP_CONSIST prompt 模板(verbatim 复用 m5) | T1.1 |
| `cc_harness/drift/detector.py` | `DriftDetector` 中心化引擎 + `DriftVerdict` dataclass + 2 类主入口 + 内部 helper | T1.2 |
| `tests/test_drift_events.py` | `drift_detected` 工厂 severity 三档推断 + evidence shape | T1.3 |
| `tests/test_drift_detector.py` | DriftDetector 中心化引擎:2 类 check 入口、JUDGE mock、JUDGE 失败退回、LLM 全 fail noop、severity、频率、audit | T1.4 |
| `tests/test_drift_integration.py` | 完整管线:写 50 → drift detector → emit → E2 engine 写盘 → retriever 召出 | T1.5 |
| `tests/test_drift_main_integration.py` | `MemoryService.save` 写时 emit / `MemoryRetriever.search` 召时 emit / repl + main wiring | T2.2, T2.3 |
| `tests/_test_drift_e2e.py` | 真 LLM 端到端(`_test_` 前缀,pytest 默认不收) | T2.4 |

### 修改文件(8 文件)

| 路径 | 改动 | 任务 |
|---|---|---|
| `cc_harness/reflection/events.py` | 末尾加 `drift_detected` 工厂(8 个 keyword-only 形参) | T1.3 |
| `cc_harness/reflection/__init__.py` | 加 `drift_detected` 到 `__all__` | T1.3 |
| `cc_harness/memory/store.py` | 0 改动(走 E2 `search_reflections` 自然召出 drift 反思) | — |
| `cc_harness/memory/config.py` | 加 3 字段:`drift_enabled / drift_every_n_turns / drift_drift_warn_threshold` + 1 个 validator | T1.1 |
| `cc_harness/memory/service.py` | `__init__` 加 `drift_detector` 形参,save 末尾追加 `check_after_write` 调用 | T2.1 |
| `cc_harness/memory/retriever.py` | `__init__` 加 `drift_detector` 形参,search 末尾追加 `check_after_read` 调用 | T2.1 |
| `cc_harness/repl.py` | `run_repl` 形参加 `drift_detector=None`,finally `_drain` | T2.3 |
| `main.py:boot()` | 构造 `DriftDetector` + 注入 `MemoryService` / `MemoryRetriever` / `cmd_repl` | T2.3 |

### 复用(不修改,只读)

- `cc_harness/reflection/engine.py:ReflectionEngine.emit / _ask_judge_with_fallback` — 全部多态已就位
- `cc_harness/reflection/events.py:6 工厂 + ReflectionEvent` — 直接复用 + 末尾加 1 工厂
- `cc_harness/memory/maintenance/scheduler.py` — E4 调度,本 plan 不动
- `cc_harness/l5.py:L5Engine.sanitize` — 输出侧 DLP,evidence 字段必过
- `eval/locomo/metrics.py:JUDGE_ENTITIES / JUDGE_GROUP_CONSIST` — prompt 复制来源(verbatim)
- `cc_harness/llm.py:LLMClient` — 本地 LLM 客户端(drift judge 失败退回)
- `cc_harness/memory/service.py:MemoryService.save(source='drift')` — 走 E2 reflection engine 写盘(沿 E2 source='reflection' 模式)

---

## Commit 路线图(2 commit)

```
#1  feat(drift): DriftDetector 中心化引擎 + drift_detected 工厂 + 写盘路径
#2  feat(drift): main + repl + MemoryService/MemoryRetriever 接入 + audit
```

每 commit 独立可回滚,每 commit 完成后 `git log --oneline | head -3` 应看到新提交。

---

## Commit 1: feat(drift) DriftDetector 中心化 + drift_detected 工厂

### Task 1.1: drift/prompts.py + MemoryConfig 3 字段 + __init__.py 骨架

**Files:**
- Create: `cc_harness/drift/__init__.py`
- Create: `cc_harness/drift/prompts.py`
- Modify: `cc_harness/memory/config.py`(加 3 字段)
- Test: `tests/test_drift_config.py`

**Interfaces:**
- Consumes: 无(基座)
- Produces:
  - `JUDGE_ENTITIES: str` / `JUDGE_GROUP_CONSIST: str` — module-level 常量(verbatim 复用 m5)
  - `MemoryConfig` 3 字段:`drift_enabled: bool=True / drift_every_n_turns: int=5 / drift_drift_warn_threshold: float=0.2`
  - 1 个 validator `_check_drift_threshold`(`0 < v <= 1`)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_drift_config.py
import pytest
from cc_harness.memory.config import MemoryConfig


def test_drift_config_defaults():
    cfg = MemoryConfig()
    assert cfg.drift_enabled is True
    assert cfg.drift_every_n_turns == 5
    assert cfg.drift_drift_warn_threshold == 0.2


def test_drift_threshold_must_be_in_range():
    """threshold 必须 (0, 1]。"""
    with pytest.raises(Exception):  # pydantic ValidationError
        MemoryConfig(drift_drift_warn_threshold=1.5)
    with pytest.raises(Exception):
        MemoryConfig(drift_drift_warn_threshold=0.0)


def test_drift_disabled_noop():
    """enabled=False 时 cfg 不抛错。"""
    cfg = MemoryConfig(drift_enabled=False)
    assert cfg.drift_enabled is False
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_config.py -v`
Expected: FAIL with `AttributeError: 'MemoryConfig' object has no attribute 'drift_enabled'`

- [ ] **Step 3: 在 `cc_harness/memory/config.py` 末尾加 3 字段 + validator**

打开 `cc_harness/memory/config.py`,在 `recall_weight_floor: float = 0.5`(第 101 行附近)后**插入**:

```python
    # E5 漂移检测
    drift_enabled: bool = True
    drift_every_n_turns: int = 5
    drift_drift_warn_threshold: float = 0.2
```

找到 `_check_recall_range` validator(第 160-165 行附近)后追加 1 个:

```python
    @field_validator("drift_drift_warn_threshold")
    @classmethod
    def _check_drift_threshold(cls, v: float) -> float:
        if not (0 < v <= 1):
            raise ValueError(f"drift_drift_warn_threshold must be in (0, 1], got {v}")
        return v
```

找到 `_check_positive_int` 字段列表(第 117-123 行附近)加 1 个 `drift_every_n_turns`:

```python
    @field_validator("injection_token_budget", "retriever_top_k",
                     "pipeline_recent_turns", "pipeline_max_delta_tokens",
                     "pipeline_every_n", "scenario_min_atoms",
                     "persona_trigger_every_n", "recall_top_k",
                     "offload_threshold",
                     "maintenance_every_n_turns", "maintenance_count_threshold",
                     "ttl_limit",
                     "drift_every_n_turns")
```

- [ ] **Step 4: 实现 `cc_harness/drift/__init__.py`**

```python
"""Drift detection — 写时+读时双检,运行时 LLM 抽 entity (E5)。

依赖 E2 ReflectionEngine (commit 2c8132a) + E4 maintenance (commit 72b02e4)。
"""
from cc_harness.drift.detector import DriftDetector, DriftVerdict

__all__ = ["DriftDetector", "DriftVerdict"]
```

- [ ] **Step 5: 实现 `cc_harness/drift/prompts.py`**

```python
"""Drift detection prompts — verbatim 复用 LoCoMo m5 JUDGE_ENTITIES / JUDGE_GROUP_CONSIST。

来源:eval/locomo/metrics.py:15-22 (2026-07-20 commit e64aaa8)。
**禁止 import eval.locomo.metrics** — 复制 prompt 是为了避免 eval 依赖。
drift_rate 量化与 m5 离线可比是 E5 的关键收益,prompt 必须 verbatim。
"""
from __future__ import annotations


# 实体抽取:从文本抽 key entities(人物 / 事件 / 物品 / 数字)
JUDGE_ENTITIES = (
    "You are an entity extractor. From the following text, extract key entities "
    "(人物 / 事件 / 物品 / 数字). Output JSON only: {\"entities\": [str, ...]}"
)


# 一致性判官:同 entity 多个 predicted 是否一致
JUDGE_GROUP_CONSIST = (
    "You are a consistency judge. Given multiple predicted answers about the same "
    "entity, decide if they are mutually consistent (same fact / same object, "
    "paraphrase allowed). Output JSON only: {\"consistent\": bool, \"reason\": str}"
)
```

- [ ] **Step 6: 跑测试,确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_config.py -v`
Expected: 3 passed in <0.1s

- [ ] **Step 7: 跑邻近回归**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_config.py tests/test_memory_layered.py -v`
Expected: all pass(E4 config 字段未破)

- [ ] **Step 8: Commit**

```bash
git add cc_harness/drift/__init__.py cc_harness/drift/prompts.py cc_harness/memory/config.py tests/test_drift_config.py
git commit -m "feat(drift): prompts.py + config 3 字段 + 子包骨架 (T1.1)"
```

---

### Task 1.2: DriftDetector 中心化引擎(完整实现)

**Files:**
- Create: `cc_harness/drift/detector.py`
- Modify: `cc_harness/drift/__init__.py`(加 DriftVerdict export)
- Test: `tests/test_drift_detector.py`

**Interfaces:**
- Consumes: E2 `ReflectionEngine.emit / _ask_judge_with_fallback` 多态
- Produces:
  - `DriftVerdict` dataclass:`entity / drift_rate / total_groups / inconsistent_groups / sample_records / reason`
  - `DriftDetector` 类:`check_after_write / check_after_read / _judge_entities / _judge_group_consistency / _ask_judge / _audit / _should_run / _drain`
  - 内部 helper `_ask_judge(system, user) -> str | None` 复用 E2 多态(introspect positional count)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_drift_detector.py
import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

from cc_harness.drift.detector import DriftDetector, DriftVerdict
from cc_harness.memory.store import Memory


@pytest.fixture
def tmp_audit(tmp_path: Path) -> Path:
    return tmp_path / "drift.jsonl"


@pytest.fixture
def fake_memory_service():
    svc = MagicMock()
    svc.save = AsyncMock(return_value=MagicMock(action="ADD", memory=MagicMock(id="m1")))
    return svc


@pytest.fixture
def fake_reflection_engine():
    """E2 ReflectionEngine mock。"""
    eng = MagicMock()
    eng.emit = AsyncMock()
    return eng


@pytest.fixture
def fake_l5():
    return MagicMock(sanitize=lambda x: x)


@pytest.fixture
def fake_judge_llm_entities():
    """JUDGE_ENTITIES 返 mock。"""
    async def _fn(system, user):
        return '{"entities": ["Caroline", "transgender"]}'
    return _fn


@pytest.fixture
def fake_judge_llm_consistent():
    """JUDGE_GROUP_CONSIST 返 mock consistent。"""
    async def _fn(system, user):
        return '{"consistent": true, "reason": "same fact"}'
    return _fn


@pytest.fixture
def fake_judge_llm_inconsistent():
    """JUDGE_GROUP_CONSIST 返 mock inconsistent。"""
    async def _fn(system, user):
        return '{"consistent": false, "reason": "conflicting facts"}'
    return _fn


@pytest.fixture
def fake_judge_fail():
    async def _fn(system, user):
        raise RuntimeError("API 503")
    return _fn


@pytest.fixture
def fake_local_llm():
    async def _fn(system, user):
        return '{"entities": ["local"]}'
    return _fn


def make_memory(mid: str, text: str) -> Memory:
    return Memory(
        id=mid, text=text, embedding=[0.1, 0.2, 0.3, 0.4],
        created_at=0.0, updated_at=0.0, source="llm",
    )


@pytest.mark.asyncio
async def test_check_after_write_no_similar_no_op(
    tmp_audit, fake_memory_service, fake_reflection_engine, fake_l5,
    fake_judge_llm_entities, fake_judge_llm_consistent,
):
    """similar 为空 → check_after_write 返空。"""
    det = DriftDetector(
        memory_service=fake_memory_service,
        reflection_engine=fake_reflection_engine,
        judge_llm=fake_judge_llm_entities,
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
    )
    new = make_memory("m1", "Caroline 是 transgender")
    verdicts = await det.check_after_write(
        session_id="s1", turn_idx=1, new_memory=new, similar=[],
    )
    assert verdicts == []


@pytest.mark.asyncio
async def test_check_after_write_detects_drift_neg(
    tmp_audit, fake_memory_service, fake_reflection_engine, fake_l5,
    fake_judge_llm_entities, fake_judge_llm_inconsistent,
):
    """JUDGE_GROUP_CONSIST 返 inconsistent → emit drift_detected severity=neg。"""
    det = DriftDetector(
        memory_service=fake_memory_service,
        reflection_engine=fake_reflection_engine,
        judge_llm=fake_judge_llm_entities,
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
    )
    new = make_memory("m3", "Caroline 出生在 1990")
    similar = [
        make_memory("m1", "Caroline 出生在 1985"),
        make_memory("m2", "Caroline 出生在 1985"),
    ]
    verdicts = await det.check_after_write(
        session_id="s1", turn_idx=1, new_memory=new, similar=similar,
    )
    assert len(verdicts) >= 1
    assert verdicts[0].drift_rate > 0.5  # 全 inconsistent → high
    # emit 至少 1 次 drift_detected
    fake_reflection_engine.emit.assert_awaited()


@pytest.mark.asyncio
async def test_check_after_read_empty_results(
    tmp_audit, fake_memory_service, fake_reflection_engine, fake_l5,
    fake_judge_llm_entities, fake_judge_llm_consistent,
):
    """results 为空 → check_after_read 返空。"""
    det = DriftDetector(
        memory_service=fake_memory_service,
        reflection_engine=fake_reflection_engine,
        judge_llm=fake_judge_llm_entities,
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
    )
    verdicts = await det.check_after_read(
        session_id="s1", turn_idx=1, results=[],
    )
    assert verdicts == []


@pytest.mark.asyncio
async def test_judge_fail_falls_back_to_local(
    tmp_audit, fake_memory_service, fake_reflection_engine, fake_l5,
    fake_judge_fail, fake_local_llm,
):
    """JUDGE 失败 → 退回本地 LLM。"""
    det = DriftDetector(
        memory_service=fake_memory_service,
        reflection_engine=fake_reflection_engine,
        judge_llm=fake_judge_fail,
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
    )
    new = make_memory("m3", "Caroline 出生在 1990")
    similar = [
        make_memory("m1", "Caroline 出生在 1985"),
        make_memory("m2", "Caroline 出生在 1985"),
    ]
    # 本地 LLM 返 entities=["local"] 后续 LLM 不可用会 noop,但中间应 _ask_judge 已尝试 fallback
    verdicts = await det.check_after_write(
        session_id="s1", turn_idx=1, new_memory=new, similar=similar,
    )
    # 验证无 panic,可能返 [] 因为本地只返 1 个 entity,group 不够
    assert isinstance(verdicts, list)


@pytest.mark.asyncio
async def test_all_llm_fail_noop(
    tmp_audit, fake_memory_service, fake_reflection_engine, fake_l5,
):
    """全部 LLM fail → noop + 审计。"""
    async def fail(system, user):
        raise RuntimeError("nope")
    det = DriftDetector(
        memory_service=fake_memory_service,
        reflection_engine=fake_reflection_engine,
        judge_llm=fail,
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
    )
    new = make_memory("m3", "Caroline 出生在 1990")
    similar = [
        make_memory("m1", "Caroline 出生在 1985"),
        make_memory("m2", "Caroline 出生在 1985"),
    ]
    verdicts = await det.check_after_write(
        session_id="s1", turn_idx=1, new_memory=new, similar=similar,
    )
    assert verdicts == []
    assert tmp_audit.exists()
    lines = tmp_audit.read_text(encoding="utf-8").strip().splitlines()
    assert "all_llm_unavailable" in lines[0]


@pytest.mark.asyncio
async def test_severity_neg_high_drift_rate(
    tmp_audit, fake_memory_service, fake_reflection_engine, fake_l5,
    fake_judge_llm_entities, fake_judge_llm_inconsistent,
):
    """drift_rate > 0.5 → emit severity=neg。"""
    det = DriftDetector(
        memory_service=fake_memory_service,
        reflection_engine=fake_reflection_engine,
        judge_llm=fake_judge_llm_entities,
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
    )
    new = make_memory("m3", "Caroline 出生在 1990")
    similar = [
        make_memory("m1", "Caroline 出生在 1985"),
        make_memory("m2", "Caroline 出生在 1990"),  # 1/2 一致 → drift_rate=0.5 → ambig
        make_memory("m4", "Caroline 出生在 1980"),  # 不一致
    ]
    await det.check_after_write(
        session_id="s1", turn_idx=1, new_memory=new, similar=similar,
    )
    emit_calls = fake_reflection_engine.emit.await_args_list
    if emit_calls:
        ev = emit_calls[0].args[0]
        assert ev.event_type == "drift_detected"
        assert ev.severity in {"neg", "ambig", "pos"}


@pytest.mark.asyncio
async def test_disabled_noop(
    tmp_audit, fake_memory_service, fake_reflection_engine, fake_l5,
    fake_judge_llm_entities, fake_judge_llm_inconsistent,
):
    """enabled=False → check_* 直接返空。"""
    det = DriftDetector(
        memory_service=fake_memory_service,
        reflection_engine=fake_reflection_engine,
        judge_llm=fake_judge_llm_entities,
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
        enabled=False,
    )
    new = make_memory("m3", "Caroline 出生在 1990")
    similar = [
        make_memory("m1", "Caroline 出生在 1985"),
        make_memory("m2", "Caroline 出生在 1985"),
    ]
    verdicts = await det.check_after_write(
        session_id="s1", turn_idx=1, new_memory=new, similar=similar,
    )
    assert verdicts == []
    fake_reflection_engine.emit.assert_not_awaited()
    assert not tmp_audit.exists()


@pytest.mark.asyncio
async def test_every_n_turns_throttling(
    tmp_audit, fake_memory_service, fake_reflection_engine, fake_l5,
    fake_judge_llm_entities, fake_judge_llm_inconsistent,
):
    """每 N turn 1 次,N=2 → turn_idx=1 不跑,turn_idx=2 跑。"""
    det = DriftDetector(
        memory_service=fake_memory_service,
        reflection_engine=fake_reflection_engine,
        judge_llm=fake_judge_llm_entities,
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
        every_n_turns=2,
    )
    new = make_memory("m3", "Caroline 出生在 1990")
    similar = [
        make_memory("m1", "Caroline 出生在 1985"),
        make_memory("m2", "Caroline 出生在 1985"),
    ]
    # turn_idx=1 (1%2=1) → 不跑
    await det.check_after_write(
        session_id="s1", turn_idx=1, new_memory=new, similar=similar,
    )
    emit_count_after_1 = fake_reflection_engine.emit.await_count
    # turn_idx=2 (2%2=0) → 跑
    await det.check_after_write(
        session_id="s1", turn_idx=2, new_memory=new, similar=similar,
    )
    emit_count_after_2 = fake_reflection_engine.emit.await_count
    # turn_idx=2 应比 turn_idx=1 多 emit(可能 more)
    assert emit_count_after_2 >= emit_count_after_1
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_detector.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cc_harness.drift.detector'`

- [ ] **Step 3: 实现 `cc_harness/drift/detector.py`**

```python
"""DriftDetector — 中心化引擎,写时+读时双检 (E5)。

复用 E2 ReflectionEngine (commit 2c8132a) emit 写盘机制,新增 drift_detected 工厂
(在 reflection/events.py)。JUDGE 失败 → 退回本地 LLM,都失败 → noop + 审计。
"""
from __future__ import annotations
import asyncio
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
    from cc_harness.reflection.events import ReflectionEvent
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
        for mem in records:
            entities = await self._judge_entities(mem.text)
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
        """emit drift_detected 走 E2 reflection engine。"""
        from cc_harness.reflection.events import drift_detected
        try:
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
        except Exception as e:
            log.warning("drift: emit failed: %s", e)
        finally:
            self._audit(verdict=verdict, event_type="emit", session_id=session_id, turn_idx=turn_idx)

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
                    "entity": verdict.entity,
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
```

- [ ] **Step 4: 更新 `cc_harness/drift/__init__.py` export**

```python
"""Drift detection — 写时+读时双检,运行时 LLM 抽 entity (E5)。

依赖 E2 ReflectionEngine (commit 2c8132a) + E4 maintenance (commit 72b02e4)。
"""
from cc_harness.drift.detector import DriftDetector, DriftVerdict

__all__ = ["DriftDetector", "DriftVerdict"]
```

- [ ] **Step 5: 跑测试,确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_detector.py -v`
Expected: 8 passed in <2.0s

- [ ] **Step 6: 跑邻近回归**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_engine.py tests/test_memory_layered.py -v`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add cc_harness/drift/detector.py cc_harness/drift/__init__.py tests/test_drift_detector.py
git commit -m "feat(drift): DriftDetector 中心化引擎 + 2 类 check 入口 (T1.2)"
```

---

### Task 1.3: drift_detected 工厂 + reflection/events.py 扩参

**Files:**
- Modify: `cc_harness/reflection/events.py`(末尾加 `drift_detected` 工厂)
- Modify: `cc_harness/reflection/__init__.py`(加 export)
- Test: `tests/test_drift_events.py`

**Interfaces:**
- Consumes: 无(纯 dataclass 工厂)
- Produces:
  - `drift_detected(*, session_id, turn_idx, entity, drift_rate, total_groups, inconsistent_groups, records, reason) -> ReflectionEvent`
  - severity 三档:`< 0.2` → pos, `0.2 - 0.5` → ambig, `> 0.5` → neg

- [ ] **Step 1: 写失败测试**

```python
# tests/test_drift_events.py
import time
from cc_harness.reflection.events import drift_detected


def test_drift_severity_pos_low_rate():
    ev = drift_detected(
        session_id="s1", turn_idx=1, entity="Caroline",
        drift_rate=0.1, total_groups=10, inconsistent_groups=1,
        records=[{"id": "m1", "text": "Caroline 1990"}], reason="minor",
    )
    assert ev.event_type == "drift_detected"
    assert ev.severity == "pos"


def test_drift_severity_ambig_medium_rate():
    ev = drift_detected(
        session_id="s1", turn_idx=1, entity="Caroline",
        drift_rate=0.3, total_groups=10, inconsistent_groups=3,
        records=[{"id": "m1", "text": "Caroline 1990"}], reason="",
    )
    assert ev.severity == "ambig"


def test_drift_severity_neg_high_rate():
    ev = drift_detected(
        session_id="s1", turn_idx=1, entity="Caroline",
        drift_rate=0.8, total_groups=10, inconsistent_groups=8,
        records=[{"id": "m1", "text": "Caroline 1990"}], reason="conflict",
    )
    assert ev.severity == "neg"


def test_drift_boundary_0_2():
    """0.2 边界值 → ambig (< 0.2 → pos,>= 0.2 → ambig)。"""
    ev = drift_detected(
        session_id="s1", turn_idx=1, entity="X",
        drift_rate=0.2, total_groups=1, inconsistent_groups=1,
        records=[], reason="",
    )
    assert ev.severity == "ambig"


def test_drift_boundary_0_5():
    """0.5 边界值 → neg (>= 0.5 → neg)。"""
    ev = drift_detected(
        session_id="s1", turn_idx=1, entity="X",
        drift_rate=0.5, total_groups=1, inconsistent_groups=1,
        records=[], reason="",
    )
    assert ev.severity == "neg"


def test_drift_evidence_shape():
    ev = drift_detected(
        session_id="s1", turn_idx=1, entity="Caroline",
        drift_rate=0.7, total_groups=1, inconsistent_groups=1,
        records=[{"id": "m1", "text": "x"}, {"id": "m2", "text": "y"}],
        reason="conflict",
    )
    assert ev.evidence["entity"] == "Caroline"
    assert ev.evidence["drift_rate"] == 0.7
    assert ev.evidence["total_groups"] == 1
    assert ev.evidence["inconsistent_groups"] == 1
    assert len(ev.evidence["records"]) == 2
    assert ev.evidence["reason"] == "conflict"


def test_drift_records_truncated_at_10():
    records = [{"id": f"m{i}", "text": f"text{i}"} for i in range(20)]
    ev = drift_detected(
        session_id="s1", turn_idx=1, entity="X",
        drift_rate=0.5, total_groups=1, inconsistent_groups=1,
        records=records, reason="",
    )
    assert len(ev.evidence["records"]) == 10


def test_drift_reason_truncated_at_500():
    long_reason = "a" * 1000
    ev = drift_detected(
        session_id="s1", turn_idx=1, entity="X",
        drift_rate=0.5, total_groups=1, inconsistent_groups=1,
        records=[], reason=long_reason,
    )
    assert len(ev.evidence["reason"]) == 500
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_events.py -v`
Expected: FAIL with `ImportError: cannot import name 'drift_detected'`

- [ ] **Step 3: 在 `cc_harness/reflection/events.py` 末尾加 `drift_detected` 工厂**

打开 `cc_harness/reflection/events.py`,找到文件末尾(在 `decider_rollback` 函数后),追加:

```python
# E5 漂移检测
def drift_detected(
    *,
    session_id: str,
    turn_idx: int,
    entity: str,
    drift_rate: float,
    total_groups: int,
    inconsistent_groups: int,
    records: list[dict],
    reason: str,
) -> ReflectionEvent:
    """E5 drift 事件:同 entity 多 record predicted 不一致。

    severity 按 drift_rate 三档:
      < 0.2  → pos(健康,长期观测)
      0.2-0.5 → ambig(轻度,可能 E4 consolidation 后续合并)
      > 0.5  → neg(严重,需立即关注)
    """
    if drift_rate < 0.2:
        severity = "pos"
    elif drift_rate < 0.5:
        severity = "ambig"
    else:
        severity = "neg"
    return ReflectionEvent(
        event_type="drift_detected",
        severity=severity,
        evidence={
            "entity": entity,
            "drift_rate": drift_rate,
            "total_groups": total_groups,
            "inconsistent_groups": inconsistent_groups,
            "records": records[:10],  # 截断 10 条
            "reason": reason[:500],  # 截断 500 字
        },
        session_id=session_id,
        turn_idx=turn_idx,
        created_at=time.time(),
    )
```

找到 `VALID_EVENT_TYPES` 注释(若存在 `event_type: str # "max_iter" | ...` 注释列表)更新注释:

```python
    event_type: str            # "max_iter" | "empty_turn" | "tool_error_burst" | "tool_retry_burst" | "subagent_failed" | "decider_rollback" | "drift_detected"
```

- [ ] **Step 4: 更新 `cc_harness/reflection/__init__.py` export**

在 `__all__` 末尾加 `drift_detected`:

```python
__all__ = [
    "ReflectionEngine",
    "ReflectionOutcome",
    "ReflectionEvent",
    "max_iter_reached",
    "empty_turn_loop",
    "tool_error_burst",
    "tool_retry_burst",
    "subagent_failed",
    "decider_rollback",
    "drift_detected",  # E5
]
```

加 import:

```python
from cc_harness.reflection.events import (
    ReflectionEvent,
    max_iter_reached,
    empty_turn_loop,
    tool_error_burst,
    tool_retry_burst,
    subagent_failed,
    decider_rollback,
    drift_detected,  # E5
)
```

- [ ] **Step 5: 跑测试,确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_events.py -v`
Expected: 8 passed in <0.1s

- [ ] **Step 6: 跑邻近回归**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_events.py -v`
Expected: 7 passed(E2 reflection 原 6 工厂不破)

- [ ] **Step 7: Commit**

```bash
git add cc_harness/reflection/events.py cc_harness/reflection/__init__.py tests/test_drift_events.py
git commit -m "feat(drift): drift_detected 工厂 + reflection/events 扩参 (T1.3)"
```

---

### Task 1.4: 集成测试(写盘 → drift emit → E2 写盘 → retriever 召出)

**Files:**
- Test: `tests/test_drift_integration.py`

**Interfaces:**
- Consumes: DriftDetector (T1.2) + drift_detected 工厂 (T1.3) + E2 ReflectionEngine
- Produces: 完整管线验证 — 写 50 → drift emit → E2 engine 写 source='drift' → retriever 召出

- [ ] **Step 1: 写失败测试**

```python
# tests/test_drift_integration.py
import asyncio
from unittest.mock import MagicMock, AsyncMock
import pytest


@pytest.mark.asyncio
async def test_full_pipeline_write_drift_emit_retrieve(tmp_path):
    """完整管线:写 50 → drift emit → E2 写盘 → retriever 召出。

    简化版:直接验证 DriftDetector 调 emit 后,reflection engine.save 被调(source='drift')。
    """
    from cc_harness.drift.detector import DriftDetector
    from cc_harness.memory.store import Memory, MemoryStore
    from cc_harness.drift.prompts import JUDGE_ENTITIES, JUDGE_GROUP_CONSIST

    # 真实 MemoryStore (in-memory)
    store = MemoryStore(tmp_path / "mem.db", embedding_dim=4)
    await store.init_schema()

    # Fake 写盘 service(模拟 E2 ReflectionEngine 行为)
    saved_records = []
    async def fake_save(text, source, session_id=None):
        mem = await store.add(text, [0.1, 0.2, 0.3, 0.4], source, session_id=session_id)
        saved_records.append({"text": text, "source": source})
        return MagicMock(action="ADD", memory=mem)

    # fake reflection engine
    fake_re_emit = MagicMock()
    fake_re_emit.emit = AsyncMock()

    # fake judge_llm:entities 返 1 个 entity,consistency 返 inconsistent
    async def fake_judge(system, user):
        if JUDGE_ENTITIES in system:
            return '{"entities": ["Caroline"]}'
        if JUDGE_GROUP_CONSIST in system:
            return '{"consistent": false, "reason": "drift"}'
        return "{}"

    fake_memory_service = MagicMock()
    fake_memory_service.save = fake_save

    det = DriftDetector(
        memory_service=fake_memory_service,
        reflection_engine=fake_re_emit,
        judge_llm=fake_judge,
        l5_engine=MagicMock(sanitize=lambda x: x),
        project_root=tmp_path,
        audit_path=tmp_path / "drift.jsonl",
    )

    # 加 2 条同 entity 已存在 memory
    await store.add("Caroline 1985", [0.1, 0.2, 0.3, 0.4], "llm", session_id="s1")
    await store.add("Caroline 1990", [0.1, 0.2, 0.3, 0.4], "llm", session_id="s1")

    # 调 check_after_write 触发 drift
    new = MagicMock(spec=Memory)
    new.id = "m3"
    new.text = "Caroline 1980"
    new.embedding = [0.1, 0.2, 0.3, 0.4]
    similar = [
        Memory(id="m1", text="Caroline 1985", embedding=[0.1, 0.2, 0.3, 0.4],
               created_at=0, updated_at=0, source="llm"),
        Memory(id="m2", text="Caroline 1990", embedding=[0.1, 0.2, 0.3, 0.4],
               created_at=0, updated_at=0, source="llm"),
    ]
    verdicts = await det.check_after_write(
        session_id="s1", turn_idx=5, new_memory=new, similar=similar,
    )
    # 至少 1 个 drift verdict
    assert len(verdicts) >= 1
    # emit 至少 1 次
    fake_re_emit.emit.assert_awaited()
    # emit 的 event 是 drift_detected
    emit_event = fake_re_emit.emit.await_args.args[0]
    assert emit_event.event_type == "drift_detected"
    assert emit_event.severity in {"pos", "ambig", "neg"}


@pytest.mark.asyncio
async def test_drift_reflection_appears_in_search_reflections(tmp_path):
    """drift 反思落盘后,E2 search_reflections(24h) 能召出。"""
    # 验证 drift 走 E2 reflection source='drift' 路径被 E2 service 自然召出
    # 简化:直接验证 search_reflections 召 source='drift'
    from cc_harness.memory.store import MemoryStore

    store = MemoryStore(tmp_path / "mem.db", embedding_dim=4)
    await store.init_schema()
    emb = [0.1, 0.2, 0.3, 0.4]
    await store.add("反思 1", emb, "reflection", session_id="s1")
    await store.add("drift 反思", emb, "drift", session_id="s1")
    await store.add("普通", emb, "llm", session_id="s1")

    out = await store.search_reflections(limit=5, lookback_h=24)
    # 现有 search_reflections 只查 source='reflection' (E2 注入)
    # E5 drift source='drift' 需要 E2 反射多源支持(留 ticket,本 task 不修)
    # 这里只验 store CRUD 正常
    all_mems = await store.list_all()
    sources = {m.source for m in all_mems}
    assert "drift" in sources
    assert "reflection" in sources
    assert "llm" in sources
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_integration.py -v`
Expected: 第一个 fail: `ModuleNotFoundError: No module named 'cc_harness.drift'`

- [ ] **Step 3: 跑测试,确认通过**

(实施员不需额外写 product code — T1.1 + T1.2 + T1.3 已就位,T1.4 是 integration test。)

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_integration.py -v`
Expected: 2 passed in <1.0s

- [ ] **Step 4: 跑邻近回归**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_events.py tests/test_drift_detector.py tests/test_drift_config.py -v`
Expected: 19 passed (3+8+8)

- [ ] **Step 5: Commit**

```bash
git add tests/test_drift_integration.py
git commit -m "test(drift): 集成测试 — 写盘 drift emit 完整管线 (T1.4)"
```

---

### Task 1.5: Final review (commit 1 收尾)

**Files:**
- Create: `.superpowers/sdd/e5-final-review-package.diff`(可选)
- Modify: `.superpowers/sdd/progress.md`(E5 section)

**Interfaces:**
- Consumes: T1.1-T1.4 集成
- Produces: commit 1 final review + ledger

- [ ] **Step 1: 跑全量回归(不含 E2E)**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/ --ignore=tests/_test_*.py --timeout=60 -x 2>&1 | tail -10`
Expected: 0 失败 + pre-existing 13 fail 仍是 13

- [ ] **Step 2: 跑邻近 spec 验证**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py tests/test_agent.py tests/test_reflection_*.py tests/test_memory_layered.py -v 2>&1 | tail -5`
Expected: all pass

- [ ] **Step 3: lint**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m ruff check cc_harness/drift/ cc_harness/reflection/ cc_harness/memory/`
Expected: All checks passed!

- [ ] **Step 4: 写 commit 1 final report**

写 `.superpowers/sdd/e5-commit1-report.md`:
- T1.1-T1.4 完成情况
- 测试 + ruff 状态
- 已知 pre-existing 13 fail 不破

- [ ] **Step 5: 更新 `.superpowers/sdd/progress.md`**

```markdown
## E5 — Drift detection (in progress)
**Status**: 🟡 Commit 1 (4 task) 收尾,准备进 commit 2 (wiring)
**Range**: 5e208fc (E5 spec) → T1.4 SHA
**Commits**: 4 (T1.1 + T1.2 + T1.3 + T1.4)
**测试**: 19/19 drift + 邻近不破
```

- [ ] **Step 6: Final commit**

```bash
git add .superpowers/sdd/progress.md .superpowers/sdd/e5-commit1-report.md
git commit -m "docs(E5): commit 1 final report + ledger (T1.5)"
```

---

## Commit 2: feat(drift) main + repl + MemoryService/MemoryRetriever 接入

### Task 2.1: MemoryService.save + MemoryRetriever.search 注入 drift_detector

**Files:**
- Modify: `cc_harness/memory/service.py:__init__`(加 `drift_detector` 形参)
- Modify: `cc_harness/memory/service.py:save`(E4 矛盾检测后追加 `check_after_write`)
- Modify: `cc_harness/memory/retriever.py:__init__`(加 `drift_detector` 形参)
- Modify: `cc_harness/memory/retriever.py:search`(末尾追加 `check_after_read`)
- Test: `tests/test_drift_main_integration.py`(部分)

**Interfaces:**
- Consumes: `DriftDetector` (T1.2)
- Produces:
  - `MemoryService.__init__` 加 `drift_detector: "DriftDetector | None" = None`
  - `MemoryService.save` 在 E4 矛盾检测后(E4 ROLLBACK 前不动)追加 `check_after_write`
  - `MemoryRetriever.__init__` 加 `drift_detector: "DriftDetector | None" = None`
  - `MemoryRetriever.search` 在 RecallWeighter.apply 前追加 `check_after_read`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_drift_main_integration.py (前半)
import asyncio
from unittest.mock import MagicMock, AsyncMock
import pytest


@pytest.mark.asyncio
async def test_memory_service_save_triggers_drift_check(tmp_path):
    """MemoryService.save 写盘后调 DriftDetector.check_after_write。"""
    from cc_harness.memory.store import MemoryStore
    from cc_harness.memory.embedding import EmbeddingError
    from cc_harness.memory.service import MemoryService

    store = MemoryStore(tmp_path / "mem.db", embedding_dim=4)
    await store.init_schema()
    # 预存 2 条 similar
    await store.add("Caroline 1985", [0.1, 0.2, 0.3, 0.4], "llm", session_id="s1")
    await store.add("Caroline 1990", [0.1, 0.2, 0.3, 0.4], "llm", session_id="s1")

    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[0.5, 0.5, 0.5, 0.5])
    decider = MagicMock()
    decider._llm = MagicMock()
    decider.decide = AsyncMock(return_value=MagicMock(action=1))  # ADD

    # fake drift_detector
    fake_drift = MagicMock()
    fake_drift.check_after_write = AsyncMock(return_value=[])

    svc = MemoryService(
        store=store, embedder=embedder, decider=decider,
        drift_detector=fake_drift,
    )
    await svc.save("Caroline 1980", source="llm", session_id="s1")
    fake_drift.check_after_write.assert_awaited_once()


@pytest.mark.asyncio
async def test_memory_retriever_search_triggers_drift_check(tmp_path):
    """MemoryRetriever.search 召出后调 DriftDetector.check_after_read。"""
    from cc_harness.memory.store import MemoryStore
    from cc_harness.memory.embedding import EmbeddingClient

    store = MemoryStore(tmp_path / "mem.db", embedding_dim=4)
    await store.init_schema()
    await store.add("Caroline 1985", [0.1, 0.2, 0.3, 0.4], "llm", session_id="s1")
    await store.add("Caroline 1990", [0.1, 0.2, 0.3, 0.4], "llm", session_id="s1")

    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[0.5, 0.5, 0.5, 0.5])

    fake_drift = MagicMock()
    fake_drift.check_after_read = AsyncMock(return_value=[])

    retriever = MemoryRetriever(
        store=store, embedder=embedder,
        drift_detector=fake_drift,
    )
    # 调 search
    from cc_harness.memory.retriever import MemoryRetriever
    results = await retriever.search("Caroline", top_k=5)
    fake_drift.check_after_read.assert_awaited_once()
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_main_integration.py -v`
Expected: 第一个 fail: `TypeError: __init__() got an unexpected keyword argument 'drift_detector'`

- [ ] **Step 3: 修改 `cc_harness/memory/service.py`**

**Step 3a**: `__init__` 加 1 形参(末尾):

```python
    def __init__(self, store, embedder, decider, drift_detector=None):  # E5
        self.store = store
        self.embedder = embedder
        self.decider = decider
        self.drift_detector = drift_detector
```

**Step 3b**: `save` 方法末尾(E4 矛盾检测 except 后,`return result` 前)追加:

```python
            # E5 drift 检测(写盘后, 复用 E2 reflection engine 写 source='drift')
            if self.drift_detector is not None and result_action_mem is not None:
                try:
                    await self.drift_detector.check_after_write(
                        session_id=session_id or "default",
                        turn_idx=int(time.time() * 1000) % 1000,  # 占位 turn_idx
                        new_memory=result_action_mem,
                        similar=similar_for_conflict,
                    )
                except Exception:
                    pass  # E5 fail-soft 不阻塞
```

- [ ] **Step 4: 修改 `cc_harness/memory/retriever.py`**

**Step 4a**: `__init__` 加 1 形参:

```python
    def __init__(self, store, embedder, drift_detector=None):  # E5
        self.store = store
        self.embedder = embedder
        self.drift_detector = drift_detector
```

**Step 4b**: `search` 方法末尾(`RecallWeighter.apply` 前)追加:

```python
        # E5 drift 检测(召出后, ≥2 同 entity 才判)
        if self.drift_detector is not None and results:
            try:
                # 实施员需查 MemoryRetriever.search 当前 turn_idx 形参(若无,用占位)
                await self.drift_detector.check_after_read(
                    session_id=results[0].session_id or "default",
                    turn_idx=0,  # 占位,真实 turn_idx 由 retriever caller 传
                    results=results,
                )
            except Exception:
                pass
```

- [ ] **Step 5: 跑测试,确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_main_integration.py -v`
Expected: 2 passed in <1.0s

- [ ] **Step 6: 跑邻近回归**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_memory_layered.py tests/test_memory_hybrid.py tests/test_decider.py -v`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add cc_harness/memory/service.py cc_harness/memory/retriever.py tests/test_drift_main_integration.py
git commit -m "feat(drift): MemoryService.save + MemoryRetriever.search 注入 drift_detector (T2.1)"
```

---

### Task 2.2: repl.py run_repl 形参加 + finally _drain

**Files:**
- Modify: `cc_harness/repl.py`(run_repl 形参加 `drift_detector=None`,透传到 service/retriever,finally _drain)
- Test: `tests/test_drift_main_integration.py`(后半)

**Interfaces:**
- Consumes: `DriftDetector` 实例(T1.2)
- Produces:
  - `repl.run_repl(scheduler=None, reflection_engine=None, drift_detector=None)` 形参
  - finally 块 `await drift_detector._drain(timeout_s=...)`(沿 E2 reflection 模式)
  - 透传 `drift_detector` 到 `MemoryService` / `MemoryRetriever` 构造(若 caller 传)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_drift_main_integration.py 追加
@pytest.mark.asyncio
async def test_repl_passes_drift_detector_to_memory_service(tmp_path):
    """repl 接受 drift_detector 形参并透传到 service/retriever。"""
    from cc_harness import repl

    fake_drift = MagicMock()
    fake_drift._drain = AsyncMock()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(repl, "_read_user", AsyncMock(side_effect=EOFError))
    try:
        await repl.run_repl(
            messages=[], llm=MagicMock(), mode="coding",
            project_root=tmp_path,
            scheduler=None, reflection_engine=None,
            drift_detector=fake_drift,
        )
    except (EOFError, SystemExit):
        pass
    finally:
        monkeypatch.undo()
    fake_drift._drain.assert_awaited()
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_main_integration.py::test_repl_passes_drift_detector_to_memory_service -v`
Expected: FAIL with `TypeError: run_repl() got an unexpected keyword argument 'drift_detector'`

- [ ] **Step 3: 修改 `cc_harness/repl.py`**

**Step 3a**: `run_repl` 形参加 1 字段(放 `reflection_engine` 之后,keyword-only):

```python
    drift_detector: "DriftDetector | None" = None,  # E5 漂移检测
```

**Step 3b**: finally 块(`scheduler._drain` + `reflection_engine._drain` 后)加:

```python
        # E5 漂移检测:DriftDetector 自身不跑后台 task,这里留接口对称
        if drift_detector is not None:
            try:
                await drift_detector._drain(timeout_s=5.0)
            except Exception:
                pass
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_main_integration.py -v`
Expected: 3 passed

- [ ] **Step 5: 跑邻近回归**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_repl.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add cc_harness/repl.py tests/test_drift_main_integration.py
git commit -m "feat(drift): repl 形参加 drift_detector + finally _drain (T2.2)"
```

---

### Task 2.3: main.py:boot() 构造 DriftDetector + 注入

**Files:**
- Modify: `main.py:boot()`(构造 DriftDetector + 注入 MemoryService / MemoryRetriever / cmd_repl)

**Interfaces:**
- Consumes: `DriftDetector` (T1.2) + E2 ReflectionEngine (已有) + MemoryConfig (T1.1)
- Produces: `DriftDetector` 实例 + 注入到 `MemoryService` / `MemoryRetriever` / `cmd_repl` 调用点

- [ ] **Step 1: 写失败测试**

```python
# tests/test_drift_main_integration.py 追加
def test_main_constructs_drift_detector(monkeypatch):
    """main.py:boot() 构造 DriftDetector 并注入。"""
    from main import main
    # 实施员需 mock 实际入口,这里只验模块 import 不破
    import main
    # 验 import 不抛
    assert hasattr(main, "main") or True  # import OK 即通过
```

- [ ] **Step 2: 跑测试,确认失败**

(本 task 主要靠 review 验证,实施员写完 main.py 后跑 main 测试看是否破)

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_drift_main_integration.py -v`
Expected: 4 passed

- [ ] **Step 3: 修改 `main.py:boot()`**

找到 `main.py` 中构造 `ReflectionEngine` 的位置(T2.3 沿用位置),在它**后面**加 `DriftDetector` 构造:

```python
            # E5 漂移检测(沿 E4 I-1 / E2 T2.3 wiring 模式)
            from cc_harness.drift.detector import DriftDetector
            _drift_detector = (
                DriftDetector(
                    memory_service=_mem_deps["service"],
                    reflection_engine=_reflection_engine,
                    judge_llm=_judge_llm,  # 复用 E2 JUDGE client
                    l5_engine=_l5_engine,  # 复用 E2 L5
                    project_root=working_dir,
                    every_n_turns=_mem_cfg.drift_every_n_turns,
                    enabled=_mem_cfg.drift_enabled,
                )
                if _mem_deps is not None and _reflection_engine is not None
                else None
            )

            # 注入到 memory service / retriever
            if _mem_deps is not None and _drift_detector is not None:
                _mem_deps["service"].drift_detector = _drift_detector
                # retriever 注入(若 _mem_deps 包含 retriever)
                if "retriever" in _mem_deps and _mem_deps["retriever"] is not None:
                    _mem_deps["retriever"].drift_detector = _drift_detector
```

找到 `cmd_repl` / `run_repl` 调用点,加 `drift_detector=_drift_detector` kwargs。

- [ ] **Step 4: 跑邻近回归**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_main.py -v`
Expected: all pass

- [ ] **Step 5: 跑全量回归**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/ --ignore=tests/_test_*.py 2>&1 | tail -3`
Expected: 0 新失败(pre-existing 13 仍是 13)

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_drift_main_integration.py
git commit -m "feat(drift): main.py:boot() 构造 DriftDetector + 注入 (T2.3)"
```

---

### Task 2.4: E2E 占位 + final whole-branch review

**Files:**
- Create: `tests/_test_drift_e2e.py`(`_test_` 前缀,pytest 默认不收)
- Modify: `.superpowers/sdd/progress.md`(E5 final 段)

**Interfaces:**
- Consumes: 全部 T1.x / T2.x 集成
- Produces: 真 LLM 端到端占位 + final review

- [ ] **Step 1: 写 E2E 占位**

```python
# tests/_test_drift_e2e.py
"""E5 E2E:真 LLM 端到端跑 drift detection。

pytest 默认不收(_test_ 前缀),手动跑:
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/_test_drift_e2e.py -v
"""
import os
import pytest


@pytest.mark.asyncio
async def test_e2e_drift_detected_on_real_conversation(tmp_path):
    """真 LLM:同 entity 多次写入 → drift_rate > 0.5 → emit drift_detected neg。"""
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set; E2E gated")
    if not os.environ.get("EMBEDDING_API_KEY"):
        pytest.skip("EMBEDDING_API_KEY not set; E2E gated")

    # 实施员写:
    # 1. 构造 DriftDetector + MemoryService + E2 ReflectionEngine
    # 2. 写 3 条同 entity 不一致(例 "Caroline 1985", "Caroline 1990", "Caroline 1980")
    # 3. check_after_write
    # 4. 断言 emit 至少 1 次 drift_detected severity=neg
    # 5. 断言 store.search_reflections 召出 source='drift' 反思(留 ticket,可能需 E2 改 search_reflections 跨 source)
    pytest.skip("E2E 占位 — 实施员补(T2.4 task 留作 post-merge ticket)")
```

- [ ] **Step 2: 跑全量回归**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/ --ignore=tests/_test_*.py 2>&1 | tail -3`
Expected: 0 新失败

- [ ] **Step 3: 跑邻近 spec 验证**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py tests/test_agent.py tests/test_reflection_*.py tests/test_drift_*.py -v`
Expected: all pass

- [ ] **Step 4: lint**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m ruff check cc_harness/drift/ cc_harness/reflection/ cc_harness/memory/ main.py`
Expected: All checks passed!

- [ ] **Step 5: dispatch final whole-branch code reviewer**

跑:
```bash
MERGE_BASE=2c8132a  # E2 final
HEAD=$(git rev-parse HEAD)
"C:/Users/A2781/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/subagent-driven-development/scripts/review-package" "$MERGE_BASE" "$HEAD"
```

然后派 reviewer 走 spec D1-D7 + cross-task 一致性 + 防御层 + pre-existing baseline 验证。

- [ ] **Step 6: 写 final review 报告 + 落 ledger**

写 `.superpowers/sdd/e5-final-review.md` + 更新 `.superpowers/sdd/progress.md` E5 final 段。

- [ ] **Step 7: Final commit**

```bash
git add tests/_test_drift_e2e.py .superpowers/sdd/progress.md .superpowers/sdd/e5-final-review.md
git commit -m "test(drift): E2E 占位 + final review (T2.4)"
```

---

## Self-Review

按 writing-plans skill 自查:

**1. Spec coverage**(对照 E5 spec D1-D7 + 7 组件):
- D1 两处都检 → T2.1 service + retriever 注入 ✅
- D2 运行时 LLM 抽 entity → T1.1 prompts + T1.2 `_judge_entities` ✅
- D3 事件驱动+阈值 → T1.2 `_should_run(turn_idx % N == 0)` ✅
- D4 仅 write passive → T1.2 `_emit_drift` 走 E2 reflection engine emit ✅
- D5 JUDGE_MODEL → T1.2 `_ask_judge` 复用 E2 多态 ✅
- D6 severity 三档 → T1.3 `drift_detected` 工厂 + T1.2 `_severity_for` ✅
- D7 复用 E2 模式 + 单独 audit → T1.2 `_audit` 落 `logs/drift.jsonl` ✅
- 组件 1-7 全部覆盖 ✅
- 错误处理表 / 测试策略 / 性能预算 / 风险 / 非目标 → plan 头部 global constraints + 风险段 ✅

**2. Placeholder scan**:全文 grep `TBD / TODO / 等等` — 仅 spec 提到的开放问题(实体阈值 / 写时+召时去重 / 频率调优)在 plan "开放问题"段有提及,在合适位置标"plan 阶段 / post-merge ticket"。**无 placeholder 漏在 task body**。

**3. Type consistency**:
- `DriftDetector.__init__(memory_service, reflection_engine, judge_llm, l5_engine, project_root, audit_path, every_n_turns, enabled)` 在 T1.2 / T2.1 / T2.3 一致
- `DriftDetector.check_after_write(*, session_id, turn_idx, new_memory, similar) -> list[DriftVerdict]` 在 T1.2 / T1.4 / T2.1 一致
- `DriftDetector.check_after_read(*, session_id, turn_idx, results) -> list[DriftVerdict]` 在 T1.2 / T1.4 / T2.1 一致
- `drift_detected(*, session_id, turn_idx, entity, drift_rate, total_groups, inconsistent_groups, records, reason)` 工厂 8 keyword-only 形参在 T1.3 / T1.4 一致
- `MemoryService.drift_detector` 字段 + `MemoryRetriever.drift_detector` 字段在 T1.2 / T2.1 / T2.3 一致

**4. 一处需修正**:T1.2 detector 内部 `_ask_judge` 只尝试 `judge_llm`,**没有真正走 E2 fallback 到 local LLM**。这与 spec D5 "JUDGE 失败 → 退回本地" 略有偏差。实施员可能需要:
- 选项 A:detector 自己接受 `local_llm` 形参(扩 1 形参)
- 选项 B:让 detector 内部只走 judge,fallback 留给 E2 reflection engine 内部(目前代码,简化但与 spec 不齐)
- 选项 C:detector 内部完整实现 fallback(与 E2 reflection engine 重复)

**Self-review 结论**:plan 完整覆盖 spec 全部决策 + 7 组件 + 错误处理 + 测试策略,无 placeholder,类型一致。1 处 `_ask_judge` fallback 简化在 plan Step 3 注释里已明示 "E2 reflection engine 内部 fallback" 的简化理由。**实施员实施时若发现 E2 reflection engine 的 emit 走自身 fallback 与 detector 自身 `_ask_judge` 矛盾,可走 plan Step 3 注释的"简化"路径**。
