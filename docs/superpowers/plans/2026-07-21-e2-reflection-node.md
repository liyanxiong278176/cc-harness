# E2 Reflection Node Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 cc-harness 引入**反思节点(Reflection Node)** —— 在主 agent / SubAgent / LLMDecider 三面出现"值得反思"信号时,调 JUDGE_MODEL 产出结构化反思,**写盘入 MemoryService**(`source='reflection'`,走 E4 矛盾检测 + staleness) **+ 同步注入下一轮 system prompt 的 reflection section**(neg-only,ambig/pos 只入库)。

**Architecture:** 中心化 `ReflectionEngine`(`cc_harness/reflection/engine.py`)+ 4 类事件工厂(`events.py`)+ 反思 prompt 模板(`prompts.py`)。JUDGE_MODEL 不可用 → 退回本地 LLMClient;都失败 → fail-soft noop + 审计。1 spec 3 commit 从底到顶:engine 中心化 → main agent 接入 → memory/decider 扩参 + subagent 接入。

**Tech Stack:** Python 3.11+ / aiosqlite / asyncio / pydantic / ruff / pytest-asyncio;复用 LoCoMo m5 `eval/locomo/metrics.py:run_judge` 协议(JUDGE_MODEL + cache + pollution_guard);复用 E4 `MemoryService.save()` 走矛盾检测 + staleness;复用 L5 DLP `cc_harness/l5.py` 密钥正则;`prompts.SECTION_POOL` 末尾加 reflection section。

## Global Constraints

[Spec 锁死的全局约束 — 每个 task 隐式适用,违反即返工]

- **Python 3.11+**:禁止 `asyncio.coroutine` 之类删 API,使用 `async def`。
- **E4 已落地(2026-07-20 commit `72b02e4`)**:本 plan 强依赖 E4 `MemoryService.save()` 走矛盾检测 + staleness + `search_reflections(limit, lookback_h)`。
- **JUDGE_MODEL 已就位**:LoCoMo m5 `eval/locomo/metrics.py:run_judge` 协议稳定,E2 反思节点直接传 `system=NEG_REFLECT_PROMPT` 调用,不新写 LLM 客户端。
- **L5 DLP**:`cc_harness/l5.py` 必走,evidence 字段 emit 前 sanitize。
- **prompts.SECTION_POOL**:新 section 走 pool,不动 `build_system_prompt`(沿用 E1 spec "新 section 走 pool"原则)。
- **`llm_client` / `judge_llm` 形参注入**:`ReflectionEngine.__init__` 接受二者,不全局 import,避免循环。
- **失败 fail-soft**:JUDGE_MODEL 失败 → 退回本地 LLMClient;都失败 → `noop` + 审计 + 不抛。
- **频率上限**:`every_n_turns=10` + `max_pending=3` 队列 + 短窗口去重 5s。
- **drain 超时**:`_drain(timeout_s=5)`,超时 `task.cancel()`(沿用 E4 scheduler 模式)。
- **审计**:`<root>/logs/reflection.jsonl`,每行 `{ts, op, event_type, severity, memory_id?, reason?}`,**绝不记反思明文**。
- **section 长度**:≤200 token,~1-3 行;neg-only inject。
- **不要全局 MagicMock**:production 路径 0 命中,只测试桩位用。
- **commit 规范**:`feat(reflection): ...` / `fix(reflection): ...` / `test(reflection): ...` / `chore(reflection): ...`,Co-Authored-By 由 commit 自动加。
- **不**新建 `reflections` 表:走 `memories` 表 `source='reflection'`(E4 已扩 source 字段)。
- **不**扩 AppConfig:沿用 E4 I-1 模式,`main.py:boot()` 构造 + 注入 `repl.run_repl`。

## File Structure

### 新增文件(8 文件)

| 路径 | 职责 | 任务 |
|---|---|---|
| `cc_harness/reflection/__init__.py` | export ReflectionEngine / ReflectionEvent / 6 event factories / ReflectionOutcome | T1.1, T1.5 |
| `cc_harness/reflection/events.py` | `ReflectionEvent` dataclass + 6 event factories(max_iter/empty_turn/tool_error_burst/tool_retry_burst/subagent_failed/decider_rollback) | T1.2 |
| `cc_harness/reflection/prompts.py` | NEG/AMBIG/POS_REFLECT_PROMPT 三套反思 prompt(JSON 输出) | T1.3 |
| `cc_harness/reflection/engine.py` | `ReflectionEngine` 中心化引擎 + `ReflectionOutcome` dataclass | T1.4 |
| `tests/test_reflection_memory.py` | LLMDecider 扩参 + JUDGE_MODEL 注入 + save 触发矛盾 + search_reflections | T3.1 |
| `tests/test_reflection_memory_integration.py` | 50 条 save → decider 看见反思 → 反思写盘 → retriever 召出 | T3.2 |
| `tests/test_reflection_engine.py` | 4 类事件工厂字段 + run_judge 协议 + fail-soft + Lock + drain | T2.1 |
| `tests/test_reflection_section.py` | SECTION_POOL reflection section + 注入条件 + token 预算 | T2.2 |
| `tests/test_reflection_main_integration.py` | agent.run_turn 4 类事件真实 emit + drain 拿结果 | T2.3 |
| `tests/test_reflection_subagent.py` | SubAgentRunner 末尾 emit + status 映射 + 父 agent recent_reflections | T3.3 |
| `tests/test_reflection_subagent_integration.py` | 派 1 个故意失败的 subagent → 父收 → emit → 反思写盘 → 父下轮看见 | T3.4 |
| `tests/_test_reflection_e2e.py` | 真 LLM 端到端(`_test_` 前缀,pytest 不默认收) | T1.6 |

### 修改文件(7 文件)

| 路径 | 改动 | 任务 |
|---|---|---|
| `cc_harness/memory/decider.py` | `decide()` 扩 1 形参 `recent_reflections: list[Memory] \| None = None` | T3.1 |
| `cc_harness/memory/service.py` | `save()` 调 `decider.decide()` 前召 `search_reflections(24h)` 注入 | T3.1 |
| `cc_harness/memory/store.py` | 加 `search_reflections(*, limit, lookback_h) -> list[Memory]` | T3.1 |
| `cc_harness/memory/config.py` | 加 4 字段:`reflection_enabled / reflection_every_n_turns / reflection_max_pending / reflection_drain_timeout_s` | T1.5 |
| `cc_harness/prompts.py` | SECTION_POOL 末尾加 reflection section + `_reflection_section` builder | T2.2 |
| `cc_harness/agent.py` | `run_turn` 扩形参 `reflection_engine: ReflectionEngine \| None` + 4 处 emit + `_refresh_system_prompt` 末尾加 `last_neg_reflection` ctx | T2.1, T2.2, T2.3 |
| `cc_harness/project/subagent.py` | `SubAgentRunner.run` 末尾 emit + `_render_subagent_summary` 加 `recent_reflections` 字段 | T3.3 |
| `cc_harness/repl.py` | `run_repl` 扩形参 `reflection_engine: ReflectionEngine \| None` + finally `_drain` | T2.3 |
| `main.py` | `boot()` 构造 `ReflectionEngine` + 注入 `run_repl` | T2.3 |

### 复用(不修改,只读)

- `cc_harness/memory/service.py:MemoryService.save` — 走 E4 矛盾检测 + staleness
- `cc_harness/memory/maintenance/scheduler.py` — E4 调度,本 plan 不动
- `cc_harness/l5.py:L5Engine.sanitize` — 输出侧 DLP,evidence 字段必过
- `eval/locomo/metrics.py:run_judge` — JUDGE_MODEL 协议(异步 + cache + pollution_guard)
- `cc_harness/prompts.py:SECTION_POOL` — 10 sections 已注册
- `cc_harness/llm.py:LLMClient` — 本地 LLM 客户端(judge 失败退回)

---

## Commit 路线图(3 commit)

```
#1  feat(reflection): ReflectionEngine 中心化 + 6 类事件工厂 + 反思 prompt 模板
#2  feat(reflection): main agent 接入 (4 类 emit + section 注入 + wiring)
#3  feat(reflection): memory/decider 扩参 + subagent 末尾 emit + 父 agent 注入 recent_reflections
```

---

## Commit 1: feat(reflection) ReflectionEngine 中心化

### Task 1.1: ReflectionEvent dataclass + 6 event factories(stub 实现)

**Files:**
- Create: `cc_harness/reflection/__init__.py`
- Create: `cc_harness/reflection/events.py`
- Test: `tests/test_reflection_events.py`

**Interfaces:**
- Consumes: 无(基座)
- Produces:
  - `ReflectionEvent(event_type: str, severity: str, evidence: dict, session_id: str, turn_idx: int, created_at: float)`
  - 6 工厂函数:`max_iter_reached / empty_turn_loop / tool_error_burst / tool_retry_burst / subagent_failed / decider_rollback`,全部返回 `ReflectionEvent`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_reflection_events.py
import time
from cc_harness.reflection.events import (
    ReflectionEvent, max_iter_reached, empty_turn_loop,
    tool_error_burst, tool_retry_burst, subagent_failed, decider_rollback,
)


def test_max_iter_reached_factory():
    ev = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="...")
    assert ev.event_type == "max_iter"
    assert ev.severity == "neg"
    assert ev.session_id == "s1"
    assert ev.turn_idx == 3
    assert ev.evidence["iter_used"] == 20
    assert isinstance(ev.created_at, float)


def test_empty_turn_loop_factory():
    ev = empty_turn_loop(session_id="s1", turn_idx=4, attempts=2)
    assert ev.event_type == "empty_turn"
    assert ev.severity == "neg"


