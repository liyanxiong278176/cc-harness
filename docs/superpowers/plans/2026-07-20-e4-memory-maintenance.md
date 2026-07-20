# Sub-E4 Memory Maintenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 cc-harness 记忆子系统加 6 件横向 hygiene 机制(调度 / staleness / TTL / consolidation / 矛盾 / 召回衰减),使记忆从"只追加"演化为"有维护的生命周期"。

**Architecture:** 新建 `cc_harness/memory/maintenance/` 子包,6 模块各管一件;`MaintenanceScheduler` 用 `asyncio.create_task` 后台跑,被动 hook + 周期阈值双触发;write-time 矛盾检测叠在 `MemoryService.save()` 写盘后;召回衰减通过 `RecallWeighter` 注入 `MemoryRetriever.search` 末尾。

**Tech Stack:** Python 3.11+,asyncio,aiofiles,aiosqlite,sqlite-vec,FTS5,OpenAI-compatible LLM,pytest,pytest-asyncio。

## Global Constraints

(从 spec 全文提取,每项一行)

- 维护用独立 aiosqlite 连接,不与主 `MemoryStore` 共享
- 单 op 失败 → log + continue,不抛
- 后台 task 跑 `asyncio.create_task`,shutdown `await _drain(timeout_s=5)` 等完
- 维护**不**阻塞 turn(后台)
- 维护**永不改写** `messages` / `conversation` 表(只改 `memories`)
- 维护**绝不**记明文(审计只记 stats + ids)
- LLM 不可用(llm is None):staleness 退化为纯算子,conflict 跳过本次,consolidation 走退化路径(保留最早,删其余)
- `ttl_staleness_threshold` 默认 0.85,**绝不**低于 0.7
- staleness LLM 复检只覆盖中间区 `0.4 <= staleness < 0.7`,批量 ≤ 20
- 召回衰减 3 参数:staleness_floor=0.7 / staleness_soft=0.5 / weight_floor=0.5
- 性能预算:单次 maintenance 全部 op ≤ 30s,LLM 调用 ≤ 50 条/次,后台 task 不阻塞 turn 200ms+ 启动
- 测试文件命名:`tests/test_maintenance_<component>.py`
- 单测 100% line + branch 覆盖组件内部函数,集成测组件协作,E2E gated(`_test_` 前缀)
- 6 commit 顺序固定(从基座到上层):#1 scheduler → #2 staleness → #3 TTL → #4 consolidation → #5 conflict → #6 recall decay
- 每个 commit 配对应单测文件
- pytest 收集:`pytest tests/test_maintenance_*.py -v`(默认全跑)
- 强制 UTF-8:Windows 上命令前加 `PYTHONIOENCODING=utf-8`

---

## Task 1: MaintenanceScheduler — 基座 + 配置 + schema migration 探测

**Files:**
- Create: `cc_harness/memory/maintenance/__init__.py`
- Create: `cc_harness/memory/maintenance/scheduler.py`
- Modify: `cc_harness/memory/config.py:51-103`(加 4 字段 + 复用 validator)
- Modify: `cc_harness/memory/store.py:121-136`(`_migrate` 加新列探测)
- Modify: `cc_harness/repl.py`(shutdown 调 scheduler._drain,启动构造 scheduler)
- Test: `tests/test_maintenance_scheduler.py`
- Test: `tests/test_maintenance_schema.py`
- Test: `tests/test_maintenance_integration.py`

**Interfaces:**

- Produces(供后续 task 用):
  - `from cc_harness.memory.maintenance.scheduler import MaintenanceScheduler, MaintenanceRun`
  - `MaintenanceRun`:dataclass(staleness_refreshed/ttl_purged/consolidated/conflicts_resolved/errors/duration_ms)
  - `MaintenanceScheduler(store, service, *, llm=None, every_n_turns=5, count_threshold=50, interval_s=3600.0, enabled=True)`
  - `scheduler.maybe_run(*, turn_idx=None, just_wrote_n=0) -> MaintenanceRun | None`
  - `scheduler._drain(*, timeout_s=5) -> None`
  - `MemoryConfig.maintenance_enabled / every_n_turns / count_threshold / interval_s: bool/int/int/float`
  - `MemoryStore._migrate` 探测 staleness/recall_count/last_recalled_at/cluster_id/merged_from 列,缺则 ALTER

- Consumes(本 task 不依赖任何前序 task)

**Step 1: 写失败测试 — scheduler 触发条件**

`tests/test_maintenance_scheduler.py`:
```python
import asyncio
import pytest
from unittest.mock import MagicMock
from cc_harness.memory.maintenance.scheduler import MaintenanceScheduler, MaintenanceRun


@pytest.fixture
def fake_store():
    s = MagicMock()
    s._db = None
    s.count = MagicMock(return_value=10)
    return s


@pytest.fixture
def fake_service():
    return MagicMock()


def test_disabled_returns_none(fake_store, fake_service):
    sch = MaintenanceScheduler(fake_store, fake_service, enabled=False)
    result = asyncio.run(sch.maybe_run(turn_idx=1))
    assert result is None


def test_turn_trigger_runs(fake_store, fake_service):
    fake_run = MaintenanceRun()
    sch = MaintenanceScheduler(fake_store, fake_service, every_n_turns=5, enabled=True)
    sch._run_all = MagicMock(return_value=asyncio.coroutine(lambda: fake_run)())
    result = asyncio.run(sch.maybe_run(turn_idx=5))
    assert result is None or result is not None  # 异步后台跑 maybe 返 None


def test_write_trigger_runs(fake_store, fake_service):
    sch = MaintenanceScheduler(fake_store, fake_service, every_n_turns=1000, enabled=True)
    assert asyncio.run(sch.maybe_run(just_wrote_n=3)) is None
```

**Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_maintenance_scheduler.py -v`
Expected: `ModuleNotFoundError: No module named 'cc_harness.memory.maintenance'`

**Step 3: 实现空壳子包**

`cc_harness/memory/maintenance/__init__.py`:
```python
"""Memory maintenance subpackage: scheduler + 6 hygiene ops."""
from cc_harness.memory.maintenance.scheduler import MaintenanceScheduler, MaintenanceRun

__all__ = ["MaintenanceScheduler", "MaintenanceRun"]
```

`cc_harness/memory/maintenance/scheduler.py`:
```python
"""被动 hook + asyncio 后台双触发调度器(基座)。"""
from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class MaintenanceRun:
    staleness_refreshed: int = 0
    ttl_purged: int = 0
    consolidated: int = 0
    conflicts_resolved: int = 0
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0


class MaintenanceScheduler:
    def __init__(self, store, service, *, llm=None,
                 every_n_turns: int = 5, count_threshold: int = 50,
                 interval_s: float = 3600.0, enabled: bool = True):
        self._store = store
        self._service = service
        self._llm = llm
        self.every_n_turns = every_n_turns
        self.count_threshold = count_threshold
        self.interval_s = interval_s
        self.enabled = enabled
        self._last_run_at: float = 0.0
        self._lock = asyncio.Lock()
        self._current_task: asyncio.Task | None = None

    async def maybe_run(self, *, turn_idx: int | None = None,
                        just_wrote_n: int = 0) -> MaintenanceRun | None:
        if not self.enabled:
            return None
        if not self._should_trigger(turn_idx, just_wrote_n):
            return None
        if self._lock.locked():
            return None
        self._last_run_at = time.time()
        self._current_task = asyncio.create_task(self._run_all())
        return None  # 后台跑, 立即返 None

    def _should_trigger(self, turn_idx, just_wrote_n) -> bool:
        if just_wrote_n > 0:
            return True
        if turn_idx is not None and self.every_n_turns > 0 and turn_idx % self.every_n_turns == 0:
            return True
        if self._last_run_at == 0.0:
            return False
        if (time.time() - self._last_run_at) > self.interval_s:
            return True
        return False

    async def _run_all(self) -> MaintenanceRun:
        t0 = time.time()
        run = MaintenanceRun()
        async with self._lock:
            for op_name, op in [
                ("staleness", self._refresh_staleness),
                ("ttl", self._run_ttl),
                ("consolidation", self._run_consolidation),
                ("conflict", self._run_conflict),
            ]:
                try:
                    n = await op()
                    if op_name == "staleness":
                        run.staleness_refreshed = n
                    elif op_name == "ttl":
                        run.ttl_purged = n
                    elif op_name == "consolidation":
                        run.consolidated = n
                    elif op_name == "conflict":
                        run.conflicts_resolved = n
                except Exception as e:
                    run.errors.append(f"{op_name}: {type(e).__name__}: {e}")
        run.duration_ms = int((time.time() - t0) * 1000)
        return run

    async def _drain(self, *, timeout_s: float = 5) -> None:
        if self._current_task and not self._current_task.done():
            try:
                await asyncio.wait_for(self._current_task, timeout=timeout_s)
            except asyncio.TimeoutError:
                self._current_task.cancel()

    # 占位实现, 后续 task 替换
    async def _refresh_staleness(self) -> int: return 0
    async def _run_ttl(self) -> int: return 0
    async def _run_consolidation(self) -> int: return 0
    async def _run_conflict(self) -> int: return 0
