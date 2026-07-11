# Plan 2: 长期记忆接入生产

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 locomo runner 临时注入的 memory 工具(`memory_recall`/`memory_save`)提到共享 helper,接入生产 REPL——chat/coding 模式自动拥有长期记忆,session 级单例,优雅降级。

**Architecture:** ① 新建 `cc_harness/memory/extras.py::build_memory_extras(env, db_path)`,从 `runner._build_memory_extras` 提取(`runner` 改调它,消除重复);② `ReplState` 加 `mem_deps` 字段;③ `run_repl` 启动时 `await build_memory_extras` 构造(session 级),每轮 `run_turn` 传 `extra_native_specs`;④ 失败优雅降级(无 `EMBEDDING_*` → warning + 不接,不阻断)。

**Tech Stack:** Python 3.11 / pytest-asyncio / cc_harness.memory(SQLite + sqlite-vec + embedding)

**关联 spec:** `docs/superpowers/specs/2026-07-10-assistant-chat-memory-compaction-eval-design.md`(子系统③)
**前置:** Plan 1(chat mode 存在 + `extra_native_specs` 机制)
**后续:** Plan 3(压缩)/ Plan 4(指标消费 tool_calls)

---

## File Structure(Plan 2 涉及)

| 文件 | 责任 | 改动 |
|---|---|---|
| `cc_harness/memory/extras.py` | 新 | `build_memory_extras(env, db_path)` 共享 helper |
| `eval/locomo/runner.py:54-117` | 改 | `_build_memory_extras` 改调共享 helper(消除重复) |
| `cc_harness/repl.py:51-56` | 改 | `ReplState` 加 `memory_extras`(与 Task 3 代码一致;非 `mem_deps`) |
| `cc_harness/repl.py:107-228` | 改 | `run_repl` 启动构造 + `run_turn` 传 `extra_native_specs` |
| `tests/test_memory_extras.py` | 新 | helper 单测 |
| `tests/test_repl.py` | 改 | mem_deps 构造 + 传参 |

---

## Task 1: 共享 helper `build_memory_extras`(新建 `cc_harness/memory/extras.py`)

**Files:**
- Create: `cc_harness/memory/extras.py`
- Test: `tests/test_memory_extras.py`

- [ ] **Step 1: 写失败测试**

`tests/test_memory_extras.py`:
```python
"""memory/extras.py 共享 helper 单测。"""
import asyncio
import pytest


def test_build_memory_extras_returns_extras_when_deps_ok(monkeypatch, tmp_path):
    """依赖齐全 → 返回 (extras 非空, deps 非空)。
    mock 掉需要 sqlite-vec / 网络的部分(关键:init_schema,避免无 sqlite-vec 时降级)。"""
    import asyncio
    # sqlite-vec 缺失时 init_schema 抛 → 降级 ([], None),test 失败。patch 成 async no-op。
    async def _noop_init(self): self._db = None
    monkeypatch.setattr("cc_harness.memory.store.MemoryStore.init_schema", _noop_init)
    # EmbeddingClient / LLMClient / LLMDecider 构造不联网(只存配置),用 fake env key 即可构造;
    # 真正联网只在 save/recall 调用时,extras 构造本身不触发。实现者按需补 mock。
    env = {"OPENAI_API_KEY": "k", "OPENAI_BASE_URL": "u", "OPENAI_MODEL": "m",
           "EMBEDDING_BASE_URL": "u", "EMBEDDING_API_KEY": "k", "EMBEDDING_MODEL": "bge"}
    from cc_harness.memory.extras import build_memory_extras
    extras, deps = asyncio.run(build_memory_extras(env, tmp_path / "mem.db"))
    assert len(extras) == 2
    names = [e["spec"]["function"]["name"] for e in extras]
    assert "memory_recall" in names and "memory_save" in names


def test_build_memory_extras_fail_soft_on_missing_env(monkeypatch, tmp_path):
    """缺 EMBEDDING_* 且构造失败 → 返回 ([], None),不抛。"""
    from cc_harness.memory.extras import build_memory_extras
    env = {}  # 空 env,缺 key
    extras, deps = asyncio.run(build_memory_extras(env, tmp_path / "mem.db"))
    assert extras == []
    assert deps is None
```