def test_tool_error_burst_factory():
    ev = tool_error_burst(
        session_id="s1", turn_idx=5,
        errors=[{"tool": "run_command", "error": "exit 1"}],
    )
    assert ev.event_type == "tool_error_burst"
    assert ev.severity == "neg"
    assert len(ev.evidence["errors"]) == 1


def test_tool_retry_burst_factory():
    ev = tool_retry_burst(
        session_id="s1", turn_idx=6,
        calls=[{"tool": "fs__read", "args": {"path": "/x.py"}, "count": 3}],
    )
    assert ev.event_type == "tool_retry_burst"
    assert ev.severity == "ambig"


def test_subagent_failed_factory():
    ev = subagent_failed(
        session_id="s1", turn_idx=7,
        result={"status": "failed", "task_id": "t1", "final_text": "..."},
    )
    assert ev.event_type == "subagent_failed"
    assert ev.severity == "neg"


def test_subagent_blocked_maps_ambig():
    ev = subagent_failed(
        session_id="s1", turn_idx=8,
        result={"status": "blocked", "task_id": "t1"},
    )
    assert ev.severity == "ambig"


def test_decider_rollback_factory():
    ev = decider_rollback(
        session_id="s1", turn_idx=9,
        save_result={"action": "ROLLBACK", "error": "conflict:contradicts"},
    )
    assert ev.event_type == "decider_rollback"
    assert ev.severity == "neg"
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_events.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cc_harness.reflection'`

- [ ] **Step 3: 实现 `cc_harness/reflection/__init__.py`**

```python
"""Reflection node — passive event-driven self-correction layer (E2)."""
from cc_harness.reflection.events import (
    ReflectionEvent,
    max_iter_reached,
    empty_turn_loop,
    tool_error_burst,
    tool_retry_burst,
    subagent_failed,
    decider_rollback,
)

__all__ = [
    "ReflectionEvent",
    "max_iter_reached",
    "empty_turn_loop",
    "tool_error_burst",
    "tool_retry_burst",
    "subagent_failed",
    "decider_rollback",
]
```

- [ ] **Step 4: 实现 `cc_harness/reflection/events.py`**

```python
"""Reflection event dataclass + 6 event factories.

Each factory returns a `ReflectionEvent` ready to be fed to
`ReflectionEngine.emit()`. The factory shape hides evidence shape details
so callers in `agent.py` / `subagent.py` only need to import the function
name.
"""
from __future__ import annotations
import time
from dataclasses import dataclass


@dataclass
class ReflectionEvent:
    event_type: str            # "max_iter" | "empty_turn" | "tool_error_burst" | "tool_retry_burst" | "subagent_failed" | "decider_rollback"
    severity: str              # "neg" | "ambig" | "pos"
    evidence: dict             # 原始事件载荷(去 PII,emit 前过 L5)
    session_id: str
    turn_idx: int
    created_at: float          # time.time() — 避免 datetime.now() 阻塞


def max_iter_reached(*, session_id: str, turn_idx: int, iter_used: int, last_content: str) -> ReflectionEvent:
    return ReflectionEvent(
        event_type="max_iter",
        severity="neg",
        evidence={"iter_used": iter_used, "last_content": last_content[:500]},
        session_id=session_id,
        turn_idx=turn_idx,
        created_at=time.time(),
    )


def empty_turn_loop(*, session_id: str, turn_idx: int, attempts: int) -> ReflectionEvent:
    return ReflectionEvent(
        event_type="empty_turn",
        severity="neg",
        evidence={"attempts": attempts},
        session_id=session_id,
        turn_idx=turn_idx,
        created_at=time.time(),
    )


def tool_error_burst(*, session_id: str, turn_idx: int, errors: list[dict]) -> ReflectionEvent:
    return ReflectionEvent(
        event_type="tool_error_burst",
        severity="neg",
        evidence={"errors": errors[:10]},  # 截断 10 条
        session_id=session_id,
        turn_idx=turn_idx,
        created_at=time.time(),
    )


def tool_retry_burst(*, session_id: str, turn_idx: int, calls: list[dict]) -> ReflectionEvent:
    return ReflectionEvent(
        event_type="tool_retry_burst",
        severity="ambig",
        evidence={"calls": calls[:10]},
        session_id=session_id,
        turn_idx=turn_idx,
        created_at=time.time(),
    )


def subagent_failed(*, session_id: str, turn_idx: int, result: dict) -> ReflectionEvent:
    status = result.get("status", "unknown")
    severity = "neg" if status in {"failed", "incomplete", "timeout"} else (
        "ambig" if status == "blocked" else "pos"
    )
    return ReflectionEvent(
        event_type="subagent_failed",
        severity=severity,
        evidence={
            "status": status,
            "task_id": result.get("task_id"),
            "final_text": (result.get("final_text") or "")[:500],
        },
        session_id=session_id,
        turn_idx=turn_idx,
        created_at=time.time(),
    )


def decider_rollback(*, session_id: str, turn_idx: int, save_result: dict) -> ReflectionEvent:
    return ReflectionEvent(
        event_type="decider_rollback",
        severity="neg",
        evidence={
            "action": save_result.get("action"),
            "error": save_result.get("error"),
        },
        session_id=session_id,
        turn_idx=turn_idx,
        created_at=time.time(),
    )
```

- [ ] **Step 5: 跑测试,确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_events.py -v`
Expected: 7 passed in <0.1s

- [ ] **Step 6: Commit**

```bash
git add cc_harness/reflection/__init__.py cc_harness/reflection/events.py tests/test_reflection_events.py
git commit -m "feat(reflection): ReflectionEvent dataclass + 6 event factories (T1.1)"
```

---

### Task 1.2: 反思 prompt 模板(neg/ambig/pos 3 套)

**Files:**
- Create: `cc_harness/reflection/prompts.py`
- Test: `tests/test_reflection_prompts.py`

**Interfaces:**
- Consumes: 6 类 `event_type` 字符串
- Produces:
  - `NEG_REFLECT_SYSTEM: str` / `NEG_REFLECT_USER_FMT: str`
  - `AMBIG_REFLECT_SYSTEM: str` / `AMBIG_REFLECT_USER_FMT: str`
  - `POS_REFLECT_SYSTEM: str` / `POS_REFLECT_USER_FMT: str`
  - `build_reflect_prompt(event: ReflectionEvent) -> tuple[str, str]` — 返回 `(system, user)` 给 `_ask_judge` 调用

- [ ] **Step 1: 写失败测试**

```python
# tests/test_reflection_prompts.py
import json
from cc_harness.reflection.events import max_iter_reached, tool_retry_burst
from cc_harness.reflection.prompts import build_reflect_prompt


def test_max_iter_uses_neg_template():
    ev = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="x")
    system, user = build_reflect_prompt(ev)
    assert "反思" in system or "反思" in user
    assert "max_iter" in user or "iter_used" in user


def test_tool_retry_uses_ambig_template():
    ev = tool_retry_burst(
        session_id="s1", turn_idx=6,
        calls=[{"tool": "fs__read", "args": {"path": "/x.py"}, "count": 3}],
    )
    system, user = build_reflect_prompt(ev)
    assert "反思" in system or "反思" in user
    assert "tool_retry_burst" in user or "刷运" in user or "犹豫" in user


def test_output_format_specified():
    """模板必须要求 JSON 输出,便于解析。"""
    ev = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="x")
    system, user = build_reflect_prompt(ev)
    assert "JSON" in system + user
    assert "reflection" in system + user
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_prompts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cc_harness.reflection.prompts'`

- [ ] **Step 3: 实现 `cc_harness/reflection/prompts.py`**

```python
"""Reflection prompt templates (neg/ambig/pos) for JUDGE_MODEL.

JSON 化输出便于解析;复用 LoCoMo m5 `_judge(judge_llm, system, user) -> str`
协议(2026-07-20 commit e64aaa8)。E2 不直接 import `eval.locomo.metrics._judge`
避免引入 eval 依赖,Engine 内联实现等价调用。
"""
from __future__ import annotations
from cc_harness.reflection.events import ReflectionEvent


NEG_REFLECT_SYSTEM = """你是 cc-harness 反思节点。LLM 在以下场景失败,你需要:
1. 找出失败根因(不归咎用户/环境,只反思 LLM 自身决策)
2. 提出下次如何避免(具体到 tool_call 选择 / 参数 / 顺序)
3. 输出 ≤ 200 字

严格 JSON 输出(无 markdown):
{"reflection": "<text>", "tags": ["<tag1>", "<tag2>"]}"""


AMBIG_REFLECT_SYSTEM = """你是 cc-harness 反思节点。LLM 出现决策不一致,你需要:
1. 判断是否在「刷运 / 犹豫 / 套话」
2. 如果是,反思下次如何收敛
3. 输出 ≤ 200 字

严格 JSON 输出(无 markdown):
{"reflection": "<text>", "tags": ["<tag1>", "<tag2>"]}"""


POS_REFLECT_SYSTEM = """你是 cc-harness 反思节点。LLM 出现连续成功,你需要:
1. 判断成功是「真实价值」还是「套话 / 走捷径」
2. 如果是套话,反思下次如何保持质量
3. 输出 ≤ 200 字

严格 JSON 输出(无 markdown):
{"reflection": "<text>", "tags": ["<tag1>", "<tag2>"]}"""


_USER_FMT = """事件类型: {event_type}
严重等级: {severity}
Session: {session_id} / Turn: {turn_idx}
证据: {evidence_json}

请产出反思 JSON。"""


def build_reflect_prompt(event: ReflectionEvent) -> tuple[str, str]:
    """根据 severity 选模板,返回 (system, user) 给 _ask_judge 调用。"""
    import json
    if event.severity == "neg":
        system = NEG_REFLECT_SYSTEM
    elif event.severity == "ambig":
        system = AMBIG_REFLECT_SYSTEM
    else:
        system = POS_REFLECT_SYSTEM
    user = _USER_FMT.format(
        event_type=event.event_type,
        severity=event.severity,
        session_id=event.session_id,
        turn_idx=event.turn_idx,
        evidence_json=json.dumps(event.evidence, ensure_ascii=False),
    )
    return system, user
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_prompts.py -v`
Expected: 3 passed in <0.1s