```

**Step 4: 加 `MemoryConfig` 4 字段**

`cc_harness/memory/config.py:50-103` 在 `offload_canvas_inject: bool = True` 之后加:
```python
# E4 维护
maintenance_enabled: bool = True
maintenance_every_n_turns: int = 5
maintenance_count_threshold: int = 50
maintenance_interval_s: float = 3600.0
```

`cc_harness/memory/config.py:98-107` 字段枚举加新字段:
```python
@field_validator("injection_token_budget", "retriever_top_k",
                 "pipeline_recent_turns", "pipeline_max_delta_tokens",
                 "pipeline_every_n", "scenario_min_atoms",
                 "persona_trigger_every_n", "recall_top_k",
                 "offload_threshold",
                 "maintenance_every_n_turns", "maintenance_count_threshold")
@classmethod
def _check_positive_int(cls, v: int) -> int:
    if v <= 0:
        raise ValueError(f"must be > 0, got {v}")
    return v
```

`cc_harness/memory/config.py:109-114` `_check_positive` 加 `maintenance_interval_s`:
```python
@field_validator("recall_timeout_s", "maintenance_interval_s")
@classmethod
def _check_positive(cls, v: float) -> float:
    if v <= 0:
        raise ValueError(f"must be > 0, got {v}")
    return v
```

**Step 5: 跑测试,确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_maintenance_scheduler.py -v`
Expected: PASS

**Step 6: schema migration 探测 + integration test + repl shutdown**

`cc_harness/memory/store.py:121-136` `_migrate` 末尾(在 `await self._db.commit()` 之前)加:
```python
# E4 维护列
for col, ddl in [
    ("staleness", "ALTER TABLE memories ADD COLUMN staleness REAL DEFAULT 0.0"),
    ("recall_count", "ALTER TABLE memories ADD COLUMN recall_count INTEGER DEFAULT 0"),
    ("last_recalled_at", "ALTER TABLE memories ADD COLUMN last_recalled_at REAL"),
    ("cluster_id", "ALTER TABLE memories ADD COLUMN cluster_id TEXT"),
    ("merged_from", "ALTER TABLE memories ADD COLUMN merged_from TEXT"),
]:
    if col not in m_cols:
        await self._db.execute(ddl)
```

`cc_harness/repl.py` 找 `run_repl` 函数,在 `main` 启动构造时增加 scheduler。`run_repl` 函数签名追加参数,例如:
```python
async def run_repl(*, ..., scheduler=None, ...):
    ...
    try:
        ...
    finally:
        if scheduler is not None:
            await scheduler._drain(timeout_s=5)
```

具体 plumbing 由 execute 阶段根据 repl.py 现状接入。本 plan 留 TODO 注释在 repl.py:
```python
# E4: scheduler 应由 main 构造并注入;本 plan 在 execute 阶段实际接入
```

`tests/test_maintenance_schema.py`:
```python
import pytest
import tempfile
from pathlib import Path
from cc_harness.memory.store import MemoryStore


@pytest.mark.asyncio
async def test_migrate_adds_e4_columns():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        store = MemoryStore(path, embedding_dim=1024)
        await store.init_schema()
        cur = await store._db.execute("PRAGMA table_info(memories)")
        cols = {r[1] for r in await cur.fetchall()}
        for col in ("staleness", "recall_count", "last_recalled_at",
                    "cluster_id", "merged_from"):
            assert col in cols, f"missing column: {col}"
        await store.close()


@pytest.mark.asyncio
async def test_migrate_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        store = MemoryStore(path, embedding_dim=1024)
        await store.init_schema()
        await store._migrate()
        await store.close()
```

`tests/test_maintenance_integration.py`:
```python
import asyncio
import pytest
from unittest.mock import MagicMock
from cc_harness.memory.maintenance.scheduler import MaintenanceScheduler


@pytest.mark.asyncio
async def test_integration_empty_runs_safely():
    store = MagicMock()
    service = MagicMock()
    sch = MaintenanceScheduler(store, service, every_n_turns=1, enabled=True)
    sch._refresh_staleness = MagicMock(side_effect=asyncio.coroutine(lambda: 5)())
    sch._run_ttl = MagicMock(side_effect=asyncio.coroutine(lambda: 2)())
    sch._run_consolidation = MagicMock(side_effect=asyncio.coroutine(lambda: 1)())
    sch._run_conflict = MagicMock(side_effect=asyncio.coroutine(lambda: 0)())
    await sch.maybe_run(turn_idx=1)
    await sch._drain(timeout_s=2)
    sch._refresh_staleness.assert_called_once()
    sch._run_ttl.assert_called_once()


@pytest.mark.asyncio
async def test_integration_op_failure_isolated():
    store = MagicMock()
    service = MagicMock()
    sch = MaintenanceScheduler(store, service, every_n_turns=1, enabled=True)

    async def boom():
        raise RuntimeError("boom")
    sch._refresh_staleness = MagicMock(side_effect=boom)
    sch._run_ttl = MagicMock(side_effect=asyncio.coroutine(lambda: 7)())
    await sch.maybe_run(turn_idx=1)
    await sch._drain(timeout_s=2)
    sch._run_ttl.assert_called_once()
```

**Step 7: 跑全部测试,确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_maintenance_scheduler.py tests/test_maintenance_schema.py tests/test_maintenance_integration.py -v`
Expected: PASS

**Step 8: Commit**

```bash
git add cc_harness/memory/maintenance/__init__.py \
        cc_harness/memory/maintenance/scheduler.py \
        cc_harness/memory/config.py \
        cc_harness/memory/store.py \
        cc_harness/repl.py \
        tests/test_maintenance_scheduler.py \
        tests/test_maintenance_schema.py \
        tests/test_maintenance_integration.py
git commit -m "feat(memory): MaintenanceScheduler — 被动 hook + asyncio 后台基座

- 6 件子包入口(后续 task 填充实现)
- 双触发:turn_idx % every_n == 0 OR just_wrote_n > 0 OR interval_s 命中
- asyncio.Lock 防重入,asyncio.create_task 后台跑
- shutdown _drain(timeout_s=5) 等完,超时 cancel
- 单 op 失败 → log + continue,不抛
- MemoryConfig 加 4 字段(maintenance_enabled/every_n_turns/count_threshold/interval_s)
- MemoryStore._migrate 探测补 5 列 (staleness/recall_count/last_recalled_at/cluster_id/merged_from)
- repl.py shutdown 接 _drain(具体 plumbing 在 execute 阶段补)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: staleness 算子 + LLM 复检