> mock 策略:Step 1 的 fake 依赖较繁琐,实现者可简化——直接用真 MemoryStore(tmp_path db)+ mock EmbeddingClient/LLMDecider 的网络调用,或全 mock。关键是验证:成功返 2 spec、失败返 ([], None)。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_extras.py -v`
Expected: FAIL(`cc_harness.memory.extras` 不存在,ImportError)

- [ ] **Step 3: 实现 `build_memory_extras`**

`cc_harness/memory/extras.py`(从 `runner.py:54-117` 的 `_build_memory_extras` 提取,签名改为 `(env, db_path)`):
```python
"""Shared helper: construct memory tools (memory_recall/save) as extra_native_specs.

Used by both locomo runner (eval) and repl (production). Caller owns the
inject_memory_tools gate (kill-switch) and db_path (isolation).
"""
from __future__ import annotations
from pathlib import Path


async def build_memory_extras(env: dict, db_path: Path) -> tuple[list[dict], dict | None]:
    """Return (extras, deps). extras: [{spec, handler, deps}].

    async because MemoryStore.init_schema() is async (store.py:44).
    Any dependency failure (missing EMBEDDING_*, sqlite-vec missing, schema init
    failure) → graceful degrade: print warning, return ([], None).
    """
    try:
        from cc_harness.memory.store import MemoryStore
        from cc_harness.memory.embedding import EmbeddingClient
        from cc_harness.memory.decider import LLMDecider
        from cc_harness.memory.retriever import MemoryRetriever
        from cc_harness.memory.service import MemoryService
        from cc_harness.memory.tools import (
            MEMORY_RECALL_SPEC, MEMORY_SAVE_SPEC,
            memory_recall_handler, memory_save_handler,
        )
        from cc_harness.llm import LLMClient
    except ImportError as e:
        print(f"[memory] import failed: {e}; running without memory tools")
        return [], None
    try:
        emb_base = env.get("EMBEDDING_BASE_URL") or env["OPENAI_BASE_URL"]
        emb_key = env.get("EMBEDDING_API_KEY") or env["OPENAI_API_KEY"]
        emb_model = env.get("EMBEDDING_MODEL", "BAAI/bge-m3")
        emb_dim = int(env.get("EMBEDDING_DIM", "1024"))

        store = MemoryStore(db_path=db_path, embedding_dim=emb_dim)
        await store.init_schema()
        embedder = EmbeddingClient(
            base_url=emb_base, api_key=emb_key, model=emb_model, dim=emb_dim, timeout_s=10.0,
        )
        decider_llm = LLMClient(
            api_key=env["OPENAI_API_KEY"], model=env["OPENAI_MODEL"], base_url=env["OPENAI_BASE_URL"],
        )
        decider = LLMDecider(llm=decider_llm)
        service = MemoryService(store=store, embedder=embedder, decider=decider)
        retriever = MemoryRetriever(store=store, embedder=embedder)
    except Exception as e:
        print(f"[memory] service init failed: {e}; running without memory tools")
        return [], None

    extras = [
        {"spec": MEMORY_RECALL_SPEC, "handler": memory_recall_handler, "deps": {"retriever": retriever}},
        {"spec": MEMORY_SAVE_SPEC, "handler": memory_save_handler, "deps": {"service": service}},
    ]
    return extras, {"service": service, "retriever": retriever}
```

> 与 `runner._build_memory_extras` 几乎一致,差异:① 签名 `(env, db_path)` 而非读 policy;② 不含 `inject_memory_tools` gate(留给 caller)。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_extras.py -v`
Expected: 2 PASS(或按 mock 策略调整后 PASS)

- [ ] **Step 5: Commit**

```bash
git add cc_harness/memory/extras.py tests/test_memory_extras.py
git commit -m "feat(memory): 共享 helper build_memory_extras — runner/repl 复用

从 runner._build_memory_extras 提取,签名 (env, db_path),不含 inject gate。
失败优雅降级返 ([], None)。Plan2 Task1。"
```

---

## Task 2: runner 改用共享 helper(消除重复)

**Files:**
- Modify: `eval/locomo/runner.py:54-117`、`:318`(amain 调用点)

- [ ] **Step 1: `_build_memory_extras` 改为薄封装调共享 helper**