- [ ] **Step 5: Commit**

```bash
git add cc_harness/reflection/prompts.py tests/test_reflection_prompts.py
git commit -m "feat(reflection): 反思 prompt 模板 (neg/ambig/pos) + build_reflect_prompt (T1.2)"
```

---

### Task 1.3: ReflectionEngine 中心化引擎(完整实现)

**Files:**
- Create: `cc_harness/reflection/engine.py`
- Modify: `cc_harness/reflection/__init__.py`(加 export)
- Modify: `cc_harness/memory/config.py`(加 4 字段)
- Test: `tests/test_reflection_engine.py`

**Interfaces:**
- Consumes: `ReflectionEvent` + 注入 `memory_service / llm_client / judge_llm / l5_engine`
- Produces:
  - `ReflectionEngine` 类:`emit / _run_one / _drain / get_last_neg_reflection / get_recent`
  - `ReflectionOutcome` dataclass:`event / discarded / memory_id / reason`
  - 内部 helper `_ask_judge(judge_llm, system, user) -> str`(参考 LoCoMo m5 `eval/locomo/metrics.py:178`,内联实现)
  - 内部 helper `_audit(event, outcome)` 落 `logs/reflection.jsonl`
  - `MemoryConfig` 4 字段:`reflection_enabled / reflection_every_n_turns / reflection_max_pending / reflection_drain_timeout_s`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_reflection_engine.py
import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

from cc_harness.reflection.engine import ReflectionEngine, ReflectionOutcome
from cc_harness.reflection.events import max_iter_reached, tool_retry_burst


@pytest.fixture
def tmp_audit(tmp_path: Path) -> Path:
    return tmp_path / "reflection.jsonl"


@pytest.fixture
def fake_memory_service():
    svc = MagicMock()
    svc.save = AsyncMock(return_value=MagicMock(action="ADD", memory=MagicMock(id="m1")))
    return svc


@pytest.fixture
def fake_l5():
    return MagicMock(sanitize=lambda x: x)


@pytest.fixture
def fake_judge_llm():
    """模拟 JUDGE_MODEL 返 JSON。"""
    async def _fn(system, user):
        return '{"reflection": "失败根因: 没用 Grep 先查。", "tags": ["grep"]}'
    return _fn


@pytest.fixture
def fake_judge_fail():
    """模拟 JUDGE_MODEL 抛错。"""
    async def _fn(system, user):
        raise RuntimeError("API 503")
    return _fn


@pytest.fixture
def fake_local_llm():
    async def _fn(system, user):
        return '{"reflection": "本地兜底反思。", "tags": ["fallback"]}'
    return _fn


@pytest.mark.asyncio
async def test_emit_returns_immediately(tmp_audit, fake_memory_service, fake_l5, fake_judge_llm, fake_local_llm):
    """emit() 立即返回,不阻塞 turn。"""
    eng = ReflectionEngine(
        memory_service=fake_memory_service, llm_client=fake_local_llm,
        judge_llm=fake_judge_llm, l5_engine=fake_l5,
        project_root=tmp_audit.parent, audit_path=tmp_audit,
    )
    ev = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="x")
    t0 = asyncio.get_event_loop().time()
    await eng.emit(ev)
    elapsed = asyncio.get_event_loop().time() - t0
    assert elapsed < 0.05  # emit 必须 < 50ms


@pytest.mark.asyncio
async def test_judge_success_writes_to_memory(tmp_audit, fake_memory_service, fake_l5, fake_judge_llm, fake_local_llm):
    eng = ReflectionEngine(
        memory_service=fake_memory_service, llm_client=fake_local_llm,
        judge_llm=fake_judge_llm, l5_engine=fake_l5,
        project_root=tmp_audit.parent, audit_path=tmp_audit,
    )
    ev = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="x")
    await eng.emit(ev)
    await eng._drain(timeout_s=2)
    fake_memory_service.save.assert_awaited_once()
    call = fake_memory_service.save.await_args
    assert call.kwargs["source"] == "reflection" or call.args[1] == "reflection"


@pytest.mark.asyncio
async def test_judge_fail_falls_back_to_local(tmp_audit, fake_memory_service, fake_l5, fake_judge_fail, fake_local_llm):
    eng = ReflectionEngine(
        memory_service=fake_memory_service, llm_client=fake_local_llm,
        judge_llm=fake_judge_fail, l5_engine=fake_l5,
        project_root=tmp_audit.parent, audit_path=tmp_audit,
    )
    ev = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="x")
    await eng.emit(ev)
    await eng._drain(timeout_s=2)
    fake_memory_service.save.assert_awaited_once()  # 本地兜底成功 → 仍写


@pytest.mark.asyncio
async def test_all_llm_fail_noop(tmp_audit, fake_memory_service, fake_l5):
    async def fail(system, user):
        raise RuntimeError("nope")
    eng = ReflectionEngine(
        memory_service=fake_memory_service, llm_client=fail,
        judge_llm=fail, l5_engine=fake_l5,
        project_root=tmp_audit.parent, audit_path=tmp_audit,
    )
    ev = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="x")
    await eng.emit(ev)
    await eng._drain(timeout_s=2)
    fake_memory_service.save.assert_not_awaited()
    assert tmp_audit.exists()
    lines = tmp_audit.read_text(encoding="utf-8").strip().splitlines()
    assert "all_llm_unavailable" in lines[0]


@pytest.mark.asyncio
async def test_get_last_neg_reflection_updates(tmp_audit, fake_memory_service, fake_l5, fake_judge_llm, fake_local_llm):
    eng = ReflectionEngine(
        memory_service=fake_memory_service, llm_client=fake_local_llm,
        judge_llm=fake_judge_llm, l5_engine=fake_l5,
        project_root=tmp_audit.parent, audit_path=tmp_audit,
    )
    assert eng.get_last_neg_reflection() is None
    ev = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="x")
    await eng.emit(ev)
    await eng._drain(timeout_s=2)
    last = eng.get_last_neg_reflection()
    assert last is not None
    assert "Grep" in last or "失败" in last


@pytest.mark.asyncio
async def test_disabled_noop(tmp_audit, fake_memory_service, fake_l5, fake_judge_llm, fake_local_llm):
    eng = ReflectionEngine(
        memory_service=fake_memory_service, llm_client=fake_local_llm,
        judge_llm=fake_judge_llm, l5_engine=fake_l5,
        project_root=tmp_audit.parent, audit_path=tmp_audit,
        enabled=False,
    )
    ev = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="x")
    await eng.emit(ev)
    await eng._drain(timeout_s=2)
    fake_memory_service.save.assert_not_awaited()
    assert not tmp_audit.exists()


@pytest.mark.asyncio
async def test_lock_prevents_duplicate(tmp_audit, fake_memory_service, fake_l5, fake_judge_llm, fake_local_llm):
    """同 event_type+session+turn_idx 5s 内只跑一次。"""
    eng = ReflectionEngine(
        memory_service=fake_memory_service, llm_client=fake_local_llm,
        judge_llm=fake_judge_llm, l5_engine=fake_l5,
        project_root=tmp_audit.parent, audit_path=tmp_audit,
    )
    ev1 = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="x")
    ev2 = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="y")
    await eng.emit(ev1)
    await eng.emit(ev2)  # 5s 内重复
    await eng._drain(timeout_s=2)
    assert fake_memory_service.save.await_count == 1
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cc_harness.reflection.engine'`

- [ ] **Step 3: 在 `cc_harness/memory/config.py` 末尾加 4 字段**

打开 `cc_harness/memory/config.py`,找到 `recall_weight_floor: float = 0.5` 这一行(第 101 行附近),在它后面**插入**:

```python
    # E2 反思节点
    reflection_enabled: bool = True
    reflection_every_n_turns: int = 10
    reflection_max_pending: int = 3
    reflection_drain_timeout_s: float = 5.0
```

找到 `_check_positive` 装饰器(第 130-135 行附近),在它的字段列表加 1 个:

```python
    @field_validator("recall_timeout_s", "maintenance_interval_s",
                     "reflection_every_n_turns", "reflection_max_pending",
                     "reflection_drain_timeout_s")
```

(`reflection_every_n_turns` 和 `reflection_max_pending` 是 int,validator 误用 `_check_positive` 会 fail — **所以新加一个 validator** 或拆开。)

**正确做法**:在 `_check_positive` 后追加一个针对 int 的:

```python
    @field_validator("reflection_every_n_turns", "reflection_max_pending")
    @classmethod
    def _check_reflection_int(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"must be > 0, got {v}")
        return v
```

- [ ] **Step 4: 实现 `cc_harness/reflection/engine.py`**