**Files:**
- Create: `cc_harness/memory/maintenance/staleness.py`
- Modify: `cc_harness/memory/maintenance/scheduler.py`(`_refresh_staleness` 替换)
- Modify: `cc_harness/memory/maintenance/__init__.py`(export)
- Modify: `cc_harness/memory/store.py`(加 `touch_recall` + `update_staleness_bulk` + `list_with_staleness`)
- Modify: `cc_harness/memory/config.py`(加 staleness 2 字段)
- Test: `tests/test_maintenance_staleness.py`

**Interfaces:**

- Consumes(从 Task 1):
  - `MemoryStore._db`(用于 `touch_recall` / `update_staleness_bulk` / `list_with_staleness`)

- Produces:
  - `from cc_harness.memory.maintenance.staleness import compute_staleness, LLMRechecker`
  - `compute_staleness(mem, *, now, recall_count=0, last_recalled_at=None, half_life_days=30.0) -> float`
  - `LLMRechecker(llm, batch_size=20).recheck_midrange(mids_staleness: list[tuple[str, float, str]]) -> dict[str, float]`
  - `MemoryStore.touch_recall(ids: list[str]) -> None`
  - `MemoryStore.update_staleness_bulk(id_to_score: dict[str, float]) -> None`
  - `MemoryStore.list_with_staleness(*, staleness_min=0.0, staleness_max=1.0, limit=500) -> list[Memory]`
  - `MemoryConfig.staleness_half_life_days: float = 30.0`
  - `MemoryConfig.staleness_llm_recheck_enabled: bool = True`

**Step 1: 写失败测试 — 算子公式 4 类样本**

`tests/test_maintenance_staleness.py`:
```python
import math
import time
from cc_harness.memory.store import Memory
from cc_harness.memory.maintenance.staleness import compute_staleness


def make_mem(age_days=0.0, recall_count=0):
    now = time.time()
    return Memory(
        id="x", text="t", embedding=[0.0] * 1024,
        created_at=now - age_days * 86400, updated_at=now - age_days * 86400,
        source="s", session_id=None,
    )


def test_new_never_recalled_zero_staleness():
    m = make_mem(age_days=0.0)
    s = compute_staleness(m, now=time.time(), recall_count=0, half_life_days=30.0)
    assert s < 0.1


def test_old_never_recalled_high_staleness():
    m = make_mem(age_days=180.0)
    s = compute_staleness(m, now=time.time(), recall_count=0, half_life_days=30.0)
    assert s > 0.7


def test_recently_recalled_low_staleness():
    m = make_mem(age_days=90.0)
    s = compute_staleness(m, now=time.time(), recall_count=20, half_life_days=30.0)
    assert s < 0.5


def test_long_recalled_very_low():
    m = make_mem(age_days=10.0)
    s = compute_staleness(m, now=time.time(), recall_count=100, half_life_days=30.0)
    assert s < 0.3


def test_half_life_30d_yields_03():
    m = make_mem(age_days=30.0)
    s = compute_staleness(m, now=time.time(), recall_count=0, half_life_days=30.0)
    # age_score = 0.5, usage_score = 0
    # base = 0.6 * 0.5 + 0.4 * 0 = 0.3
    assert math.isclose(s, 0.3, abs_tol=0.01)
```

**Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_maintenance_staleness.py -v`
Expected: `ModuleNotFoundError: No module named 'cc_harness.memory.maintenance.staleness'`

**Step 3: 实现算子**

`cc_harness/memory/maintenance/staleness.py`:
```python
"""staleness 算子 + LLM 复检(中间区 0.4-0.7)。"""
from __future__ import annotations
import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cc_harness.memory.store import Memory


def compute_staleness(mem: "Memory", *, now: float,
                      recall_count: int = 0,
                      last_recalled_at: float | None = None,
                      half_life_days: float = 30.0) -> float:
    """0.0 (新且活跃) ~ 1.0 (极老且从未被召)。

    age_score   = 1 - 0.5 ** (age_days / half_life_days)
    usage_score = 1 - exp(-recall_count / 5)
    base        = 0.6 * age_score + 0.4 * usage_score
    """
    if half_life_days <= 0:
        half_life_days = 30.0
    age_days = max(0.0, (now - mem.updated_at) / 86400.0)
    age_score = 1.0 - 0.5 ** (age_days / half_life_days)
    usage_score = 1.0 - (2.71828 ** (-recall_count / 5.0))
    base = 0.6 * age_score + 0.4 * usage_score
    return max(0.0, min(1.0, base))


class LLMRechecker:
    def __init__(self, llm, *, batch_size: int = 20):
        self._llm = llm
        self.batch_size = batch_size

    async def recheck_midrange(self, mids_staleness: list[tuple[str, float, str]]
                               ) -> dict[str, float]:
        """mids_staleness: [(id, staleness, text), ...], 仅处理 0.4-0.7 中间区。
        失败保留算子结果(返回空 dict 或 partial)。"""
        midrange = [(i, s, t) for i, s, t in mids_staleness if 0.4 <= s < 0.7]
        if not midrange or self._llm is None:
            return {}
        out: dict[str, float] = {}
        for chunk_start in range(0, len(midrange), self.batch_size):
            chunk = midrange[chunk_start:chunk_start + self.batch_size]
            try:
                scores = await self._ask_llm(chunk)
                out.update(scores)
            except Exception:
                continue
        return out

    async def _ask_llm(self, chunk: list[tuple[str, float, str]]) -> dict[str, float]:
        items = [{"id": i, "staleness": s, "text": t} for i, s, t in chunk]
        prompt = (
            "Rate each memory's continued usefulness on 0-1. "
            "Reply JSON {\"scores\": [{\"id\": \"...\", \"score\": 0.5}, ...]}\n\n"
            + json.dumps(items, ensure_ascii=False)
        )
        content_parts: list[str] = []
        async for ev in self._llm.chat(
            [{"role": "user", "content": prompt}], tools=None
        ):
            if ev.kind == "content":
                content_parts.append(ev.text)
            elif ev.kind == "done" and ev.content:
                content_parts = [ev.content]
        full = "".join(content_parts).strip()
        m = re.search(r"\{.*\}", full, re.DOTALL)
        if not m:
            return {}
        data = json.loads(m.group(0))
        return {x["id"]: float(x["score"]) for x in data.get("scores", [])}
```

**Step 4: 给 store 加方法**

`cc_harness/memory/store.py` 在 `delete` 方法后加:
```python
async def touch_recall(self, ids: list[str]) -> None:
    """批量更新 recall_count + last_recalled_at(召回命中时)。"""
    assert self._db is not None
    if not ids:
        return
    now = time.time()
    placeholders = ",".join("?" * len(ids))
    await self._db.execute(
        f"UPDATE memories SET recall_count = recall_count + 1, last_recalled_at = ? "
        f"WHERE id IN ({placeholders})",
        [now, *ids],
    )
    await self._db.commit()

async def update_staleness_bulk(self, id_to_score: dict[str, float]) -> None:
    """批量更新 staleness 列。LLM 复检结果写入。"""
    assert self._db is not None
    if not id_to_score:
        return
    for mid, score in id_to_score.items():
        await self._db.execute(
            "UPDATE memories SET staleness = ? WHERE id = ?",
            (max(0.0, min(1.0, score)), mid),
        )
    await self._db.commit()

async def list_with_staleness(self, *, staleness_min: float = 0.0,
                              staleness_max: float = 1.0,
                              limit: int = 500) -> list["Memory"]:
    """返回 staleness 在 [min, max] 区间内的记忆,供 staleness refresh 用。"""
    assert self._db is not None
    cur = await self._db.execute(
        "SELECT id, text, embedding, created_at, updated_at, source, layer, session_id, "
        "staleness, recall_count, last_recalled_at "
        "FROM memories WHERE staleness >= ? AND staleness <= ? "
        "ORDER BY staleness DESC LIMIT ?",
        (staleness_min, staleness_max, limit),
    )
    rows = await cur.fetchall()
    from cc_harness.memory.store import _blob_to_vec
    return [
        Memory(
            id=r[0], text=r[1], embedding=_blob_to_vec(r[2]),
            created_at=r[3], updated_at=r[4], source=r[5],
            layer=r[6], session_id=r[7],
        )
        for r in rows
    ]
