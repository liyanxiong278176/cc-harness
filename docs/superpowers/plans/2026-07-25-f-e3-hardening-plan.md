# Sub-F E3 hardening + Standards cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 E3(自动续接)从"测试通过但生产多半失效"修真为"生产路径真接通"(D7 tool-diff + D6 cancelled 双端),清理 3 项 Standards 偏硬 smell(checkpoint_path / last_loaded_session_id / agent.py 内联 import),启用 FK pragma,补 manifest YAML round-trip。6 项修真 + 1 项 final review = 7 commit。

**Architecture:** Thin-layer 修真 across 7 modified files(`memory/store.py` / `project/service.py` / `project/manifest.py` / `mcp_client.py` / `repl.py` / `agent.py` / `main.py`)+ `audit.py` 接入 + 5 modified test files。No new deps, no new sub-package, no new LLM。

**Tech Stack:** Python 3.11+, asyncio, existing E1 / E2 / E3 / E5 contracts. aiosqlite. No new deps.

## Global Constraints

- TDD red→green for every fix;do NOT commit until tests pass
- Ruff-clean on every commit
- No breakage of:
  - D1 SubAgentRunner 8-status contract(F-T3 仅加 service 接口,不动 runner 主体)
  - E1 /reject + decomposition hint
  - E2 reflection 7-event pipeline
  - E5 drift detection(F-T3 list_in_progress 是 todo 层,与 drift memory 层不冲突)
  - Plan3 compression(Tier1 snip / Tier2 prune / delta cap)— F 不引新压缩路径
- Pre-existing baseline:13 failures in `tests/test_strategies_yaml.py` (4) + `test_attacks_exec` (2) + `test_attacks_yaml` (1) + `test_promptfoo_configs` (4) + `test_agent.py` (2);do NOT regress,do NOT attempt to fix
- Spec verbatim lock:
  - `cross_session_mode` Literal:`"off"` / `"last_only"` / `"ask"` (no other values)— F-T5 YAML 序列化沿用
  - Tool hash 形态:`sha256:{full_64_hex}`(F-T1/F-T2 修真 SHA256 全长,删除 `[:16]` 截短)
  - OpenAI tool dict 形态:`{"type": "function", "function": {"name": ..., "parameters": ...}}` (F-T1/F-T2 取 `t["function"]["name"]`)
- aiosqlite 异步 pattern:`await self._db.execute(...)`(F-T4 PRAGMA 沿此)
- Windows 强制 UTF-8:全部 Python 命令加 `PYTHONIOENCODING=utf-8`
- CrossSessionMode 字面 lock(同 E3)

---

### Task 1: mcp_client.list_tools async wrapper + SHA256 全长 + dict-based name access(F-D1)

**Files:**
- Modify: `cc_harness/mcp_client.py` (lines 174-175)
- Modify: `cc_harness/repl.py` (lines 783-790, 816-820)
- Test: `tests/test_mcp_client.py` + `tests/test_repl.py` 修正 mock 形态

**Interfaces:**
- `MCPClient.list_tools()` 改为 `async`,内部仍同步返回 `list(self._tools)`(最简 async 包装)
- `repl.py:_sha256_of_tool` 接受 dict 形态(OpenAI tool schema),取 `t["function"].get("parameters", {})`,SHA256 hexdigest() 不截短
- `repl.py:_maybe_load_cross_session` 783-790 修真:dict 取 name,移除裸 except → try/except + print_warn

**Steps:**

- [ ] **Step 1: 写失败测试 `tests/test_mcp_client.py`** 验证 `await mcp.list_tools()` 返回 list[dict](已有测试基础,补 1 async 测试)
- [ ] **Step 2: 跑测试确认 red**
- [ ] **Step 3: 改 `cc_harness/mcp_client.py:174-175`** 加 `async` 关键字
- [ ] **Step 4: 改 `cc_harness/repl.py:816-820`** `_sha256_of_tool` 改 dict 形态 + SHA256 hexdigest 不截短
- [ ] **Step 5: 改 `cc_harness/repl.py:783-790`** dict 取 name + try/except + print_warn
- [ ] **Step 6: 跑测试确认 green + ruff clean**
- [ ] **Step 7: 邻近 regression**(`tests/test_mcp_client.py` + `tests/test_repl.py` 持平 + 修真覆盖)
- [ ] **Step 8: commit** `feat(F T1): mcp_client.list_tools async wrapper + dict-based name access + SHA256 全长`