`eval/locomo/runner.py:54-117` 整个 `_build_memory_extras` 替换为:
```python
async def _build_memory_extras(policy: dict):
    """locomo runner 的 memory extras 构造。复用共享 helper。
    inject_memory_tools gate 留在此处(locomo kill-switch)。"""
    if not policy.get("inject_memory_tools", True):
        return [], None
    from cc_harness.memory.extras import build_memory_extras
    return await build_memory_extras(_env(), REPO / "logs" / "locomo_memory.db")
```

> db_path 保持 `logs/locomo_memory.db`(eval 隔离)。`_env()` 提供依赖。

- [ ] **Step 2: 验证 runner 现有测试不破坏**

Run: `.venv/Scripts/python.exe -m pytest eval/ tests/ -k "locomo or runner or memory" -q`
Expected: PASS(若有 locomo runner 单测)或无回归

- [ ] **Step 3: 全量回归**

Run: `.venv/Scripts/python.exe -m pytest tests/ -x -q`
Expected: 全 PASS

- [ ] **Step 4: Commit**

```bash
git add eval/locomo/runner.py
git commit -m "refactor(locomo-eval): runner 改用共享 build_memory_extras

消除 _build_memory_extras 重复实现,inject gate 留在 runner。Plan2 Task2。"
```

---

## Task 3: ReplState 加 `mem_deps` + run_repl 启动构造

**Files:**
- Modify: `cc_harness/repl.py:51-56`(`ReplState`)
- Modify: `cc_harness/repl.py:107-166`(`run_repl` 启动段)

- [ ] **Step 1: 写失败测试**

`tests/test_repl.py` 加:
```python
def test_repl_state_has_mem_deps():
    """ReplState 有 mem_deps 字段,默认 None。"""
    from cc_harness.repl import ReplState
    s = ReplState()
    assert s.mem_deps is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_repl.py::test_repl_state_has_mem_deps -v`
Expected: FAIL(`ReplState` 无 `mem_deps`)

- [ ] **Step 3: ReplState 加字段**

`cc_harness/repl.py:51-56`:
```python
@dataclass
class ReplState:
    mode: str = "coding"
    messages: list[dict] = field(default_factory=list)
    session_stats: SessionTokenStats = field(default_factory=SessionTokenStats)
    token_counter: TokenCounter = field(default_factory=TokenCounter)
    memory_extras: list[dict] = field(default_factory=list)  # Plan2: memory 工具 extras(session 级)
```

- [ ] **Step 4: run_repl 启动段构造 memory extras**

`cc_harness/repl.py` `run_repl` 在 `state = ReplState(...)` 后(`:129` 后)、主循环前,加 memory 构造:
```python
    state = ReplState(mode=default_mode)

    # Plan2: 构造 memory 工具(session 级单例)。失败优雅降级。
    import os as _os
    from pathlib import Path as _Path
    from dotenv import dotenv_values as _dotenv
    _mem_env = {**_os.environ, **{k: v for k, v in _dotenv(_Path(cwd) / ".env").items() if v}}
    try:
        from cc_harness.memory.extras import build_memory_extras
        state.memory_extras, _mem_deps = await build_memory_extras(
            _mem_env, _Path(cwd) / "logs" / "memory.db"
        )
        if state.memory_extras:
            print_info(console, f"  memory tools: {len(state.memory_extras)} 个(memory_recall/save)")
        else:
            print_info(console, "  memory tools: 未启用(EMBEDDING_* 缺失或初始化失败)")
    except Exception as e:
        print_warn(console, f"memory 初始化异常: {e}; 不接入记忆工具")
        state.memory_extras = []
```

> 生产 db = `logs/memory.db`(与 eval `locomo_memory.db` 隔离)。

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_repl.py::test_repl_state_has_mem_deps -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add cc_harness/repl.py tests/test_repl.py
git commit -m "feat(repl): ReplState 加 memory_extras + run_repl 启动构造记忆工具