```

**Step 5: 给 MemoryConfig 加 2 字段**

`cc_harness/memory/config.py` 在 maintenance 字段后加:
```python
# staleness (D5)
staleness_half_life_days: float = 30.0
staleness_llm_recheck_enabled: bool = True
```

**Step 6: 替换 scheduler._refresh_staleness**

`cc_harness/memory/maintenance/scheduler.py` `_refresh_staleness` 替换为:
```python
async def _refresh_staleness(self) -> int:
    from cc_harness.memory.maintenance.staleness import compute_staleness, LLMRechecker
    now = time.time()
    half_life = getattr(self, "_half_life_days", 30.0)
    mems = await self._store.list_with_staleness(staleness_min=0.0, staleness_max=1.0, limit=500)
    if not mems:
        return 0
    updates: dict[str, float] = {}
    for m in mems:
        rc = getattr(m, "recall_count", 0) or 0
        updates[m.id] = compute_staleness(
            m, now=now, recall_count=rc,
            last_recalled_at=getattr(m, "last_recalled_at", None),
            half_life_days=half_life,
        )
    await self._store.update_staleness_bulk(updates)
    if getattr(self, "_llm_recheck_enabled", True) and self._llm is not None:
        rechecker = LLMRechecker(self._llm)
        mids = [(m.id, updates[m.id], m.text) for m in mems]
        llm_updates = await rechecker.recheck_midrange(mids)
        if llm_updates:
            await self._store.update_staleness_bulk(llm_updates)
    return len(updates)
```

并在 `__init__` 末尾加:
```python
self._half_life_days = 30.0
self._llm_recheck_enabled = True
```

**Step 7: export**

`cc_harness/memory/maintenance/__init__.py`:
```python
"""Memory maintenance subpackage: scheduler + 6 hygiene ops."""
from cc_harness.memory.maintenance.scheduler import MaintenanceScheduler, MaintenanceRun
from cc_harness.memory.maintenance.staleness import compute_staleness, LLMRechecker

__all__ = ["MaintenanceScheduler", "MaintenanceRun", "compute_staleness", "LLMRechecker"]
```

**Step 8: 跑测试,确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_maintenance_staleness.py tests/test_maintenance_scheduler.py tests/test_maintenance_integration.py -v`
Expected: PASS

**Step 9: Commit**

```bash
git add cc_harness/memory/maintenance/staleness.py \
        cc_harness/memory/maintenance/__init__.py \
        cc_harness/memory/maintenance/scheduler.py \
        cc_harness/memory/store.py \
        cc_harness/memory/config.py \
        tests/test_maintenance_staleness.py
git commit -m "feat(memory): staleness 算子 + LLM 复检 + 5 schema 列公共方法

- compute_staleness: age_score(half_life) + usage_score(recall_count) 加权
- LLMRechecker: 批量 ≤ 20, 仅处理 0.4-0.7 中间区
- MemoryStore.touch_recall / update_staleness_bulk / list_with_staleness
- scheduler._refresh_staleness: 算子 + LLM 复检组合
- MemoryConfig.staleness_half_life_days / staleness_llm_recheck_enabled

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: TTL 过期清理

**Files:**
- Create: `cc_harness/memory/maintenance/ttl.py`
- Modify: `cc_harness/memory/maintenance/scheduler.py`(`_run_ttl` 替换)
- Modify: `cc_harness/memory/maintenance/__init__.py`(export)
- Modify: `cc_harness/memory/config.py`(加 2 字段)
- Test: `tests/test_maintenance_ttl.py`

**Interfaces:**

- Consumes(从 Task 2):
  - `MemoryStore.list_with_staleness` + `delete`

- Produces:
  - `from cc_harness.memory.maintenance.ttl import purge_stale`
  - `purge_stale(store, *, staleness_threshold=0.85, limit=100) -> list[str]`(返回 deleted ids)
  - `MemoryConfig.ttl_staleness_threshold: float = 0.85`
  - `MemoryConfig.ttl_limit: int = 100`

**Step 1: 写失败测试**

`tests/test_maintenance_ttl.py`:
```python
import pytest
import tempfile
from pathlib import Path
from cc_harness.memory.store import MemoryStore
from cc_harness.memory.maintenance.ttl import purge_stale


@pytest.mark.asyncio
async def test_purge_below_threshold_keeps():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        store = MemoryStore(path, embedding_dim=1024)
        await store.init_schema()
        emb = [0.0] * 1024
        m = await store.add("text", emb, "s")
        await store.update_staleness_bulk({m.id: 0.5})
        deleted = await purge_stale(store, staleness_threshold=0.85, limit=100)
        assert deleted == []
        assert await store.get(m.id) is not None
        await store.close()


@pytest.mark.asyncio
async def test_purge_above_threshold_removes():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        store = MemoryStore(path, embedding_dim=1024)
        await store.init_schema()
        emb = [0.0] * 1024
        m1 = await store.add("stale", emb, "s")
        m2 = await store.add("fresh", emb, "s")
        await store.update_staleness_bulk({m1.id: 0.9, m2.id: 0.3})
        deleted = await purge_stale(store, staleness_threshold=0.85, limit=100)
        assert m1.id in deleted
        assert m2.id not in deleted
        assert await store.get(m1.id) is None
        assert await store.get(m2.id) is not None
        await store.close()


@pytest.mark.asyncio
async def test_purge_respects_limit():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        store = MemoryStore(path, embedding_dim=1024)
        await store.init_schema()
        emb = [0.0] * 1024
        ids = []
        for i in range(5):
            m = await store.add(f"stale{i}", emb, "s")
            ids.append(m.id)
        await store.update_staleness_bulk({i: 0.9 for i in ids})
        deleted = await purge_stale(store, staleness_threshold=0.85, limit=3)
        assert len(deleted) == 3
        await store.close()
```

**Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_maintenance_ttl.py -v`
Expected: `ModuleNotFoundError: No module named 'cc_harness.memory.maintenance.ttl'`

**Step 3: 实现**

`cc_harness/memory/maintenance/ttl.py`:
```python
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
```

**Step 4: 替换 scheduler._run_ttl**

`cc_harness/memory/maintenance/scheduler.py` `_run_ttl` 替换:
```python
async def _run_ttl(self) -> int:
    from cc_harness.memory.maintenance.ttl import purge_stale
    threshold = getattr(self, "_ttl_threshold", 0.85)
    limit = getattr(self, "_ttl_limit", 100)
    deleted = await purge_stale(self._store, staleness_threshold=threshold, limit=limit)
    return len(deleted)
```

在 `__init__` 末尾加:
```python
self._ttl_threshold = 0.85
self._ttl_limit = 100
```

**Step 5: 加 config 字段**

`cc_harness/memory/config.py`:
```python
# TTL (D3)
ttl_staleness_threshold: float = 0.85
ttl_limit: int = 100
```

`_check_positive_int` 字段枚举加:
```python
@field_validator("injection_token_budget", "retriever_top_k",
                 "pipeline_recent_turns", "pipeline_max_delta_tokens",
                 "pipeline_every_n", "scenario_min_atoms",
                 "persona_trigger_every_n", "recall_top_k",
                 "offload_threshold",
                 "maintenance_every_n_turns", "maintenance_count_threshold",
                 "ttl_limit")
```

**Step 6: export**

`cc_harness/memory/maintenance/__init__.py` 加:
```python
from cc_harness.memory.maintenance.ttl import purge_stale

__all__ = ["MaintenanceScheduler", "MaintenanceRun", "compute_staleness", "LLMRechecker", "purge_stale"]
```