---

### Task 2: repl.py tool-diff 真接生产 — run_turn 透传 + 首次 snapshot(F-D1 续)

**Files:**
- Modify: `cc_harness/repl.py` (lines 459-479 run_turn 调用;783-790 首次 snapshot)
- Test: `tests/test_repl.py` 修真 mock 形态 + 新增首次 snapshot 测试

**Interfaces:**
- `repl.py:run_repl` 内 `run_turn` 调用必须传 `tool_diff=state.cross_session_tools_diff`(已有形参,只是没传)
- 首次 session(candidate=None 或 candidate.extra 无 tool_hash_snapshot)也要采集快照,`old_hash = {}` → diff = 全部 `+X`

**Steps:**

- [ ] **Step 1: 写失败测试 `tests/test_repl.py:test_run_repl_passes_tool_diff_to_run_turn`** 验证 `state.cross_session_tools_diff` 透传到 `run_turn`
- [ ] **Step 2: 写失败测试 `tests/test_repl.py:test_first_session_captures_tool_snapshot`** 验证 candidate=None 时 `state.tool_hash_snapshot` 被填 + `cross_session_tools_diff` 全部 `+X`
- [ ] **Step 3: 跑测试确认 red**
- [ ] **Step 4: 改 `cc_harness/repl.py:459-479`** run_turn 调用加 `tool_diff=` 形参
- [ ] **Step 5: 改 `cc_harness/repl.py:783-790`** 首次 snapshot:旧 `old_hash = candidate.extra.get(...)` → `old_hash = (candidate.extra.get(...) if candidate else {}) or {}`
- [ ] **Step 6: 跑测试确认 green + ruff clean**
- [ ] **Step 7: 邻近 regression**(test_repl.py + test_agent.py 持平)
- [ ] **Step 8: commit** `feat(F T2): repl.py tool-diff 真接生产 — run_turn 透传 + 首次 snapshot`

---

### Task 3: TodoService list_in_progress + mark_cancelled + repl.py D6 双端接入(F-D2)

**Files:**
- Modify: `cc_harness/project/service.py` (新增 2 方法)
- Modify: `cc_harness/repl.py` (lines 503-524 finally save + 789-792 load)
- Test: `tests/test_project_service.py` + `tests/test_repl.py`

**Interfaces:**
- `TodoService.list_in_progress() -> list[str]`:扫所有 non-terminal 状态 todo id(non-terminal = 不在 `{completed, cancelled, skipped}` 里)
- `TodoService.mark_cancelled(ids: list[str]) -> None`:批量标 cancelled;若 todo 不存在或已 terminal,no-op
- `repl.py` finally save extra 加 `"in_progress_subagents": state.todo_service.list_in_progress() if state.todo_service else []`
- `repl.py` load 端:`state.subagent_cancelled` 非空 + `state.todo_service` 存在 → 调 `mark_cancelled` + `print_warn`

**Steps:**