session 级单例,失败优雅降级。生产 db=logs/memory.db。Plan2 Task3。"
```

---

## Task 4: run_repl 传 `extra_native_specs` 给 run_turn

**Files:**
- Modify: `cc_harness/repl.py:219-228`(run_turn 调用)

- [ ] **Step 1: 写失败测试(传参验证)**

`tests/test_repl.py` 加(用 mock run_turn 捕获 extra_native_specs):
```python
async def test_run_repl_passes_memory_extras_to_run_turn(monkeypatch):
    """run_repl 把 memory_extras 传给 run_turn 的 extra_native_specs。"""
    # mock run_turn 捕获 kwargs;mock build_memory_extras 返固定 extras
    # (实现者参考 test_repl.py 现有 run_repl 测试的 mock 模式)
    captured = {}
    async def fake_run_turn(messages, llm, mcp, **kw):
        captured["extra_native_specs"] = kw.get("extra_native_specs")
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()
    monkeypatch.setattr("cc_harness.repl.run_turn", fake_run_turn, raising=False)
    # ... 触发一轮 run_repl(参考现有 test_repl run_repl 测试)
    assert captured["extra_native_specs"] is not None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_repl.py -k memory_extras_to_run_turn -v`
Expected: FAIL(run_repl 没传 extra_native_specs)

- [ ] **Step 3: run_turn 调用加 extra_native_specs**

`cc_harness/repl.py:219-228` `run_turn(...)` 加参数:
```python
            turn_stats = await run_turn(
                state.messages, llm, mcp,
                max_iter=max_iter,
                mode=state.mode,
                cwd=cwd,
                design_dir=design_dir,
                token_counter=state.token_counter,
                policy=policy,
                l5=l5,
                extra_native_specs=state.memory_extras or None,  # Plan2: 记忆工具
            )
```

> `or None`:无 memory 工具时传 None(避免空 list 干扰)。chat/coding 接收(有工具);plan/design 的 `tool_specs=None` 物理禁工具,extras 不生效。

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `.venv/Scripts/python.exe -m pytest tests/ -x -q`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add cc_harness/repl.py tests/test_repl.py
git commit -m "feat(repl): run_repl 传 extra_native_specs(memory 工具)给 run_turn

chat/coding 模式接入 memory_recall/save;plan/design 物理禁工具不生效。Plan2 Task4。"
```

---

## Task 5: 集成验证(生产 chat 模式调 memory 工具)

**Files:** 无代码改动,验证用

- [ ] **Step 1: 启动 REPL(chat 模式),确认 memory tools 加载**

Run(用户执行):
```bash
cd /d/agent_learning/cc-harness
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe main.py --mode chat
```
Expected: 启动信息含 `memory tools: 2 个(memory_recall/save)`(若 EMBEDDING_* 配好)或 `memory tools: 未启用`(无 EMBEDDING,优雅降级,不崩)。

- [ ] **Step 2: 对话中触发 memory_save + memory_recall**

在 REPL 里输入:
```
记住:我喜欢用 Python 写脚本,讨厌 Java。
```
再输入:
```
我喜欢什么编程语言?
```
Expected: 第二问前 agent 调 `memory_recall`(观察段显示记忆),答出 Python(从记忆或上下文)。

- [ ] **Step 3: 验证 db 写入**

Run:
```bash
.venv/Scripts/python.exe -c "import sqlite3; c=sqlite3.connect('logs/memory.db'); print(c.execute('SELECT id,substr(text,1,40),source FROM memories LIMIT 5').fetchall())"
```
Expected: 有记忆行(source 含 "llm")。

> 烟测通过 = Plan 2 目标达成:生产 REPL chat/coding 接入长期记忆,memory_recall/save 可用。

---

## Plan 2 完成标准

- [ ] Task 1-4 全 commit,`pytest tests/ -x -q` 全绿
- [ ] `ruff check cc_harness/ tests/ eval/` 干净
- [ ] Task 5 集成:REPL chat 模式 memory tools 加载(或优雅降级),对话触发 save/recall,db 有写入
- [ ] 生产 `logs/memory.db` 与 eval `logs/locomo_memory.db` 隔离(不串)

## 给 Plan 3/4 的接口契约(本 plan 落地)

- `cc_harness.memory.extras.build_memory_extras(env, db_path) -> (extras, deps)`(Plan 3/4 不直接用,但 runner/repl 共享)
- `ReplState.memory_extras: list[dict]`(run_repl 内部,session 级)
- 生产 chat/coding 模式有 memory 工具 → Plan 4 locomo 记忆指标(memory_recall/save 调用数)有数据源