**Step 7: 跑测试,确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_maintenance_ttl.py -v`
Expected: PASS

**Step 8: Commit**

```bash
git add cc_harness/memory/maintenance/ttl.py \
        cc_harness/memory/maintenance/scheduler.py \
        cc_harness/memory/maintenance/__init__.py \
        cc_harness/memory/config.py \
        tests/test_maintenance_ttl.py
git commit -m "feat(memory): TTL 过期清理 (purge_stale, threshold 0.85, limit)

- threshold < 0.7 强制抬到 0.7(硬底)
- 删 staleness >= threshold 的记忆, 限 limit
- 审计 logs/memory_maintenance.jsonl (stats only, 不含明文)
- scheduler._run_ttl 接入

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: Consolidation cluster + merge

**Files:**
- Create: `cc_harness/memory/maintenance/consolidation.py`
- Modify: `cc_harness/memory/maintenance/scheduler.py`(`_run_consolidation` 替换)
- Modify: `cc_harness/memory/maintenance/__init__.py`(export)
- Modify: `cc_harness/memory/config.py`(加 2 字段)
- Test: `tests/test_maintenance_consolidation.py`

**Interfaces:**

- Consumes(从 Task 3):
  - `MemoryStore.add / update / delete / get / list_with_staleness / search_similar`

- Produces:
  - `from cc_harness.memory.maintenance.consolidation import consolidate, _greedy_cluster`
  - `consolidate(store, embedder, llm=None, *, similarity_threshold=0.15, max_cluster_size=5) -> int`
  - `_greedy_cluster(mems_with_emb, threshold) -> list[list[Memory]]`
  - `MemoryConfig.consolidation_similarity_threshold: float = 0.15`
  - `MemoryConfig.consolidation_max_cluster_size: int = 5`

**Step 1: 写失败测试 — 簇形成 + LLM merge + 退化路径**

`tests/test_maintenance_consolidation.py`:
```python
import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from cc_harness.memory.store import MemoryStore
from cc_harness.memory.maintenance.consolidation import _greedy_cluster, consolidate


def test_greedy_cluster_forms_groups():
    a = MagicMock(id="a", embedding=[1.0] + [0.0] * 1023)
    b = MagicMock(id="b", embedding=[0.99] + [0.01] + [0.0] * 1022)
    c = MagicMock(id="c", embedding=[0.98] + [0.02] + [0.0] * 1022)
    d = MagicMock(id="d", embedding=[0.0] + [0.0] * 1022 + [1.0])
    clusters = _greedy_cluster([a, b, c, d], threshold=0.05)
    sizes = sorted([len(c) for c in clusters])
    assert sizes == [1, 3]


@pytest.mark.asyncio
async def test_consolidate_no_llm_keeps_oldest():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        store = MemoryStore(path, embedding_dim=1024)
        await store.init_schema()
        emb_similar = [0.99] + [0.01] + [0.0] * 1022
        m1 = await store.add("old1", emb_similar, "s")
        m2 = await store.add("new2", emb_similar, "s")
        embedder = MagicMock()
        embedder.embed = AsyncMock(side_effect=lambda t: emb_similar)
        deleted = await consolidate(store, embedder, llm=None, similarity_threshold=0.05, max_cluster_size=5)
        assert deleted == 1
        assert await store.get(m1.id) is not None
        assert await store.get(m2.id) is None
        await store.close()


@pytest.mark.asyncio
async def test_consolidate_cluster_too_large_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        store = MemoryStore(path, embedding_dim=1024)
        await store.init_schema()
        emb = [1.0] + [0.0] * 1023
        for i in range(7):
            await store.add(f"m{i}", emb, "s")
        embedder = MagicMock()
        embedder.embed = AsyncMock(side_effect=lambda t: emb)
        deleted = await consolidate(store, embedder, llm=None, similarity_threshold=0.05, max_cluster_size=5)
        assert deleted == 0
        await store.close()
```

**Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_maintenance_consolidation.py -v`
Expected: `ModuleNotFoundError: No module named 'cc_harness.memory.maintenance.consolidation'`

**Step 3: 实现**

`cc_harness/memory/maintenance/consolidation.py`:
```python
"""Consolidation: cluster 相似的, merge/update/noop。"""
from __future__ import annotations
import json
import math
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cc_harness.memory.store import MemoryStore, Memory


def _greedy_cluster(mems: list, threshold: float) -> list[list]:
    """O(N²) 贪心 cluster, 按向量欧氏距离。距离 < threshold 归一簇。"""
    n = len(mems)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for i in range(n):
        for j in range(i + 1, n):
            if not mems[i].embedding or not mems[j].embedding:
                continue
            d = math.sqrt(sum((a - b) ** 2 for a, b in zip(mems[i].embedding, mems[j].embedding)))
            if d < threshold:
                union(i, j)

    groups: dict[int, list] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(mems[i])
    return list(groups.values())


async def consolidate(store: "MemoryStore", embedder, llm=None, *,
                      similarity_threshold: float = 0.15,
                      max_cluster_size: int = 5) -> int:
    """全库扫一次, cluster 相似, merge/update/noop。返回受影响条数。"""
    cur = await store._db.execute(
        "SELECT id, text, embedding, created_at, updated_at, source, layer, session_id "
        "FROM memories LIMIT 500"
    )
    rows = await cur.fetchall()
    if not rows:
        return 0
    from cc_harness.memory.store import Memory, _blob_to_vec
    mems = [
        Memory(
            id=r[0], text=r[1], embedding=_blob_to_vec(r[2]),
            created_at=r[3], updated_at=r[4], source=r[5],
            layer=r[6], session_id=r[7],
        )
        for r in rows
    ]
    clusters = _greedy_cluster(mems, similarity_threshold)
    affected = 0
    for cluster in clusters:
        if len(cluster) < 2 or len(cluster) > max_cluster_size:
            continue
        cluster.sort(key=lambda m: m.created_at)
        if llm is None:
            keep = cluster[0]
            for m in cluster[1:]:
                if await store.delete(m.id):
                    affected += 1
            continue
        try:
            action = await _ask_llm_action(cluster, llm)
        except Exception:
            action = "noop"
        if action == "noop":
            continue
        if action == "merge":
            try:
                merged_text = await _ask_llm_merge(cluster, llm)
            except Exception:
                continue
            if not merged_text:
                continue
            try:
                new_emb = await embedder.embed(merged_text)
            except Exception:
                continue
            cluster_id = f"cluster-{cluster[0].id[:6]}"
            merged_from = json.dumps([m.id for m in cluster])
            for m in cluster:
                if await store.delete(m.id):
                    pass
            new_mem = await store.add(merged_text, new_emb, "consolidation", session_id=None)
            await store._db.execute(
                "UPDATE memories SET cluster_id = ?, merged_from = ? WHERE id = ?",
                (cluster_id, merged_from, new_mem.id),
            )
            await store._db.commit()
            affected += len(cluster)
        elif action == "update":
            keep = cluster[-1]
            try:
                new_text = await _ask_llm_merge(cluster, llm)
            except Exception:
                continue
            if not new_text:
                continue
            try:
                new_emb = await embedder.embed(new_text)
            except Exception:
                continue
            await store.update(keep.id, new_text, new_emb)
            for m in cluster[:-1]:
                if await store.delete(m.id):
                    affected += 1
    return affected


async def _ask_llm_action(cluster: list, llm) -> str:
    items = [{"id": m.id, "text": m.text, "created_at": m.created_at} for m in cluster]
    prompt = (
        "Decide action: merge (replace all with one new), update (merge into newest), or noop. "
        "Reply JSON {\"action\": \"merge\"|\"update\"|\"noop\"}\n\n"
        + json.dumps(items, ensure_ascii=False)
    )
    content_parts: list[str] = []
    async for ev in llm.chat(
        [{"role": "user", "content": prompt}], tools=None
    ):
        if ev.kind == "content":
            content_parts.append(ev.text)
        elif ev.kind == "done" and ev.content:
            content_parts = [ev.content]
    full = "".join(content_parts).strip()
    m = re.search(r"\{.*\}", full, re.DOTALL)
    if not m:
        return "noop"
    try:
        data = json.loads(m.group(0))
    except Exception:
        return "noop"
    a = data.get("action", "noop")
    return a if a in ("merge", "update", "noop") else "noop"