- [ ] **Step 1: 写失败测试 `tests/test_project_service.py:test_list_in_progress_returns_non_terminal_only`** 创建 3 todo(pending/running/completed),assert list_in_progress 返回前 2 个 id
- [ ] **Step 2: 写失败测试 `tests/test_project_service.py:test_mark_cancelled_batch`** 验证 mark_cancelled 批量标 cancelled + 已 terminal 不动
- [ ] **Step 3: 跑测试确认 red**
- [ ] **Step 4: 改 `cc_harness/project/service.py`** 加 `list_in_progress` + `mark_cancelled`
- [ ] **Step 5: 写失败测试 `tests/test_repl.py:test_save_persists_in_progress_subagents`** 验证 finally save extra 含 in_progress_subagents
- [ ] **Step 6: 写失败测试 `tests/test_repl.py:test_load_marks_in_progress_cancelled_and_warns`** 验证 load 端 mark_cancelled 调用 + print_warn 输出
- [ ] **Step 7: 改 `cc_harness/repl.py:503-524`** finally save 加 in_progress_subagents
- [ ] **Step 8: 改 `cc_harness/repl.py:789-792`** load 端 mark_cancelled + print_warn
- [ ] **Step 9: 跑测试确认 green + ruff clean**
- [ ] **Step 10: 邻近 regression**(test_repl.py + test_project_service.py 持平)
- [ ] **Step 11: commit** `feat(F T3): TodoService list_in_progress + mark_cancelled + repl.py D6 双端接入`

---

### Task 4: store.py PRAGMA foreign_keys=ON 初始化(F-D4)

**Files:**
- Modify: `cc_harness/memory/store.py` (lines 46-54)
- Test: `tests/test_memory_store_schema.py` 新增 1 测试

**Interfaces:**
- `MemoryStore.__init__` 或 `_open()` 阶段:`await self._db.execute("PRAGMA foreign_keys = ON")`,连接级一次性

**Steps:**

- [ ] **Step 1: 写失败测试 `tests/test_memory_store_schema.py:test_store_enables_fk_on_open`** 创建 store → 执行 `PRAGMA foreign_keys` 查询 → assert `1`
- [ ] **Step 2: 跑测试确认 red**
- [ ] **Step 3: 改 `cc_harness/memory/store.py:46-54`** 加 `await self._db.execute("PRAGMA foreign_keys = ON")`
- [ ] **Step 4: 跑测试确认 green + ruff clean**
- [ ] **Step 5: 邻近 regression**(test_memory_store_schema.py + test_memory_store.py 持平)
- [ ] **Step 6: commit** `feat(F T4): store.py PRAGMA foreign_keys=ON 初始化`

---

### Task 5: manifest.py _manifest_to_yaml 加 cross_session_mode + round-trip 测试(F-D5)

**Files:**
- Modify: `cc_harness/project/manifest.py` (lines 271-297)
- Test: `tests/test_project_manifest.py` (lines 466-498 区域)

**Interfaces:**
- `_manifest_to_yaml(m: Manifest) -> dict` 加 `"cross_session_mode": m.cross_session_mode.value`
- `_manifest_from_yaml` 已支持(沿用既有解析路径)

**Steps:**

- [ ] **Step 1: 写失败测试 `tests/test_project_manifest.py:test_save_load_manifest_round_trip_cross_session_mode`** Manifest 设 cross_session_mode=ASK → save_manifest 临时文件 → load_manifest → assert 仍是 ASK
- [ ] **Step 2: 跑测试确认 red**
- [ ] **Step 3: 改 `cc_harness/project/manifest.py:271-297 _manifest_to_yaml`** 加 cross_session_mode 字段
- [ ] **Step 4: 跑测试确认 green + ruff clean**
- [ ] **Step 5: 邻近 regression**(test_project_manifest.py 持平)
- [ ] **Step 6: commit** `feat(F T5): manifest.py _manifest_to_yaml 加 cross_session_mode + round-trip`

---

### Task 6: Standards cleanup — checkpoint_path 删 / last_loaded_session_id 接入 audit / agent.py 内联 import 提到模块级(F-D6)

**Files:**
- Modify: `cc_harness/repl.py` (lines 99-100)
- Modify: `cc_harness/agent.py` (顶部 + lines 983-991)
- Test: `tests/test_repl.py` (audit log 测试) + `tests/test_agent.py` (模块级预编译 regex 引用测试)