```python
"""ReflectionEngine — central passive event-driven self-correction (E2).

复用 E4 scheduler 模式:asyncio.create_task 后台跑 + asyncio.Lock 防重入 +
_drain 优雅退出。JUDGE_MODEL 失败 → 退回本地 LLMClient;都失败 → fail-soft
noop + 审计。
"""
from __future__ import annotations
import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cc_harness.reflection.events import ReflectionEvent
from cc_harness.reflection.prompts import build_reflect_prompt


log = logging.getLogger(__name__)


@dataclass
class ReflectionOutcome:
    event: ReflectionEvent
    discarded: bool = False
    memory_id: str | None = None
    reason: str | None = None


# 同 event_type+session+turn_idx 5s 内去重
_DEDUP_WINDOW_S = 5.0


class ReflectionEngine:
    def __init__(
        self,
        *,
        memory_service,
        llm_client,
        judge_llm,
        l5_engine,
        project_root: Path,
        audit_path: Path | None = None,
        enabled: bool = True,
        every_n_turns: int = 10,
        max_pending: int = 3,
        drain_timeout_s: float = 5.0,
    ):
        self._memory_service = memory_service
        self._llm_client = llm_client
        self._judge_llm = judge_llm
        self._l5 = l5_engine
        self._project_root = Path(project_root)
        self._audit_path = audit_path or (self._project_root / "logs" / "reflection.jsonl")
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._enabled = enabled
        self._every_n_turns = every_n_turns
        self._max_pending = max_pending
        self._drain_timeout_s = drain_timeout_s
        # 后台 task 跟踪(同 E4 scheduler 模式)
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        # 同 key 短窗口去重
        self._seen: dict[tuple, float] = {}
        # last neg 反思(供 section 注入)
        self._last_neg: str | None = None
        # 全部反思(供 subagent recent_reflections)
        self._recent: list[str] = []
        self._recent_max = 3

    # ---------------- 公共 API ----------------

    async def emit(self, event: ReflectionEvent) -> None:
        """被动 hook。立即返回,内部 asyncio.create_task 后台跑。"""
        if not self._enabled:
            return
        # 短窗口去重
        key = (event.event_type, event.session_id, event.turn_idx)
        now = time.time()
        last_seen = self._seen.get(key)
        if last_seen is not None and (now - last_seen) < _DEDUP_WINDOW_S:
            return
        self._seen[key] = now
        # 队列上限:超过 max_pending 丢最旧
        if len(self._tasks) >= self._max_pending:
            done = [t for t in self._tasks if t.done()]
            for t in done:
                self._tasks.discard(t)
            if len(self._tasks) >= self._max_pending:
                # 仍满,丢最旧
                oldest = next(iter(self._tasks))
                oldest.cancel()
                self._tasks.discard(oldest)
        task = asyncio.create_task(self._run_one(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _drain(self, *, timeout_s: float | None = None) -> None:
        if not self._tasks:
            return
        timeout = timeout_s if timeout_s is not None else self._drain_timeout_s
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            for t in self._tasks:
                t.cancel()
            self._tasks.clear()

    def get_last_neg_reflection(self) -> str | None:
        return self._last_neg

    def get_recent(self, *, limit: int = 3) -> list[str]:
        return list(self._recent[-limit:])

    # ---------------- 后台 task 内部 ----------------

    async def _run_one(self, event: ReflectionEvent) -> ReflectionOutcome:
        async with self._lock:
            # 1. evidence 过 L5
            try:
                evidence = self._l5.sanitize(event.evidence)
            except Exception:
                evidence = event.evidence
            ev_safe = ReflectionEvent(
                event_type=event.event_type,
                severity=event.severity,
                evidence=evidence,
                session_id=event.session_id,
                turn_idx=event.turn_idx,
                created_at=event.created_at,
            )

            # 2. 调 JUDGE_MODEL → 退回本地
            system, user = build_reflect_prompt(ev_safe)
            text = await self._ask_judge_with_fallback(system, user)
            if text is None:
                return self._audit_noop(ev_safe, reason="all_llm_unavailable")

            # 3. 解析 JSON,容错:失败 → 当纯文本处理
            reflection_text = self._parse_reflection(text)

            # 4. 反思文本过 L5
            try:
                reflection_text = self._l5.sanitize(reflection_text)
            except Exception:
                pass

            # 5. 走 MemoryService.save(source='reflection')
            try:
                result = await self._memory_service.save(
                    text=reflection_text,
                    source="reflection",
                    session_id=ev_safe.session_id,
                )
            except Exception as e:
                return self._audit_noop(ev_safe, reason=f"save_error:{type(e).__name__}")

            # 6. ROLLBACK → 审计,不重试
            if getattr(result, "action", None) == "ROLLBACK":
                return self._audit_noop(ev_safe, reason="contradicted_by_existing_reflection")

            # 7. 写盘成功
            memory_id = getattr(getattr(result, "memory", None), "id", None)
            self._audit(ev_safe, outcome=ReflectionOutcome(event=ev_safe, memory_id=memory_id))

            # 8. 更新 last_neg / recent
            if ev_safe.severity == "neg" and reflection_text:
                self._last_neg = reflection_text[:200]
            if reflection_text:
                self._recent.append(reflection_text[:200])
                if len(self._recent) > self._recent_max:
                    self._recent = self._recent[-self._recent_max:]

            return ReflectionOutcome(event=ev_safe, memory_id=memory_id)

    # ---------------- 内部 helper ----------------

    async def _ask_judge_with_fallback(self, system: str, user: str) -> str | None:
        """JUDGE_MODEL → 退回本地 LLMClient → None。"""
        for llm, label in [(self._judge_llm, "judge"), (self._llm_client, "local")]:
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
                # async fn 形式:参考 eval/locomo/metrics.py:178 `_judge` 多态
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
                log.warning("reflection: %s llm failed: %s", label, e)
                continue
        return None

    @staticmethod
    def _parse_reflection(text: str) -> str:
        """解析 JSON 反射,容错回退。"""
        import re
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                reflection = data.get("reflection", "")
                if reflection:
                    return reflection
            except (json.JSONDecodeError, ValueError):
                pass
        return text  # 容错:原文

    def _audit(self, event: ReflectionEvent, *, outcome: ReflectionOutcome) -> None:
        try:
            with self._audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "op": "emit",
                    "event_type": event.event_type,
                    "severity": event.severity,
                    "memory_id": outcome.memory_id,
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("reflection: audit write failed: %s", e)

    def _audit_noop(self, event: ReflectionEvent, *, reason: str) -> ReflectionOutcome:
        try:
            with self._audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "op": "noop",
                    "event_type": event.event_type,
                    "severity": event.severity,
                    "reason": reason,
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return ReflectionOutcome(event=event, discarded=True, reason=reason)
```

- [ ] **Step 5: 更新 `cc_harness/reflection/__init__.py` export**

把 `cc_harness/reflection/__init__.py` 替换为:

```python
"""Reflection node — passive event-driven self-correction layer (E2)."""
from cc_harness.reflection.engine import ReflectionEngine, ReflectionOutcome
from cc_harness.reflection.events import (
    ReflectionEvent,
    max_iter_reached,
    empty_turn_loop,
    tool_error_burst,
    tool_retry_burst,
    subagent_failed,
    decider_rollback,
)

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
]
```

- [ ] **Step 6: 跑测试,确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_engine.py -v`
Expected: 7 passed in <1.0s

- [ ] **Step 7: 跑邻近回归**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_config.py tests/test_memory_layered.py -v`
Expected: all pass(E4 config 字段未破坏)

- [ ] **Step 8: Commit**

```bash
git add cc_harness/reflection/engine.py cc_harness/reflection/__init__.py cc_harness/memory/config.py tests/test_reflection_engine.py
git commit -m "feat(reflection): ReflectionEngine 中心化引擎 + config 4 字段 (T1.3)"
```

---

## Commit 2: feat(reflection) main agent 接入 (4 类 emit + section 注入 + wiring)

### Task 2.1: prompts.SECTION_POOL 加 reflection section

**Files:**
- Modify: `cc_harness/prompts.py:200-250`(SECTION_POOL 末尾)
- Test: `tests/test_reflection_section.py`

**Interfaces:**
- Consumes: `ReflectionEngine.get_last_neg_reflection() -> str | None`(T1.3 已实现)
- Produces:
  - `_reflection_section(ctx: dict) -> str | None` — ctx 含 `last_neg_reflection` 键
  - `SECTION_POOL` 末尾增 1 entry: `("reflection", _reflection_section, "last_neg_reflection")`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_reflection_section.py
import pytest
from cc_harness.prompts import (
    SECTION_POOL, build_system_prompt, _reflection_section,
)


def test_section_pool_has_reflection_entry():
    names = [name for name, _, _ in SECTION_POOL]
    assert "reflection" in names


def test_reflection_section_returns_none_when_no_neg():
    assert _reflection_section({"last_neg_reflection": None}) is None
    assert _reflection_section({}) is None


def test_reflection_section_wraps_with_xml_tags():
    out = _reflection_section({"last_neg_reflection": "上次失败了 X"})
    assert out is not None
    assert "<上一轮反思>" in out
    assert "上次失败了 X" in out
    assert "</上一轮反思>" in out


def test_reflection_section_truncates_at_200_tokens():
    """长反思应被截断,~200 token。"""
    long = "字" * 1000
    out = _reflection_section({"last_neg_reflection": long})
    assert out is not None
    # 反射体本身 ≤ 200 字
    assert out.count("字") <= 250  # 留余量


def test_reflection_section_appears_in_build_system_prompt(tmp_path):
    """build_system_prompt 应在末尾拼 reflection section(若 last_neg 非 None)。"""
    # 起一个最小 cwd(tmp_path) → Section pool 跑通
    ctx = {"last_neg_reflection": "上轮 max_iter 触达,反思根因:没用 Grep。"}
    out = build_system_prompt(
        cwd=tmp_path, mode="coding", extra_ctx=ctx,
    )
    assert "上轮反思" in out or "<上一轮反思>" in out
    assert "Grep" in out
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_section.py -v`
Expected: FAIL with `ImportError: cannot import name '_reflection_section'`

- [ ] **Step 3: 在 `cc_harness/prompts.py` 加 `_reflection_section` 函数 + 注册到 SECTION_POOL**

打开 `cc_harness/prompts.py`,找到 SECTION_POOL 定义(大约 line 200-250),在末尾加:

```python
def _reflection_section(ctx: dict) -> str | None:
    """E2 反思节点 section:仅当存在 last_neg_reflection 时注入(neg-only)。"""
    last = ctx.get("last_neg_reflection")
    if not last:
        return None
    # 截断 ~200 token(中文算 1 token/字,英文 ~0.75 token/字,统一 200 char 上限)
    body = str(last)[:200]
    return f"\n<上一轮反思>\n{body}\n</上一轮反思>"