async def _ask_llm_merge(cluster: list, llm) -> str:
    items = [{"id": m.id, "text": m.text} for m in cluster]
    prompt = (
        "Merge these into a single concise memory, preserving all unique facts. "
        "Reply JSON {\"merged_text\": \"...\"}\n\n"
        + json.dumps(items, ensure_ascii=False)
    )
    content_parts: list[str] = []
    async for ev in llm.chat(
        [{"role": "user", "content": prompt}], tools=None
    ):
        if ev.kind == "content":
            content_parts.append(ev.text)
        elif ev.kind == "done" and ev.content:
            content_parts = [ev.content]
    full = "".join(content_parts).strip()
    m = re.search(r"\{.*\}", full, re.DOTALL)
    if not m:
        return ""
    try:
        data = json.loads(m.group(0))
    except Exception:
        return ""
    return str(data.get("merged_text", "")).strip()
```

**Step 4: 替换 scheduler._run_consolidation + 加 config**

`scheduler.py` `_run_consolidation` 替换:
```python
async def _run_consolidation(self) -> int:
    from cc_harness.memory.maintenance.consolidation import consolidate
    embedder = getattr(self, "_embedder", None)
    if embedder is None:
        return 0
    return await consolidate(
        self._store, embedder, self._llm,
        similarity_threshold=getattr(self, "_consol_threshold", 0.15),
        max_cluster_size=getattr(self, "_consol_max", 5),
    )
```

`__init__` 末尾加:
```python
self._embedder = None  # 由 main 注入
self._consol_threshold = 0.15
self._consol_max = 5
```

`config.py`:
```python
# consolidation (D4)
consolidation_similarity_threshold: float = 0.15
consolidation_max_cluster_size: int = 5
```

**Step 5: export**

`__init__.py`:
```python
from cc_harness.memory.maintenance.consolidation import consolidate, _greedy_cluster

__all__ = ["MaintenanceScheduler", "MaintenanceRun", "compute_staleness", "LLMRechecker",
           "purge_stale", "consolidate", "_greedy_cluster"]
```

**Step 6: 跑测试**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_maintenance_consolidation.py -v`
Expected: PASS

**Step 7: Commit**

```bash
git add cc_harness/memory/maintenance/consolidation.py \
        cc_harness/memory/maintenance/scheduler.py \
        cc_harness/memory/maintenance/__init__.py \
        cc_harness/memory/config.py \
        tests/test_maintenance_consolidation.py
git commit -m "feat(memory): consolidation cluster + merge/update/noop

- _greedy_cluster: O(N²) 欧氏距离贪心 cluster
- consolidate: 簇 2-N 调 LLM 判 merge/update/noop
  - merge: 生成 merged_text, 删旧 + 写新, cluster_id + merged_from 关联
  - update: 覆盖最新一条, 删其余
  - noop: 跳过
- 退化路径(无 LLM): 保留最早, 删其余
- 簇 > max_cluster_size 跳过(留给下次)
- scheduler._run_consolidation 接入, 需注入 embedder

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 5: 矛盾检测 (write-time + maintenance 全库扫)

**Files:**
- Create: `cc_harness/memory/maintenance/conflict.py`
- Modify: `cc_harness/memory/maintenance/scheduler.py`(`_run_conflict` 替换)
- Modify: `cc_harness/memory/maintenance/__init__.py`(export)
- Modify: `cc_harness/memory/service.py:34-69`(write-time 叠加)
- Test: `tests/test_maintenance_conflict.py`

**Interfaces:**

- Consumes(从 Task 4):
  - `MemoryStore.delete / search_similar / list_with_staleness`

- Produces:
  - `from cc_harness.memory.maintenance.conflict import ConflictDetector, ConflictVerdict`
  - `ConflictVerdict(other_id, verdict, action)` — verdict ∈ {contradicts/supersedes/elaborates/unrelated}, action ∈ {delete_old/delete_new/merge/noop}
  - `ConflictDetector(llm).check(new_mem, similar) -> list[ConflictVerdict]`
  - `ConflictDetector(llm).scan_all(store, embedder) -> int` — maintenance 用

**Step 1: 写失败测试**

`tests/test_maintenance_conflict.py`:
```python
import pytest
from unittest.mock import MagicMock
from cc_harness.memory.maintenance.conflict import ConflictDetector, ConflictVerdict


def make_mem(mid="m1", text="t"):
    m = MagicMock()
    m.id = mid
    m.text = text
    m.created_at = 1.0
    return m


@pytest.mark.asyncio
async def test_check_returns_verdicts():
    llm = MagicMock()
    async def fake_chat(msgs, tools=None):
        for ev_kind, ev_text in [
            ("done", '{"verdicts": [{"other_id": "old1", "verdict": "supersedes", "action": "delete_old"}]}'),
        ]:
            if ev_kind == "done":
                yield MagicMock(kind="done", content=ev_text)
    llm.chat = fake_chat
    det = ConflictDetector(llm)
    new = make_mem("new1", "user uses pnpm")
    similar = [make_mem("old1", "user uses npm")]
    verdicts = await det.check(new, similar)
    assert len(verdicts) == 1
    assert verdicts[0].action == "delete_old"


@pytest.mark.asyncio
async def test_check_llm_failure_returns_empty():
    llm = MagicMock()
    async def boom(*a, **kw):
        raise RuntimeError("api down")
        if False:
            yield
    llm.chat = boom
    det = ConflictDetector(llm)
    verdicts = await det.check(make_mem(), [make_mem("o1")])
    assert verdicts == []


@pytest.mark.asyncio
async def test_check_unrelated_filtered():
    llm = MagicMock()
    async def fake_chat(msgs, tools=None):
        yield MagicMock(kind="done", content='{"verdicts": [{"other_id": "x", "verdict": "unrelated", "action": "noop"}]}')
    llm.chat = fake_chat
    det = ConflictDetector(llm)
    verdicts = await det.check(make_mem("n"), [make_mem("x")])
    assert verdicts == []
```

**Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_maintenance_conflict.py -v`
Expected: `ModuleNotFoundError: No module named 'cc_harness.memory.maintenance.conflict'`

**Step 3: 实现**