**Interfaces:**
- `repl.py:99` 删 `checkpoint_path: Path | None = None` 字段
- `repl.py:100` `last_loaded_session_id` 在 `_maybe_load_cross_session` load 成功后调 `audit.log_decision("session_resume", session_id=state.last_loaded_session_id, ...)`
- `agent.py` 顶部加 `_CROSS_SESSION_TOOLS_BLOCK_RE = re.compile(r"\n<cross_session_tools>.*?</cross_session_tools>\n", re.DOTALL)`,`_refresh_system_prompt` 内用预编译 pattern

**Steps:**

- [ ] **Step 1: 写失败测试 `tests/test_repl.py:test_load_cross_session_writes_audit_log`** 验证 load 成功后 audit log 含 "session_resume" event
- [ ] **Step 2: 跑测试确认 red**
- [ ] **Step 3: 改 `cc_harness/repl.py:99-100`** 删 checkpoint_path + 在 `_maybe_load_cross_session` 加 audit log_decision
- [ ] **Step 4: 跑测试确认 green**
- [ ] **Step 5: 写失败测试 `tests/test_agent.py:test_cross_session_tools_block_uses_module_level_regex`** 验证 agent 模块级 `_CROSS_SESSION_TOOLS_BLOCK_RE` 存在且为 re.Pattern,`_refresh_system_prompt` 调用时使用它
- [ ] **Step 6: 跑测试确认 red**
- [ ] **Step 7: 改 `cc_harness/agent.py`** 顶部加 `_CROSS_SESSION_TOOLS_BLOCK_RE` 预编译 + `_refresh_system_prompt` 内改用预编译 pattern.sub
- [ ] **Step 8: 跑测试确认 green + ruff clean**
- [ ] **Step 9: 邻近 regression**(test_repl.py + test_agent.py 持平)
- [ ] **Step 10: commit** `feat(F T6): Standards cleanup — checkpoint_path 删 / last_loaded_session_id 接入 audit / agent.py 内联 import 提到模块级`

---

### Task 7: 全量回归 + final review

**Files:**
- Read-only:全仓
- Write: `.superpowers/sdd/f-final-review.md`

**Steps:**

- [ ] **Step 1: 全量 pytest**(预期 13 个 pre-existing failure 持平,0 新失败)
  ```bash
  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/ -q 2>&1 | tail -20
  ```
- [ ] **Step 2: ruff 全量检查**
  ```bash
  .venv/Scripts/python.exe -m ruff check cc_harness/ tests/
  ```
- [ ] **Step 3: 写 `.superpowers/sdd/f-final-review.md`** F-T1..F-T6 6 commit 完成报告 + spec F-D1..F-D6 字面 lock + 8 components 全覆盖 + cross-cutting(L2/L4/L5/E2/E5/D1 不破)+ 13 baseline 持平
- [ ] **Step 4: ledger 入 commit message**(tag `F:` 前缀)
- [ ] **Step 5: 待用户决策 merge to master**

---

## 报告 contract(每个 T1-T6 任务)

写到 `D:\agent_learning\cc-harness\.superpowers\sdd\f-t{N}-report.md`:

```
# F T{N} report

**Status**: DONE
**BASE**: <base_sha>
**HEAD**: <sha>
**Test summary**: 
  - X/X new tests pass
  - 邻近 regression: 持平,0 新失败
  - ruff clean
**Commits**: 
  <sha> "feat(F T{N}): ..."

## What was done
- ...

## Plan/code adjustments
- (任何 deviation,或 "无")

## Self-review
- spec F-D{...} 字面 lock 全实现
- ...

## Concerns
- (任何,或 "无")
```

## 不要做

- ❌ 不要写 integration tests(T7 范围)
- ❌ 不要写 E2E tests(留 out of scope)
- ❌ 不要重写 E3 主体(只修真 6 项)
- ❌ 不要重写 subagent.py(F-D2 仅加 service 接口,不动 runner 契约)
- ❌ 不要引入新依赖
- ❌ 不要 commit 前先跑全量(只跑邻近)
- ❌ 不要碰 L2/L4/L5/L8 防御层
- ❌ 不要碰 Out of scope 3 项(D5 recall 注入 / 真 LLM E2E / Standards 4 judgement smell)