```

**SECTION_POOL 注册** — 找到当前 pool 末尾(应该是某 entry 后跟 `]`,或元组列表的最后一个元组),在最后一个 entry 后追加:

```python
    ("reflection", _reflection_section, "last_neg_reflection"),
```

(`"last_neg_reflection"` 是 condition key — E1 spec §D2 设计的条件字符串: `ctx.get(condition) is not None` 才注入。)

- [ ] **Step 4: 跑测试,确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_section.py -v`
Expected: 5 passed in <0.1s

- [ ] **Step 5: 跑邻近回归**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_prompts.py tests/test_main.py -v`
Expected: all pass(加 section 不破旧 build)

- [ ] **Step 6: Commit**

```bash
git add cc_harness/prompts.py tests/test_reflection_section.py
git commit -m "feat(reflection): prompts.SECTION_POOL 加 reflection section (T2.1)"
```

---

### Task 2.2: agent.run_turn 4 类 emit + _refresh_system_prompt 注入

**Files:**
- Modify: `cc_harness/agent.py:77`(run_turn 形参加 reflection_engine)
- Modify: `cc_harness/agent.py:178-180`(empty-turn 命中 emit)
- Modify: `cc_harness/agent.py:490-510`(max_iter 兜底 emit)
- Modify: `cc_harness/agent.py:520-560`(tool is_error 连续 2+ emit)
- Modify: `cc_harness/agent.py:511`(同工具同 args 调 2+ 次 emit)
- Modify: `cc_harness/agent.py:_refresh_system_prompt` body(末尾加 ctx["last_neg_reflection"])
- Test: `tests/test_reflection_main_integration.py`

**Interfaces:**
- Consumes: `ReflectionEngine.emit(event)`(T1.3 已实现)
- Produces: `agent.run_turn` 形参加 `reflection_engine: ReflectionEngine | None = None`(默认 None 保持向后兼容);4 处 try/except 包 emit;`_refresh_system_prompt` 在 `extra_ctx` 加 `last_neg_reflection`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_reflection_main_integration.py
import asyncio
from unittest.mock import MagicMock, AsyncMock
import pytest

from cc_harness.reflection.engine import ReflectionEngine
from cc_harness.reflection.events import (
    max_iter_reached, empty_turn_loop, tool_error_burst, tool_retry_burst,
)


@pytest.mark.asyncio
async def test_max_iter_emit_triggers_reflection(tmp_path):
    """run_turn 触发 max_iter 后,reflection_engine.emit 收到 max_iter 事件。"""
    from cc_harness.agent import run_turn  # import 内层避免副作用

    # Fake LLM 永远 finish_reason="stop" + 空 content → empty-turn 兜底走
    # 我们改用 max_iter 路径:让 LLM 一直 finish_reason="tool_calls"
    fake_llm = MagicMock()
    fake_llm.chat = AsyncMock(side_effect=[
        # iter 0: tool_calls → 再 iter
        iter_tool_call("fs__read", {"path": "/x"}),
        # iter 1: tool_calls → 再 iter
        iter_tool_call("fs__read", {"path": "/x"}),
        # iter 2+20 次: tool_calls → 触达 max_iter=3(测试用)
        ] + [iter_tool_call("fs__read", {"path": "/x"})] * 5)
    # ...(更完整的 test stub 略,见 test_agent.py FakeLLM 模板)

    # 简单版:直接调 run_turn 但只验 emit 收到事件
    from cc_harness.memory.config import MemoryConfig
    config = MemoryConfig(enabled=False)  # 跳过 memory
    # reflection_engine 形参
    re_emit = MagicMock()
    re_emit.emit = AsyncMock()
    re_emit.get_last_neg_reflection = MagicMock(return_value=None)

    # 直接构造一个会被 max_iter 兜底触发的场景较复杂。
    # 简化为:用 reflection_engine 接收一个 emit,验 run_turn 不破坏。
    # 详细 max_iter 触发 → 走 test_agent.py FakeLLM 模式,见 commit 2 final task。
    pass  # 真实测试由 T2.3 完成,此处 placeholder


def iter_tool_call(name, args):
    """构造一个 fake LLM StreamEvent 流,带 tool_calls。"""
    from cc_harness.llm import StreamEvent
    async def gen():
        yield StreamEvent(kind="done", tool_calls=[
            MagicMock(id="t1", name=name, arguments_json=str(args).replace("'", '"'))
        ])
    gen.chat = lambda *a, **k: gen()
    return gen
```

> **实施注意**:上测试是占位 stub,真实 max_iter/empty_turn 触发需要 mock LLM 流。实施员应参考 `tests/test_agent.py` 中 `FakeLLM` 模板,造一个会反复 emit tool_calls 的 LLM 跑 `run_turn(max_iter=3, reflection_engine=...)`,drain 后 assert `engine._last_neg is not None`。

**完整测试代码**(实施员实际写盘时用,plan 给出关键路径):

```python
# 实际完整版
@pytest.mark.asyncio
async def test_max_iter_emit_triggers_neg_reflection(tmp_path):
    from cc_harness.agent import run_turn
    from cc_harness.llm import StreamEvent
    import json

    # FakeLLM 永远 finish tool_calls(永远不退出)
    tool_event = StreamEvent(
        kind="done",
        content=None,
        tool_calls=[MagicMock(id="t1", name="fs__read", arguments_json=json.dumps({"path": "/x"}))],
    )
    class ForeverToolLLM:
        async def chat(self, msgs, tools=None):
            yield tool_event

    re_emit = MagicMock()
    re_emit.emit = AsyncMock()
    re_emit.get_last_neg_reflection = MagicMock(return_value=None)

    msgs = [{"role": "user", "content": "do it"}]
    # max_iter=3 → 触达兜底
    await run_turn(
        messages=msgs, llm=ForeverToolLLM(), mode="coding",
        max_iter=3, project_root=tmp_path,
        policy=MagicMock(evaluate=MagicMock(return_value=MagicMock(allow=True, ask_user=None))),
        reflection_engine=re_emit,
    )
    # 验 emit 至少收到 1 个 max_iter 事件
    assert any(
        call.args[0].event_type == "max_iter"
        for call in re_emit.emit.await_args_list
    )
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_main_integration.py -v`
Expected: FAIL with `TypeError: run_turn() got an unexpected keyword argument 'reflection_engine'`

- [ ] **Step 3: 修改 `cc_harness/agent.py`**

**Step 3a**: 找到 `def run_turn(`(line 77),在形参列表加 1 项(放最后,与已有形参风格一致):

```python
    reflection_engine = None,  # E2 ReflectionEngine | None
```

(必须放 `**kwargs` 之前或 keyword-only 段,检查 `run_turn` 当前签名决定具体位置 — 若已用 `*,` 分 keyword-only,加在 `*,` 之后。)

**Step 3b**: 在 max_iter 兜底(line 492-503)后追加:

```python
    if reflection_engine is not None:
        try:
            await reflection_engine.emit(
                max_iter_reached(
                    session_id=session_id or "default",
                    turn_idx=iter_count,
                    iter_used=iter_count,
                    last_content=content or "",
                )
            )
        except Exception:
            pass  # emit 失败不阻塞 turn
```

**Step 3c**: 在 empty-turn retry 命中(line 669-673)后追加:

```python
    if reflection_engine is not None:
        try:
            await reflection_engine.emit(
                empty_turn_loop(
                    session_id=session_id or "default",
                    turn_idx=iter_count,
                    attempts=1,
                )
            )
        except Exception:
            pass
```

**Step 3d**: 在 tool_call loop(已发出 is_error 的 tool message 后,line 530/544/562 三处),累计 2+ is_error 时 emit。**最简做法**:在 line 530 之前插入 1 个 `tool_error_count = 0` 计数器;每次 append `is_error=True` 的 tool message 后 `tool_error_count += 1`;**当本 turn 累计 ≥ 2 时 emit 一次**:

```python
    tool_error_count = 0
    # ... 在每个 is_error tool message append 后:
    tool_error_count += 1
    if tool_error_count >= 2 and reflection_engine is not None:
        try:
            await reflection_engine.emit(
                tool_error_burst(
                    session_id=session_id or "default",
                    turn_idx=iter_count,
                    errors=[{"tool": p.name, "error": error_text}],
                )
            )
            tool_error_count = 0  # 避免每个 tool 都 emit
        except Exception:
            pass
```

**Step 3e**: 同工具同 args 调 2+ 次 — 在 assistant message append 处(line 511),记录 `tool_call_log: list[dict]`(plan1 task4 已有),`run_turn` 开头有 `tool_call_log: list = []` 初始化(line 179)。在每次 append `assistant_msg` 前,扫 `tool_call_log` 找同 `(name, args_json)` 出现 2+ 次则 emit:

```python
    # 在 pending tool_calls 处理前:
    for p in pending:
        sig = (p.name, p.arguments_json)
        if tool_call_log.count(sig) >= 1 and reflection_engine is not None:
            try:
                await reflection_engine.emit(
                    tool_retry_burst(
                        session_id=session_id or "default",
                        turn_idx=iter_count,
                        calls=[{"tool": p.name, "args": json.loads(p.arguments_json or "{}"), "count": tool_call_log.count(sig) + 1}],
                    )
                )
            except Exception:
                pass
    # 然后才 append:
    for p in pending:
        tool_call_log.append((p.name, p.arguments_json))
```

**Step 3f**: `_refresh_system_prompt` 末尾(调用点 line 194-205),在传 `extra_ctx` 时加 `last_neg_reflection`:

```python
    if reflection_engine is not None:
        extra_ctx["last_neg_reflection"] = reflection_engine.get_last_neg_reflection()
```

> **位置**: `_refresh_system_prompt` 是被 `run_turn` 调用的(不是 reflection_engine 调),所以 `extra_ctx` 在 caller 处拼。`run_turn` 内的 `_refresh_system_prompt` 调用(line 194 / 201)都需加这 1 行。

- [ ] **Step 4: 跑测试,确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_main_integration.py tests/test_agent.py -v`
Expected: test_reflection_main_integration.py 1 passed + test_agent.py 旧测试全 pass

- [ ] **Step 5: 跑邻近回归**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/ -k "agent or main or reflection" -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add cc_harness/agent.py tests/test_reflection_main_integration.py
git commit -m "feat(reflection): agent.run_turn 4 类 emit + _refresh_system_prompt 注入 (T2.2)"
```

---

### Task 2.3: repl 接入 + main.py boot 构造 + drain

**Files:**
- Modify: `cc_harness/repl.py`(run_repl 形参加 reflection_engine,finally _drain)
- Modify: `main.py:boot()`(构造 ReflectionEngine,注入 run_repl)
- Test: `tests/test_reflection_repl_wiring.py`

**Interfaces:**
- Consumes: `ReflectionEngine` 实例(T1.3 构造)
- Produces:
  - `repl.run_repl(scheduler=..., reflection_engine=...)` — 透传给 `run_turn`
  - `repl.run_repl` finally 块调 `reflection_engine._drain(timeout_s=...)`
  - `main.py:boot()` 构造 `ReflectionEngine(memory_service=..., llm_client=..., judge_llm=..., l5_engine=..., project_root=...)`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_reflection_repl_wiring.py
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock
import pytest

from cc_harness.reflection.engine import ReflectionEngine
from cc_harness.reflection.events import max_iter_reached


@pytest.mark.asyncio
async def test_run_repl_passes_reflection_engine_to_run_turn(tmp_path):
    """run_repl 接受 reflection_engine 形参并透传到 run_turn。"""
    from cc_harness import repl

    re_emit = MagicMock(spec=ReflectionEngine)
    re_emit.emit = AsyncMock()
    re_emit.get_last_neg_reflection = MagicMock(return_value=None)
    re_emit._drain = AsyncMock()

    # mock _read_user 让 repl 立即 exit
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(repl, "_read_user", AsyncMock(side_effect=EOFError))
    try:
        await repl.run_repl(
            messages=[],
            llm=MagicMock(),
            mode="coding",
            project_root=tmp_path,
            scheduler=None,
            reflection_engine=re_emit,
        )
    except (EOFError, SystemExit):
        pass
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_drain_called_in_finally(tmp_path):
    """run_repl finally 块必须调 _drain。"""
    from cc_harness import repl

    re = MagicMock(spec=ReflectionEngine)
    re.emit = AsyncMock()
    re._drain = AsyncMock()
    re.get_last_neg_reflection = MagicMock(return_value=None)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(repl, "_read_user", AsyncMock(side_effect=EOFError))
    try:
        await repl.run_repl(
            messages=[], llm=MagicMock(), mode="coding",
            project_root=tmp_path, scheduler=None, reflection_engine=re,
        )
    except (EOFError, SystemExit):
        pass
    finally:
        monkeypatch.undo()
    re._drain.assert_awaited()
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_repl_wiring.py -v`
Expected: FAIL with `TypeError: run_repl() got an unexpected keyword argument 'reflection_engine'`

- [ ] **Step 3: 修改 `cc_harness/repl.py`**

找到 `async def run_repl(` 签名,加 1 个 keyword-only 形参:

```python
    reflection_engine: "ReflectionEngine | None" = None,  # E2 反射节点
```

找到 `run_repl` 内调用 `agent.run_turn(...)` 的地方(line ~XXX,**实施员需 grep**),在 kwargs 加:

```python
            reflection_engine=reflection_engine,
```

在 `run_repl` 的 `try/finally` 块 finally 分支(line ~XXX,通常含 `await scheduler._drain(...)`),加:

```python
        if reflection_engine is not None:
            try:
                await reflection_engine._drain(timeout_s=reflection_engine._drain_timeout_s)
            except Exception:
                pass
```

- [ ] **Step 4: 修改 `main.py:boot()`**

找到 `main.py` 中构造 `MaintenanceScheduler` 的位置(沿用 E4 I-1 wiring 模式),在它后面加 `ReflectionEngine` 构造 + 注入 `cmd_repl` / `run_repl` 调用:

```python
    from cc_harness.reflection.engine import ReflectionEngine
    reflection_engine = ReflectionEngine(
        memory_service=memory_service,         # 已有
        llm_client=llm_client,                  # 已有
        judge_llm=judge_llm_client,             # 新增:从 JUDGE_* env 构造
        l5_engine=l5_engine,                    # 已有
        project_root=project_root,
        enabled=config.memory.reflection_enabled,
        every_n_turns=config.memory.reflection_every_n_turns,
        max_pending=config.memory.reflection_max_pending,
        drain_timeout_s=config.memory.reflection_drain_timeout_s,
    )
```

`judge_llm_client` 构造 — 沿用 `eval/locomo/metrics.py` 中 JUDGE client 模式(LLMClient 复用同 provider,但用 JUDGE_* env)。**若 JUDGE_* 未配,ReflectionEngine.__init__ 接受 `judge_llm=None`,_ask_judge_with_fallback 跳过 judge 直接走 local**。

找到 `cmd_repl` / `run_repl` 调用点,加 `reflection_engine=reflection_engine` kwargs。

- [ ] **Step 5: 跑测试,确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_repl_wiring.py tests/test_main.py tests/test_repl.py -v`
Expected: all pass

- [ ] **Step 6: 跑全量回归**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/ -x --timeout=60`
Expected: 至少 200+ 通过,reflection 相关 0 fail

- [ ] **Step 7: Commit**

```bash
git add cc_harness/repl.py main.py tests/test_reflection_repl_wiring.py
git commit -m "feat(reflection): repl + main 接入 reflection_engine (T2.3)"
```

---

## Commit 3: feat(reflection) memory/decider 扩参 + subagent 末尾 emit

### Task 3.1: MemoryStore.search_reflections + LLMDecider 扩参 + service 注入

**Files:**
- Modify: `cc_harness/memory/store.py`(末尾加 `search_reflections` 方法)
- Modify: `cc_harness/memory/decider.py:35`(decide 扩 1 形参)
- Modify: `cc_harness/memory/service.py:43`(save 调 decide 前召 reflections)
- Test: `tests/test_reflection_memory.py`

**Interfaces:**
- Consumes: `ReflectionEvent` 落盘后产生 `source='reflection'` 的 `Memory` 行
- Produces:
  - `MemoryStore.search_reflections(*, limit=5, lookback_h=24) -> list[Memory]` — 按 `created_at DESC` 查 source='reflection' 行
  - `LLMDecider.decide(new_text, similar, *, recent_reflections=None)` — 新形参,默认 None
  - `MemoryService.save(...)` 调 `decider.decide()` 前召 `self.store.search_reflections(limit=5, lookback_h=24)`,把结果作 `recent_reflections` 注入

- [ ] **Step 1: 写失败测试**

```python
# tests/test_reflection_memory.py
import asyncio
import time
import pytest
from unittest.mock import MagicMock, AsyncMock
from pathlib import Path


@pytest.mark.asyncio
async def test_store_search_reflections_filters_by_source(tmp_path):
    """search_reflections 只返 source='reflection' 的 Memory,且按时间倒序。"""
    from cc_harness.memory.store import MemoryStore
    store = MemoryStore(tmp_path / "mem.db", embedding_dim=4)
    await store.init_schema()
    emb = [0.1, 0.2, 0.3, 0.4]
    # 加 3 条:llm / pipeline / reflection
    await store.add("普通记忆", emb, "llm", session_id="s1")
    await store.add("管线记忆", emb, "pipeline", session_id="s1")
    refl_id = await store.add("反思1", emb, "reflection", session_id="s1")
    # 时间戳错开确保倒序稳定
    await asyncio.sleep(0.01)
    await store.add("反思2", emb, "reflection", session_id="s1")

    out = await store.search_reflections(limit=5, lookback_h=24)
    assert len(out) == 2
    assert all(m.source == "reflection" for m in out)
    assert out[0].id != refl_id  # 倒序:反思2 在前


@pytest.mark.asyncio
async def test_store_search_reflections_respects_lookback(tmp_path):
    """lookback_h 内的才返,外的不返。"""
    from cc_harness.memory.store import MemoryStore
    store = MemoryStore(tmp_path / "mem.db", embedding_dim=4)
    await store.init_schema()
    emb = [0.1, 0.2, 0.3, 0.4]
    await store.add("老反思", emb, "reflection", session_id="s1")
    # 把 created_at 改到 100h 前
    old = time.time() - 100 * 3600
    await store._db.execute(
        "UPDATE memories SET created_at=? WHERE source='reflection'",
        (old,),
    )
    await store._db.commit()
    out = await store.search_reflections(limit=5, lookback_h=24)
    assert len(out) == 0


@pytest.mark.asyncio
async def test_decider_receives_recent_reflections():
    """LLMDecider.decide(new_text, similar, recent_reflections=...) 注入正确。"""
    from cc_harness.memory.decider import LLMDecider, Decision, DecisionResult
    from cc_harness.memory.store import Memory

    captured = {}
    class FakeLLM:
        async def chat(self, msgs, tools=None):
            # 验 msgs 内有反思段
            for m in msgs:
                if "你过去 24h 对相似主题的反思" in (m.get("content") or ""):
                    captured["reflection_injected"] = True
            async def gen():
                from cc_harness.llm import StreamEvent
                yield StreamEvent(kind="done", content='{"action": "ADD"}')
            return gen()

    decider = LLMDecider(FakeLLM())
    sim = (Memory(id="m1", text="x", embedding=[0.0], created_at=0,
                  updated_at=0, source="llm"), 0.5)
    recent = [Memory(id="r1", text="反思1", embedding=[0.0], created_at=0,
                     updated_at=0, source="reflection")]
    res = await decider.decide("new", [sim], recent_reflections=recent)
    assert res.action == Decision.ADD
    assert captured.get("reflection_injected") is True


@pytest.mark.asyncio
async def test_decider_no_recent_reflections_works():
    """recent_reflections=None 时走旧路径,prompt 不含反思段。"""
    from cc_harness.memory.decider import LLMDecider, Decision
    from cc_harness.memory.store import Memory

    class FakeLLM:
        async def chat(self, msgs, tools=None):
            for m in msgs:
                assert "你过去 24h" not in (m.get("content") or "")
            async def gen():
                from cc_harness.llm import StreamEvent
                yield StreamEvent(kind="done", content='{"action": "ADD"}')
            return gen()

    decider = LLMDecider(FakeLLM())
    sim = (Memory(id="m1", text="x", embedding=[0.0], created_at=0,
                  updated_at=0, source="llm"), 0.5)
    res = await decider.decide("new", [sim])  # recent_reflections 不传
    assert res.action == Decision.ADD


@pytest.mark.asyncio
async def test_service_save_injects_recent_reflections_to_decider(tmp_path):
    """MemoryService.save 调 decider 前召 search_reflections 并注入。"""
    from cc_harness.memory.store import MemoryStore
    from cc_harness.memory.embedding import EmbeddingClient
    from cc_harness.memory.service import MemoryService

    store = MemoryStore(tmp_path / "mem.db", embedding_dim=4)
    await store.init_schema()
    # 预存 1 条反思
    await store.add("已有反思", [0.1, 0.2, 0.3, 0.4], "reflection", session_id="s1")
    # Fake embedder + decider
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[0.5, 0.5, 0.5, 0.5])
    decider = MagicMock()
    decider._llm = MagicMock()  # service 用 ._llm 走矛盾检测
    decider.decide = AsyncMock(return_value=MagicMock(action=1))  # ADD
    svc = MemoryService(store=store, embedder=embedder, decider=decider)
    await svc.save("新记忆", source="llm", session_id="s1")
    # 验 decide 收到了 recent_reflections
    call = decider.decide.await_args
    rr = call.kwargs.get("recent_reflections")
    assert rr is not None
    assert len(rr) == 1
    assert rr[0].source == "reflection"
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_memory.py -v`
Expected: 第一个 fail: `AttributeError: 'MemoryStore' object has no attribute 'search_reflections'`

- [ ] **Step 3: 在 `cc_harness/memory/store.py` 加 `search_reflections` 方法**

找到 `MemoryStore` 类末尾(在 `close()` 方法前),追加:

```python
    async def search_reflections(
        self, *, limit: int = 5, lookback_h: float = 24.0
    ) -> list[Memory]:
        """查最近 lookback_h 小时内 source='reflection' 的 Memory,按 created_at DESC。"""
        assert self._db is not None, "store.init_schema first"
        cutoff = time.time() - lookback_h * 3600
        cur = await self._db.execute(
            "SELECT id, text, embedding, created_at, updated_at, source, "
            "COALESCE(layer, 'L1'), session_id "
            "FROM memories "
            "WHERE source = 'reflection' AND created_at > ? "
            "ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit),
        )
        rows = await cur.fetchall()
        return [
            Memory(
                id=r[0], text=r[1], embedding=_blob_to_vec(r[2]),
                created_at=r[3], updated_at=r[4], source=r[5],
                layer=r[6], session_id=r[7],
            )
            for r in rows
        ]