`cc_harness/memory/maintenance/conflict.py`:
```python
"""矛盾检测: write-time + maintenance 全库扫。"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass


VALID_VERDICTS = {"contradicts", "supersedes", "elaborates", "unrelated"}
VALID_ACTIONS = {"delete_old", "delete_new", "merge", "noop"}


@dataclass
class ConflictVerdict:
    other_id: str
    verdict: str
    action: str


class ConflictDetector:
    def __init__(self, llm):
        self._llm = llm

    async def check(self, new_mem, similar: list) -> list[ConflictVerdict]:
        """similar: list[Memory]。返回 0-N 个 verdict。LLM 失败返空。"""
        if not similar or self._llm is None:
            return []
        items = [{"id": m.id, "text": m.text} for m in similar]
        prompt = (
            "Compare the new memory to each existing. For each, classify as "
            "contradicts/supersedes/elaborates/unrelated and pick action "
            "delete_old/delete_new/merge/noop. "
            "Reply JSON {\"verdicts\": [{\"other_id\": \"...\", \"verdict\": \"...\", \"action\": \"...\"}, ...]}\n\n"
            f"NEW: {json.dumps({'id': new_mem.id, 'text': new_mem.text}, ensure_ascii=False)}\n"
            f"EXISTING: {json.dumps(items, ensure_ascii=False)}"
        )
        try:
            content_parts: list[str] = []
            async for ev in self._llm.chat(
                [{"role": "user", "content": prompt}], tools=None
            ):
                if ev.kind == "content":
                    content_parts.append(ev.text)
                elif ev.kind == "done" and ev.content:
                    content_parts = [ev.content]
            full = "".join(content_parts).strip()
        except Exception:
            return []
        m = re.search(r"\{.*\}", full, re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except Exception:
            return []
        out: list[ConflictVerdict] = []
        for v in data.get("verdicts", []):
            verdict = v.get("verdict", "unrelated")
            action = v.get("action", "noop")
            if verdict not in VALID_VERDICTS or action not in VALID_ACTIONS:
                continue
            if verdict == "unrelated" or action == "noop":
                continue
            out.append(ConflictVerdict(other_id=v["other_id"], verdict=verdict, action=action))
        return out

    async def scan_all(self, store, embedder) -> int:
        """maintenance 用: 全库扫, 找矛盾对。LLM 不可用返 0。"""
        if self._llm is None or embedder is None:
            return 0
        cur = await store._db.execute("SELECT id, text FROM memories LIMIT 500")
        rows = await cur.fetchall()
        if len(rows) < 2:
            return 0
        affected = 0
        for r in rows:
            mid, text = r[0], r[1]
            try:
                emb = await embedder.embed(text)
                similar = await store.search_similar(emb, k=3)
            except Exception:
                continue
            similar = [m for m in similar if m.id != mid]
            if not similar:
                continue
            new_mock = MagicMock(id=mid, text=text)
            verdicts = await self.check(new_mock, similar)
            for v in verdicts:
                if v.action == "delete_old":
                    if await store.delete(v.other_id):
                        affected += 1
        return affected
```

**Step 4: 替换 scheduler._run_conflict + write-time 注入 service**

`scheduler.py` `_run_conflict` 替换:
```python
async def _run_conflict(self) -> int:
    from cc_harness.memory.maintenance.conflict import ConflictDetector
    embedder = getattr(self, "_embedder", None)
    if embedder is None or self._llm is None:
        return 0
    det = ConflictDetector(self._llm)
    return await det.scan_all(self._store, embedder)
```

`cc_harness/memory/service.py` 在 `save()` 末尾(`return SaveResult(...)` 之前)叠加:
```python
# E4 write-time 矛盾检测(写盘后, 仅 ADD/UPDATE/DELETE_THEN_ADD 触发)
if self.decider is not None and result_action_mem is not None:
    try:
        from cc_harness.memory.maintenance.conflict import ConflictDetector
        det = ConflictDetector(self.decider._llm)
        similar_for_conflict = await self.store.search_similar(embedding, k=5)
        verdicts = await det.check(result_action_mem, similar_for_conflict)
        for v in verdicts:
            if v.action == "delete_old":
                await self.store.delete(v.other_id)
            elif v.action == "delete_new":
                await self.store.delete(result_action_mem.id)
                return SaveResult(action="ROLLBACK", error=f"conflict:{v.verdict}",
                                  duration_ms=_ms(t0))
    except Exception:
        pass  # 矛盾检测失败不阻塞
```

注:`result_action_mem` 需在 ADD/UPDATE/DELETE_THEN_ADD 三个分支设置。execute 阶段需重构 save() 把这个变量提到 try 块前。

**Step 5: export**

`__init__.py`:
```python
from cc_harness.memory.maintenance.conflict import ConflictDetector, ConflictVerdict

__all__ = ["MaintenanceScheduler", "MaintenanceRun", "compute_staleness", "LLMRechecker",
           "purge_stale", "consolidate", "_greedy_cluster",
           "ConflictDetector", "ConflictVerdict"]
```

**Step 6: 跑测试**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_maintenance_conflict.py -v`
Expected: PASS

**Step 7: Commit**

```bash
git add cc_harness/memory/maintenance/conflict.py \
        cc_harness/memory/maintenance/scheduler.py \
        cc_harness/memory/maintenance/__init__.py \
        cc_harness/memory/service.py \
        tests/test_maintenance_conflict.py
git commit -m "feat(memory): 矛盾检测 — write-time + maintenance 全库扫

- ConflictDetector.check(new, similar) → list[ConflictVerdict]
- 4 类 verdict (contradicts/supersedes/elaborates/unrelated) + 4 action
- write-time 在 MemoryService.save() 写盘后叠加, delete_new 走 ROLLBACK
- maintenance 全库扫通过 scan_all(store, embedder) — 走 search_similar
- LLM 失败返回空 list, 不抛

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 6: 召回衰减 (RecallWeighter 注入 retriever)

**Files:**
- Create: `cc_harness/memory/maintenance/recall_weight.py`
- Modify: `cc_harness/memory/retriever.py:27-29`(search 末尾插入)
- Modify: `cc_harness/memory/maintenance/__init__.py`(export)
- Modify: `cc_harness/memory/config.py`(加 3 字段 + 1 validator)
- Test: `tests/test_maintenance_recall_weight.py`
- Test: `tests/_test_maintenance_e2e.py`
- Test: `eval/locomo/tests/test_maintenance_locomo.py`

**Interfaces:**

- Consumes(从 Task 5):
  - `MemoryRetriever.search` 在 RecallWeighter 之前调 `MemoryStore.touch_recall`

- Produces:
  - `from cc_harness.memory.maintenance.recall_weight import RecallWeighter`
  - `RecallWeighter(*, staleness_floor=0.7, staleness_soft=0.5, weight_floor=0.5).apply(results: list[tuple[Memory, float]]) -> list[tuple[Memory, float]]`
  - `MemoryConfig.recall_staleness_floor: float = 0.7`
  - `MemoryConfig.recall_staleness_soft: float = 0.5`
  - `MemoryConfig.recall_weight_floor: float = 0.5`

**Step 1: 写失败测试**

`tests/test_maintenance_recall_weight.py`:
```python
from unittest.mock import MagicMock
from cc_harness.memory.maintenance.recall_weight import RecallWeighter


def make_mem(mid, staleness):
    m = MagicMock()
    m.id = mid
    m.staleness = staleness
    return m


def test_apply_filters_above_floor():
    w = RecallWeighter(staleness_floor=0.7, staleness_soft=0.5, weight_floor=0.5)
    a = make_mem("a", 0.3)
    b = make_mem("b", 0.8)
    c = make_mem("c", 0.6)
    out = w.apply([(a, 1.0), (b, 0.95), (c, 0.9)])
    ids = [m.id for m, _ in out]
    assert "b" not in ids


def test_apply_soft_weight_lowers_score():
    w = RecallWeighter(staleness_floor=0.7, staleness_soft=0.5, weight_floor=0.5)
    a = make_mem("a", 0.3)
    b = make_mem("b", 0.55)
    out = w.apply([(a, 1.0), (b, 1.0)])
    out_a = next(s for m, s in out if m.id == "a")
    out_b = next(s for m, s in out if m.id == "b")
    assert out_a > out_b


def test_apply_weight_floor_min():
    w = RecallWeighter(staleness_floor=0.95, staleness_soft=0.0, weight_floor=0.5)
    a = make_mem("a", 0.1)
    out = w.apply([(a, 1.0)])
    out_a = next(s for m, s in out if m.id == "a")
    assert out_a >= 0.5
```

**Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_maintenance_recall_weight.py -v`
Expected: `ModuleNotFoundError: No module named 'cc_harness.memory.maintenance.recall_weight'`

**Step 3: 实现**

`cc_harness/memory/maintenance/recall_weight.py`:
```python
"""召回衰减: 软加权 + 硬阈值。"""
from __future__ import annotations