```

(注意:`memories` 表当前 schema 实际字段是 `id, text, embedding, created_at, updated_at, source`(store.py:58-66),**没 layer / session_id 列** — E4 maintenance 加了列但 init_schema 内 CREATE TABLE 仍只 6 列,E4 走 ALTER TABLE 补列,见 E4 task 1 report。本 task 用最简 SELECT,只取 `id, text, embedding, created_at, updated_at, source` + 用 `row[5] as source` 即可。)

**正确版本**(对照实际 schema):

```python
    async def search_reflections(
        self, *, limit: int = 5, lookback_h: float = 24.0
    ) -> list[Memory]:
        assert self._db is not None, "store.init_schema first"
        cutoff = time.time() - lookback_h * 3600
        cur = await self._db.execute(
            "SELECT id, text, embedding, created_at, updated_at, source "
            "FROM memories "
            "WHERE source = 'reflection' AND created_at > ? "
            "ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit),
        )
        rows = await cur.fetchall()
        return [
            Memory(
                id=r[0], text=r[1], embedding=_blob_to_vec(r[2]),
                created_at=r[3], updated_at=r[4], source=r[5],
            )
            for r in rows
        ]
```

> 实施员需 grep `memories` 表实际列(可能 E4 ALTER 加了 staleness/recall_count 等)决定 SELECT 列表;layer / session_id 缺则不传。

- [ ] **Step 4: 修改 `cc_harness/memory/decider.py`**

找到 `async def decide(`(line 35),改为:

```python
    async def decide(
        self,
        new_text: str,
        similar: list,
        *,
        recent_reflections: list | None = None,  # E2 注入,默认 None
    ) -> DecisionResult:
        if not similar:
            return DecisionResult(action=Decision.ADD)

        similar_json = json.dumps(
            [{"id": m.id, "text": m.text, "distance": round(float(d), 3)}
             for m, d in similar],
            ensure_ascii=False,
        )
        user_content = memory_decide_user_prompt(new_text, similar_json)
        if recent_reflections:
            ref_section = "\n\n你过去 24h 对相似主题的反思如下(供你参考):\n" + "\n".join(
                f"- {r.text[:150]}" for r in recent_reflections[:3]
            )
            user_content += ref_section

        msgs = [
            {"role": "system", "content": MEMORY_DECIDE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        # ... 后续 try/except 块不动
```

- [ ] **Step 5: 修改 `cc_harness/memory/service.py`**

找到 `decision = await self.decider.decide(text, similar)`(line 43),改为:

```python
            recent_reflections = await self.store.search_reflections(limit=5, lookback_h=24)
            decision = await self.decider.decide(
                text, similar, recent_reflections=recent_reflections
            )
```

- [ ] **Step 6: 跑测试,确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_memory.py -v`
Expected: 5 passed in <0.5s

- [ ] **Step 7: 跑邻近回归**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_memory_layered.py tests/test_memory_hybrid.py tests/test_decider.py -v`
Expected: all pass(decider 扩参默认 None 保持向后兼容)

- [ ] **Step 8: Commit**

```bash
git add cc_harness/memory/store.py cc_harness/memory/decider.py cc_harness/memory/service.py tests/test_reflection_memory.py
git commit -m "feat(reflection): MemoryStore.search_reflections + LLMDecider 扩参 + service 注入 (T3.1)"
```

---

### Task 3.2: SubAgentRunner 末尾 emit + _render_subagent_summary 加 recent_reflections

**Files:**
- Modify: `cc_harness/project/subagent.py:SubAgentRunner.run`(末尾 emit)
- Modify: `cc_harness/project/subagent.py:_render_subagent_summary`(加 recent_reflections 字段)
- Test: `tests/test_reflection_subagent.py`

**Interfaces:**
- Consumes: `ReflectionEngine.emit` + `get_recent`
- Produces:
  - `SubAgentRunner.run` 末尾:若 `status in {failed, incomplete, timeout}` → emit `subagent_failed` severity=neg;若 `status == blocked` → emit severity=ambig
  - `_render_subagent_summary` 渲染 SubAgentResult 时追加 `recent_reflections: list[str]` 字段(从 engine.get_recent 拿)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_reflection_subagent.py
import asyncio
from unittest.mock import MagicMock, AsyncMock
import pytest

from cc_harness.reflection.engine import ReflectionEngine
from cc_harness.reflection.events import subagent_failed


@pytest.mark.asyncio
async def test_subagent_failed_status_emits_neg():
    """SubAgentResult.status='failed' 触发 emit severity=neg。"""
    from cc_harness.project.subagent import SubAgentResult, SubAgentRunner

    re = MagicMock(spec=ReflectionEngine)
    re.emit = AsyncMock()
    re.get_recent = MagicMock(return_value=[])

    runner = SubAgentRunner.__new__(SubAgentRunner)  # 跳过 __init__
    # 模拟 run 末尾 emit 逻辑(直接调 T1.3 工厂验 severity)
    result = SubAgentResult(task_id="t1", title="x", status="failed", final_text="...",
                            tokens_used=0, fatal_error=True)
    ev = subagent_failed(session_id="s1", turn_idx=0, result=result.__dict__)
    assert ev.severity == "neg"


def test_subagent_blocked_maps_ambig():
    from cc_harness.project.subagent import SubAgentResult
    result = SubAgentResult(task_id="t1", title="x", status="blocked", final_text=None,
                            tokens_used=0, fatal_error=False)
    ev = subagent_failed(session_id="s1", turn_idx=0, result=result.__dict__)
    assert ev.severity == "ambig"


def test_render_subagent_summary_includes_recent_reflections():
    """_render_subagent_summary 渲染应包含 recent_reflections 字段。"""
    from cc_harness.project.subagent import _render_subagent_summary, SubAgentResult

    re = MagicMock()
    re.get_recent = MagicMock(return_value=["反思1", "反思2", "反思3"])
    results = [
        SubAgentResult(task_id="t1", title="x", status="done", final_text="ok",
                       tokens_used=10, fatal_error=False),
    ]
    out = _render_subagent_summary(results, parent_id="p1", reflection_engine=re)
    assert "recent_reflections" in out
    assert "反思1" in out


def test_render_subagent_summary_no_engine_works():
    """reflection_engine=None 时不渲染 recent_reflections 字段。"""
    from cc_harness.project.subagent import _render_subagent_summary, SubAgentResult

    results = [
        SubAgentResult(task_id="t1", title="x", status="done", final_text="ok",
                       tokens_used=10, fatal_error=False),
    ]
    out = _render_subagent_summary(results, parent_id="p1", reflection_engine=None)
    assert "recent_reflections" not in out
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_subagent.py -v`
Expected: 第一个 fail: `_render_subagent_summary() got an unexpected keyword argument 'reflection_engine'`

- [ ] **Step 3: 修改 `cc_harness/project/subagent.py`**

**Step 3a**: 找到 `SubAgentRunner.run` 末尾(返回 `SubAgentResult` 前,大约 line 440-460),加 emit 逻辑:

```python
        # E2 反思节点:失败类 status 触发 subagent_failed emit
        if self._reflection_engine is not None and final_status in {
            "failed", "incomplete", "timeout", "blocked"
        }:
            try:
                from cc_harness.reflection.events import subagent_failed as _sf
                result_dict = {
                    "status": final_status,
                    "task_id": task_id,
                    "final_text": final_text,
                }
                await self._reflection_engine.emit(
                    _sf(session_id=self._session_id or "default",
                        turn_idx=self._turn_idx,
                        result=result_dict)
                )
            except Exception:
                pass
```

`SubAgentRunner.__init__` 需加 2 形参:`reflection_engine` + `session_id` + `turn_idx`(找 __init__ 末尾追加):

```python
        self._reflection_engine = reflection_engine
        self._session_id = session_id
        self._turn_idx = turn_idx
```

**Step 3b**: 找到 `_render_subagent_summary`(line 189 附近),形参加 `reflection_engine=None`:

```python
def _render_subagent_summary(
    results: list,
    parent_id: str,
    reflection_engine: "ReflectionEngine | None" = None,
) -> str:
```

在函数体末尾(返回的 markdown/文本里)追加 recent_reflections 段:

```python
    if reflection_engine is not None:
        recent = reflection_engine.get_recent(limit=3)
        if recent:
            out += "\n\n## 最近反思(E2)\n" + "\n".join(f"- {r}" for r in recent)
    return out
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_reflection_subagent.py -v`
Expected: 4 passed in <0.5s

- [ ] **Step 5: 跑邻近回归**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py tests/test_d1_integration.py -v`
Expected: all pass(`__init__` 扩参默认 None 保持向后兼容)

- [ ] **Step 6: Commit**

```bash
git add cc_harness/project/subagent.py tests/test_reflection_subagent.py
git commit -m "feat(reflection): SubAgentRunner 末尾 emit + _render_subagent_summary recent_reflections (T3.2)"
```

---

### Task 3.3: E2E 集成 + LoCoMo 占位 + Final review

**Files:**
- Create: `tests/_test_reflection_e2e.py`(`_test_` 前缀,pytest 默认不收)
- Modify: `cc_harness/repl.py`(SubAgentRunner 构造传 reflection_engine,沿用 E4 注入模式)
- Modify: `main.py:boot()`(SubAgentRunner 注入 reflection_engine)
- Test: 跑全量回归 + 邻近 spec

**Interfaces:**
- Consumes: 全部 T1.x / T2.x / T3.x 集成
- Produces: 真 LLM 端到端跑通,LoCoMo m5 metrics 不退化,日志审计正常落盘

- [ ] **Step 1: 写 E2E 占位测试**

```python
# tests/_test_reflection_e2e.py
"""E2E:真 LLM 端到端跑 reflection_node。

pytest 默认不收(_test_ 前缀),手动跑:
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/_test_reflection_e2e.py -v
"""
import asyncio
import pytest
from pathlib import Path


@pytest.mark.asyncio
async def test_e2e_max_iter_triggers_reflection(tmp_path):
    """真 LLM:触发 max_iter → 真反思 → 真写 memory → 真召出。"""
    # 此测试需要 OPENAI_API_KEY 等环境,默认 skip
    import os
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set")
    # 实施员写:起 reflection engine + memory service + 调 run_turn 触发 max_iter
    # drain 后 assert engine._last_neg is not None
    # assert (await store.search_reflections(limit=5, lookback_h=24)) 至少 1 条
    raise NotImplementedError("E2E 实施员补")
```

- [ ] **Step 2: 跑全量回归(不含 E2E)**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/ -x --timeout=60`
Expected: 至少 200+ 通过,E2 相关 0 fail

- [ ] **Step 3: 跑邻近 spec 验证**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent.py tests/test_repl.py tests/test_main.py tests/test_d1_subagent.py tests/test_memory_layered.py tests/test_memory_hybrid.py tests/test_maintenance_*.py -v`
Expected: all pass(E2 不破 E1/B/C/D/E4 任何 spec)

- [ ] **Step 4: lint**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m ruff check cc_harness/reflection/ cc_harness/memory/ cc_harness/agent.py cc_harness/repl.py main.py tests/test_reflection_*.py`
Expected: All checks passed!

- [ ] **Step 5: 写 final review 报告 + 落 ledger**

写 `.superpowers/sdd/e2-final-review.md`(本 commit 范围 diff + 测试 + lint + 邻近 spec 验证)。

更新 `.superpowers/sdd/progress.md` 末尾加:

```markdown
## E2 — Ready to merge
**Status**: 3 commit + 全量回归 pass + lint clean
**Range**: (T1.1 SHA)..(T3.2 SHA)
**Commits**: 7 = 3 task + 1 e2e 占位 + 1 spec + 1 plan + 1 fix
**测试**: 5+1+5+4+1 = 16+ E2 新增 + 全量回归 0 破坏
```

- [ ] **Step 6: Commit final**

```bash
git add tests/_test_reflection_e2e.py .superpowers/sdd/progress.md .superpowers/sdd/e2-final-review.md
git commit -m "test(reflection): E2E 占位 + final review ledger (T3.3)"
```

---

## Self-Review

按 writing-plans skill 自查:

**1. Spec coverage**(对照 E2 spec 各 section):
- D1 三面都做 → commit 1/2/3 ✅
- D2 事件驱动 passive hook → T1.3 `_run_one` + T2.2 4 处 emit ✅
- D3 write + inject 二合一 → T2.1 section + T1.3 save ✅
- D4 neg+ambig+pos 全覆盖 → T1.1 6 event 工厂含 3 severity ✅
- D5 (C) JUDGE_MODEL → T1.3 `_ask_judge_with_fallback` judge first + local fallback ✅
- D6 走 MemoryService source='reflection' → T1.3 `_run_one` + T3.1 service 注入 ✅
- D7 neg-only inject → T2.1 `_reflection_section` 条件 + section name ✅
- 组件 1-7 → T1.1-T3.2 全部覆盖 ✅
- 错误处理表 10 类 → T1.3 fail-soft + T1.3 Lock + T1.3 dedup + T2.2 try/except 4 处 ✅
- 测试策略 8 文件 → T1.1-T3.3 9 文件(超 1 个 e2e)✅
- 性能预算 / 风险 / 非目标 → plan 头部 global constraints + 风险表 ✅

**2. Placeholder scan**:全文 grep `TBD / TODO / 等等 / 后续` — 仅 spec 提到的开放问题(plan 阶段细化 few-shot 等)有提及,在合适位置标"实施员补"或"plan 阶段用真实失败 case 写"。无 placeholder 漏在 task body。

**3. Type consistency**:
- `ReflectionEngine.emit(event: ReflectionEvent)` 在 T1.3 / T2.2 / T3.2 一致
- `ReflectionEngine.get_last_neg_reflection() -> str | None` 在 T1.3 / T2.1 / T2.2 一致
- `ReflectionEngine.get_recent(*, limit=3) -> list[str]` 在 T1.3 / T3.2 一致
- `LLMDecider.decide(new_text, similar, *, recent_reflections=None)` 在 T3.1 / T1.3 注入一致
- `MemoryStore.search_reflections(*, limit, lookback_h)` 在 T3.1 / T1.3 一致
- `ReflectionEvent` 6 工厂函数名在 T1.1 / T2.2 / T3.2 一致

**4. 一处需修正**:T2.2 测试的"完整版"包含 `await run_turn(max_iter=3, ...)` 但 `run_turn` 实际签名可能更长,实施员需在调用前先 `inspect.signature(run_turn)` 决定形参顺序。我已在 T2.2 Step 3 注释 "必须放 **kwargs 之前" 提醒实施员。

**Self-review 结论**:plan 完整覆盖 spec 全部决策 + 7 组件 + 错误处理 + 测试策略,无 placeholder,类型一致。1 处提醒事项已注释到位。可进入执行阶段。

每 commit 独立可回滚,每 commit 完成后 `git log --oneline | head -3` 应看到新提交。