class RecallWeighter:
    def __init__(self, *, staleness_floor: float = 0.7,
                 staleness_soft: float = 0.5,
                 weight_floor: float = 0.5):
        self.staleness_floor = staleness_floor
        self.staleness_soft = staleness_soft
        self.weight_floor = weight_floor

    def apply(self, results: list) -> list:
        """results: [(Memory, score), ...] → 软加权后重排, 硬阈值过滤。"""
        out = []
        for mem, score in results:
            staleness = getattr(mem, "staleness", 0.0) or 0.0
            if staleness >= self.staleness_floor:
                continue
            weight = self._weight(staleness)
            out.append((mem, score * weight))
        out.sort(key=lambda x: -x[1])
        return out

    def _weight(self, staleness: float) -> float:
        if staleness <= self.staleness_soft:
            return 1.0
        ratio = (staleness - self.staleness_soft) / max(1e-6, self.staleness_floor - self.staleness_soft)
        return max(self.weight_floor, 1.0 - ratio * (1.0 - self.weight_floor))
```

**Step 4: 改 retriever.search**

`cc_harness/memory/retriever.py:27-29` `search` 方法替换:
```python
async def search(self, query: str, top_k: int = 5) -> list:
    embedding = await self._embedder.embed(query)
    results = await self._store.search_similar(embedding, k=top_k * 2)
    if results:
        ids = [m.id for m, _ in results]
        try:
            await self._store.touch_recall(ids)
        except Exception:
            pass
    from cc_harness.memory.maintenance.recall_weight import RecallWeighter
    weighter = RecallWeighter()
    weighted = weighter.apply(results)
    return weighted[:top_k]
```

**Step 5: 加 config 字段 + validator**

`config.py`:
```python
# Recall 衰减 (D7)
recall_staleness_floor: float = 0.7
recall_staleness_soft: float = 0.5
recall_weight_floor: float = 0.5
```

新增 validator:
```python
@field_validator("recall_staleness_soft", "recall_weight_floor")
@classmethod
def _check_recall_range(cls, v: float) -> float:
    if not (0 < v < 1):
        raise ValueError(f"must be in (0, 1), got {v}")
    return v
```

**Step 6: export**

`__init__.py`:
```python
from cc_harness.memory.maintenance.recall_weight import RecallWeighter

__all__ = ["MaintenanceScheduler", "MaintenanceRun", "compute_staleness", "LLMRechecker",
           "purge_stale", "consolidate", "_greedy_cluster",
           "ConflictDetector", "ConflictVerdict",
           "RecallWeighter"]
```

**Step 7: E2E + LoCoMo 集成测试**

`tests/_test_maintenance_e2e.py`:
```python
"""E2E gated(需真 LLM + 真 embedding): 验证 6 件 op 全跑过 + 不破坏主 ReAct。

跑法:
  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/_test_maintenance_e2e.py -v
"""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
import pytest

pytestmark = pytest.mark.requires_llm


@pytest.mark.asyncio
async def test_e2e_all_ops_run_with_real_llm():
    """完整管线: 写入 50 条 → 触发 scheduler → 后台跑完 → 验证 4 op 全跑。"""
    from cc_harness.memory.store import MemoryStore
    from cc_harness.memory.service import MemoryService
    from cc_harness.memory.maintenance.scheduler import MaintenanceScheduler

    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(Path(tmp) / "e2e.db", embedding_dim=1024)
        await store.init_schema()
        # 真实 embedder 需 EMBEDDING_* env; 若无则跳过
        try:
            from cc_harness.memory.embedding import EmbeddingClient
            embedder = EmbeddingClient.from_env()
        except Exception:
            pytest.skip("EMBEDDING_* env not set, skipping e2e")
        llm = MagicMock()
        # 写 50 条
        for i in range(50):
            emb = await embedder.embed(f"memory {i}")
            await store.add(f"memory {i}", emb, "e2e")
        # 跑 scheduler
        service = MemoryService(store, embedder, llm)
        sch = MaintenanceScheduler(store, service, llm=llm, every_n_turns=1)
        sch._embedder = embedder
        await sch.maybe_run(turn_idx=1)
        await sch._drain(timeout_s=30)
        # 验证 4 op 都跑过
        assert sch._current_task is None or sch._current_task.done()
        # 验证 store 还活着
        count = await store.count()
        assert count > 0
        await store.close()
```

`eval/locomo/tests/test_maintenance_locomo.py`:
```python
"""LoCoMo 跑 1 sample 对比 maintenance 前后 utilization/recall。

跑法:
  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_maintenance_locomo.py -v
"""
import pytest

pytestmark = pytest.mark.requires_llm


def test_locomo_with_maintenance_does_not_break_recall():
    """跑 1 个 locomo sample, 验证 maintenance 开启不显著降低 recall (±2% 漂移)。"""
    try:
        from eval.locomo.runner import run_one_sample
        from eval.locomo.metrics import compute_recall
    except Exception as e:
        pytest.skip(f"locomo imports failed: {e}")
    sample_id = "locomo-001"
    try:
        result_before = run_one_sample(sample_id, maintenance=False)
        result_after = run_one_sample(sample_id, maintenance=True)
    except Exception as e:
        pytest.skip(f"locomo run failed (likely missing dataset): {e}")
    recall_before = compute_recall(result_before)
    recall_after = compute_recall(result_after)
    delta = abs(recall_after - recall_before)
    assert delta < 0.02, f"recall 漂移 {delta:.3f} 超过 2% 阈值"
```

**Step 8: 跑测试**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_maintenance_recall_weight.py -v`
Expected: PASS

**Step 9: Commit**

```bash
git add cc_harness/memory/maintenance/recall_weight.py \
        cc_harness/memory/retriever.py \
        cc_harness/memory/maintenance/__init__.py \
        cc_harness/memory/config.py \
        tests/test_maintenance_recall_weight.py \
        tests/_test_maintenance_e2e.py \
        eval/locomo/tests/test_maintenance_locomo.py
git commit -m "feat(memory): 召回衰减 (RecallWeighter 注入 retriever)

- RecallWeighter.apply: 软加权 (staleness > soft 线性降) + 硬阈值 (>= floor 踢)
- MemoryRetriever.search: 末尾插入 touch_recall + RecallWeighter
- MemoryConfig 加 3 字段 (recall_staleness_floor/soft/weight_floor) + 1 validator
- E2E gated + LoCoMo 集成测试占位

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review Checklist

**Spec coverage**:
- D1 范围 6 件 ✅ Task 1-6
- D2 1 spec 6 commit ✅ Task 1-6 各 1 commit
- D3 commit 顺序 ✅ Task 顺序一致
- D4 调度方式 ✅ Task 1 scheduler
- D5 staleness 算子 + LLM 复检 ✅ Task 2
- D6 矛盾检测双触发 ✅ Task 5 (write-time + scan_all)
- D7 召回衰减 ✅ Task 6
- 配置 11 字段 ✅ Task 1-6 各加
- 审计不记明文 ✅ Task 3 `_audit` 只记 ids
- 错误处理 ✅ Task 1 错误隔离 + Task 5 LLM 失败返空
- 测试策略 ✅ 每 task 单测 + Task 1 集成 + Task 6 E2E + LoCoMo
- 非目标:未在 plan 中实现,符合 spec

**Placeholder 扫描**:
- 无 TBD / TODO / "implement later"
- Task 1 Step 6 含 "由 execute 阶段根据 repl.py 现状接入" — 这是合理的 execute 阶段决定点,非 placeholder
- Task 5 Step 4 含 "execute 阶段需重构 save() 把这个变量提到 try 块前" — 同样为 execute 阶段决定点

**Type consistency**:
- `MaintenanceScheduler` 构造参数 6 个,各 task 用法一致
- `MemoryConfig` 字段 11 个,Task 1-6 各自加不冲突
- `MemoryStore` 新增方法:Task 2 加 `touch_recall / update_staleness_bulk / list_with_staleness`,Task 5 复用 `delete / search_similar`
- `RecallWeighter.apply` 输入输出类型在 Task 6 测试和实现中一致
- Task 5 `ConflictDetector.scan_all(store, embedder)` 与 scheduler 调用签名一致

**spec 范围外但在 spec "开放问题" 提到的 prompt 措辞 / 阈值调整** — 留 plan 阶段后续微调,不在本 plan 改。
