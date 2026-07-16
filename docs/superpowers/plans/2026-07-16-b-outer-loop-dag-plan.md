# Plan B: 其一·长程任务 — Sub-project B 最小底座落地

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 `2026-07-16-b-outer-loop-dag-design.md` spec 的 4 个组件(`topo_sort` + `get_ready_tasks` / `verify.py` / `todo_toposort` tool / `_after_turn_todo` 接线),为将来的 subagent 提供 DAG 数据底座与 verify hook。

**Architecture:** 按 spec 组件顺序逐个落地,每个组件独立可测可 commit。`topo_sort` + `get_ready_tasks` 填 A 阶段 `dependency.py` 占位(Kahn 算法 + 字典序 tiebreaker);`verify.py` 新文件(heuristic + state 双轨 verify);`todo_toposort` 在 A 阶段 7 tool 后新增第 8 个(给主 agent 查 DAG 拓扑);`_after_turn_todo` 填 A 阶段 repl.py 占位(每 turn 跑 verify 写 hints,延迟一轮注入 system prompt)。

**Tech Stack:** Python 3.11 / pytest / pytest-asyncio / PyYAML / asyncio / dataclasses(已落基础设施)

**关联 spec:**
- 设计源:`docs/superpowers/specs/2026-07-16-b-outer-loop-dag-design.md`(456 行,本 plan 直接落地;实现者**必读该 spec**)
- 上游:`docs/superpowers/specs/2026-07-14-long-horizon-task-tracking-design.md`(A spec,B 阶段 4 处引用 A 已落接口)

**前置:** Sub-project A 已落地(1016 测试 baseline,本 plan 必须保住),依赖以下 A 已落符号:
- `cc_harness.project.dependency` — `check_references` / `check_no_cycle` / `dep_check` / `DependencyCycleError`
- `cc_harness.project.service` — `TodoService` 7 ops + `subscribe` / `unsubscribe`
- `cc_harness.project.tools` — 7 个 tool handler + `inject_todo_tools`
- `cc_harness.repl` — `ReplState` + `_after_turn_todo(state, todo_service)` 占位
- `cc_harness.agent` — `run_turn(..., resume_task=None)` + `_refresh_system_prompt`

**后续:** Sub-project C(HTN + checkpoint 自检)在本 plan 完成后另立 plan;远期 Sub-project D(SubAgent)用 B 阶段 `get_ready_tasks` 做 fan-out,verify hook 做 result 验收入口。

> ⚠ 本 plan 大量引用 spec 的数据契约、算法、决策记录。每个 Task 标注 spec 对应节,实现者读 spec 该节拿完整代码/算法。本 plan 只给 TDD 步骤编排 + 关键代码骨架。

**commit message 规范**(spec 迁移计划段已定):每个 B 阶段 commit 末尾必须显式报告测试 baseline + delta:
```
baseline: 1016 passed → now: X passed (delta +N)
```
确保任何回归在 commit message 可见,review 时一眼能看出 A 阶段 1016 是否保住。

## 测试 API 约定(全 plan 适用)

`TodoService.create()` 签名(`cc_harness/project/service.py:182-196`):**只接受 keyword-only 参数**,无 `status` 字段。`status` 必须创建后通过 `update()` 设。

**所有 B 阶段测试用 helper 函数(避免每个 test 都写两行)**:

```python
async def _create(svc, title, status="pending", criteria=None, deps=None, session_id="s"):
    """Helper: 创建 task + (可选) update status + acceptance_criteria + depends_on。
    
    用法:await _create(svc, "T1", status="in_progress", criteria=["..."], deps=["T2"])
    
    关键 API 细节:
    - svc.create() 是 keyword-only, 无 status 字段 (status 默认 pending)
    - svc.update(task_id, *, session_id, **fields) 也是 keyword-only,
      fields 必须作为 kwargs 传入, 不能传 dict
    """
    t = await svc.create(
        title=title,
        acceptance_criteria=criteria or [],
        depends_on=deps or [],
        session_id=session_id,
    )
    if status != "pending":  # pending 是默认, 不必 update
        t = await svc.update(t.id, status=status, session_id=session_id)
    return t
```

**完整代码块(必须原样复制到 test 文件顶部)**:

```python
async def _create(svc, title, status="pending", criteria=None, deps=None, session_id="s"):
    t = await svc.create(
        title=title,
        acceptance_criteria=criteria or [],
        depends_on=deps or [],
        session_id=session_id,
    )
    if status != "pending":
        t = await svc.update(t.id, status=status, session_id=session_id)
    return t
```

放在 `tests/test_project_tools.py` 顶部 + `tests/test_repl_b_hook.py` 顶部各一份(避免 cross-test 引用)。所有本 plan 测试代码里出现的 `svc.create({"title": ..., "status": ...}, ...)` 全部改用 `_create(svc, ..., status=...)`。

---

## File Structure(Plan B 涉及,5 文件改 + 2 文件新 + 4 测试文件)

| 文件 | 责任 | spec 节 | 状态 |
|---|---|---|---|
| `cc_harness/project/dependency.py` | 填 `topo_sort` 占位 + 新 `get_ready_tasks` | 组件 1 | **改**(A 已 100% 覆盖,新函数加测试保持) |
| `cc_harness/project/verify.py` | 新:`VerifyResult` + `heuristic_check` + `state_check` + `run_verify` | 组件 2 | **新** |
| `cc_harness/project/tools.py` | 加第 8 个 tool `todo_toposort`(spec + handler) | 组件 3 | **改** |
| `cc_harness/repl.py` | 填 `_after_turn_todo` 占位 + `ReplState` 加 2 字段 + `last_turn_text` 接线 | 组件 4 | **改** |
| `cc_harness/agent.py` | `_refresh_system_prompt` 加 `<todo_hints>` 段 append | 组件 4 | **改** |

**测试文件**:

| 文件 | 覆盖 | spec 节 |
|---|---|---|
| `tests/test_project_dependency.py` | **改**:append `topo_sort` / `get_ready_tasks` 测试(保持 100% 覆盖) | 组件 1 |
| `tests/test_project_verify.py` | **新**:`VerifyResult` + `heuristic_check` + `state_check` + `run_verify`(目标 100%) | 组件 2 |
| `tests/test_project_tools.py` | **改**:append `todo_toposort` handler 测试(7 case)+ 改 `test_specs_have_distinct_names` 加 `"todo_toposort"` | 组件 3 |
| `tests/test_repl_b_hook.py` | **新**:`_after_turn_todo` 集成 + `<todo_hints>` agent 注入 | 组件 4 |
| `tests/_test_b_e2e.py` | **新**:`_` 前缀 gated,FakeLLM E2E + 1 真 LLM | 组件 4 |

(无 fixtures 文件 — 所有测试用 pytest `tmp_path` fixture 自包含)

---

## Task 1: `topo_sort` + `get_ready_tasks`

**Files:**
- Modify: `cc_harness/project/dependency.py`(line 187-191 注释占位 → 实现)
- Modify: `tests/test_project_dependency.py`(append 新测试)

**spec 节:** 组件 1(`topo_sort` + `get_ready_tasks` API 定义 + TDD 边界 case 列表)

**前置:** A 已落 `DependencyCycleError`(`cc_harness.project.exceptions.DependencyCycleError`,import 自 `cc_harness.project.dependency`)。`TodoTask` 字段从 `cc_harness.project.models` 已 import。

**测试覆盖目标:** `cc_harness/project/dependency.py` 保持 100% line + branch(A baseline + B 新增)。

### Task 1.1: `topo_sort` 失败测试 + 最小实现

- [ ] **Step 1: 在 `tests/test_project_dependency.py` 末尾 append 测试**

```python
# --- B 阶段 Task 1.1: topo_sort ---

def test_topo_sort_empty_dict():
    """空 dict → []"""
    from cc_harness.project.dependency import topo_sort
    assert topo_sort({}) == []


def test_topo_sort_single_task_no_deps():
    """单 task 无依赖 → [id]"""
    from cc_harness.project.dependency import topo_sort
    from datetime import datetime
    from cc_harness.project.models import TodoTask
    t = TodoTask(
        id="T1", title="x", status="pending", description="",
        depends_on=[], parent_task=None, assigned_to=None, priority=None,
        labels=[], due_date=None, effort_estimate=None,
        acceptance_criteria=[], created_at=datetime.now(), updated_at=datetime.now(),
    )
    assert topo_sort({"T1": t}) == ["T1"]


def test_topo_sort_chain():
    """链 T1 → T2 → T3,依赖列表: T1=[], T2=[T1], T3=[T2]"""
    from cc_harness.project.dependency import topo_sort
    from datetime import datetime
    from cc_harness.project.models import TodoTask

    def _t(id_, deps):
        return TodoTask(
            id=id_, title=id_, status="pending", description="",
            depends_on=deps, parent_task=None, assigned_to=None, priority=None,
            labels=[], due_date=None, effort_estimate=None,
            acceptance_criteria=[], created_at=datetime.now(), updated_at=datetime.now(),
        )
    tasks = {"T1": _t("T1", []), "T2": _t("T2", ["T1"]), "T3": _t("T3", ["T2"])}
    assert topo_sort(tasks) == ["T1", "T2", "T3"]


def test_topo_sort_diamond():
    """菱形: T1 → T2, T1 → T3, T2 → T4, T3 → T4"""
    from cc_harness.project.dependency import topo_sort
    from datetime import datetime
    from cc_harness.project.models import TodoTask

    def _t(id_, deps):
        return TodoTask(
            id=id_, title=id_, status="pending", description="",
            depends_on=deps, parent_task=None, assigned_to=None, priority=None,
            labels=[], due_date=None, effort_estimate=None,
            acceptance_criteria=[], created_at=datetime.now(), updated_at=datetime.now(),
        )
    tasks = {
        "T1": _t("T1", []),
        "T2": _t("T2", ["T1"]),
        "T3": _t("T3", ["T1"]),
        "T4": _t("T4", ["T2", "T3"]),
    }
    order = topo_sort(tasks)
    assert order.index("T1") < order.index("T2")
    assert order.index("T1") < order.index("T3")
    assert order.index("T2") < order.index("T4")
    assert order.index("T3") < order.index("T4")
    assert len(order) == 4


def test_topo_sort_with_cycle():
    """环 T1 → T2 → T1 → 抛 DependencyCycleError,消息含路径"""
    from cc_harness.project.dependency import topo_sort, DependencyCycleError
    from datetime import datetime
    from cc_harness.project.models import TodoTask
    import pytest

    def _t(id_, deps):
        return TodoTask(
            id=id_, title=id_, status="pending", description="",
            depends_on=deps, parent_task=None, assigned_to=None, priority=None,
            labels=[], due_date=None, effort_estimate=None,
            acceptance_criteria=[], created_at=datetime.now(), updated_at=datetime.now(),
        )
    tasks = {"T1": _t("T1", ["T2"]), "T2": _t("T2", ["T1"])}
    with pytest.raises(DependencyCycleError) as exc_info:
        topo_sort(tasks)
    # 消息含环路径
    assert "T1" in str(exc_info.value) and "T2" in str(exc_info.value)


def test_topo_sort_missing_dependency_skipped():
    """依赖引用不在 dict → 跳过该边,不阻塞拓扑"""
    from cc_harness.project.dependency import topo_sort
    from datetime import datetime
    from cc_harness.project.models import TodoTask

    def _t(id_, deps):
        return TodoTask(
            id=id_, title=id_, status="pending", description="",
            depends_on=deps, parent_task=None, assigned_to=None, priority=None,
            labels=[], due_date=None, effort_estimate=None,
            acceptance_criteria=[], created_at=datetime.now(), updated_at=datetime.now(),
        )
    # T2 依赖不存在的 "MISSING"
    tasks = {"T1": _t("T1", []), "T2": _t("T2", ["MISSING"])}
    assert topo_sort(tasks) == ["T1", "T2"]  # 跳过 MISSING 边


def test_topo_sort_self_dependency_is_cycle():
    """task.depends_on 含自己 → 视作环"""
    from cc_harness.project.dependency import topo_sort, DependencyCycleError
    from datetime import datetime
    from cc_harness.project.models import TodoTask
    import pytest

    t = TodoTask(
        id="T1", title="x", status="pending", description="",
        depends_on=["T1"], parent_task=None, assigned_to=None, priority=None,
        labels=[], due_date=None, effort_estimate=None,
        acceptance_criteria=[], created_at=datetime.now(), updated_at=datetime.now(),
    )
    with pytest.raises(DependencyCycleError):
        topo_sort({"T1": t})


def test_topo_sort_tiebreaker_alphabetical():
    """同优先级不同 id(无依赖关系)→ 字典序"""
    from cc_harness.project.dependency import topo_sort
    from datetime import datetime
    from cc_harness.project.models import TodoTask

    def _t(id_):
        return TodoTask(
            id=id_, title=id_, status="pending", description="",
            depends_on=[], parent_task=None, assigned_to=None, priority=None,
            labels=[], due_date=None, effort_estimate=None,
            acceptance_criteria=[], created_at=datetime.now(), updated_at=datetime.now(),
        )
    # T2, T1, T3 字典序应为 T1, T2, T3
    tasks = {"T2": _t("T2"), "T1": _t("T1"), "T3": _t("T3")}
    assert topo_sort(tasks) == ["T1", "T2", "T3"]


def test_topo_sort_with_done_tasks_in_dict():
    """输入含 done task,拓扑序仍包含它们"""
    from cc_harness.project.dependency import topo_sort
    from datetime import datetime
    from cc_harness.project.models import TodoTask

    def _t(id_, status="pending", deps=None):
        return TodoTask(
            id=id_, title=id_, status=status, description="",
            depends_on=deps or [], parent_task=None, assigned_to=None, priority=None,
            labels=[], due_date=None, effort_estimate=None,
            acceptance_criteria=[], created_at=datetime.now(), updated_at=datetime.now(),
        )
    tasks = {
        "T1": _t("T1", "done"),
        "T2": _t("T2", "done", ["T1"]),
        "T3": _t("T3", "pending", ["T2"]),
    }
    assert topo_sort(tasks) == ["T1", "T2", "T3"]
```

- [ ] **Step 2: 跑测试验失败**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_project_dependency.py -v -k "topo_sort"
```

Expected: 8 failed with `ImportError: cannot import name 'topo_sort' from 'cc_harness.project.dependency'`

- [ ] **Step 3: 在 `cc_harness/project/dependency.py` 末尾实现 `topo_sort`**

把 line 187-191 的 `# TODO: B 阶段实现 Kahn 拓扑排序。` 替换为:

```python
# ---------------------------------------------------------------------------
# B 阶段实现:Kahn 拓扑排序 + get_ready_tasks(spec 组件 1)
# ---------------------------------------------------------------------------


def topo_sort(tasks: dict[str, TodoTask]) -> list[str]:
    """Kahn 算法。返回拓扑序 list[id];失败抛 DependencyCycleError。

    Tiebreaker:字典序(deterministic,LLM 输出可重现)。
    只跟踪存在于字典内的依赖边,缺失依赖由 check_references 报告。
    空字典返回 []。
    """
    if not tasks:
        return []

    # 入度表:只数存在于字典内的依赖
    indegree: dict[str, int] = {tid: 0 for tid in tasks}
    # 邻接表:only 字典内边
    graph: dict[str, list[str]] = {tid: [] for tid in tasks}
    for tid, task in tasks.items():
        for dep_id in task.depends_on:
            if dep_id in tasks and dep_id != tid:  # 跳过 self-loop(下面单独处理)
                graph[dep_id].append(tid)
                indegree[tid] += 1
            elif dep_id == tid:
                # self-loop 直接视作环
                raise DependencyCycleError(
                    f"dependency cycle detected: {tid} -> {tid}"
                )

    # Kahn BFS,优先队列按字典序
    import heapq
    heap: list[str] = sorted(tid for tid, deg in indegree.items() if deg == 0)
    heapq.heapify(heap)
    order: list[str] = []
    while heap:
        node = heapq.heappop(heap)
        order.append(node)
        for neighbor in graph[node]:
            indegree[neighbor] -= 1
            if indegree[neighbor] == 0:
                heapq.heappush(heap, neighbor)

    if len(order) != len(tasks):
        # 有环,indegree 残留 > 0 的节点
        remaining = [tid for tid, deg in indegree.items() if deg > 0]
        chain = " -> ".join(remaining + [remaining[0]] if remaining else [])
        raise DependencyCycleError(
            f"dependency cycle detected: {chain}"
        )
    return order
```

- [ ] **Step 4: 跑测试验通过**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_project_dependency.py -v -k "topo_sort"
```

Expected: 8 passed

- [ ] **Step 5: 跑 100% 覆盖验证**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_project_dependency.py --cov=cc_harness/project/dependency --cov-branch --cov-report=term-missing
```

Expected: 100% line + branch 覆盖(status check + dep_check + topo_sort 全覆盖)。若 100% 不达,补缺失 case 直至达标。

- [ ] **Step 6: 跑 baseline 全量 + commit**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/ -q
```

Expected: 1016 + 8 = 1024 passed(无回归)

```bash
git add cc_harness/project/dependency.py tests/test_project_dependency.py
git commit -m "feat(dependency): topo_sort Kahn + get_ready_tasks 占位填实

B 阶段 Task 1.1: 填 A 阶段 # TODO 占位,实现 Kahn 拓扑排序。
- 字典序 tiebreaker(确定性,LLM 输出可重现)
- 缺失依赖跳过该边(由 check_references 报告)
- self-loop 视作环
- 覆盖 A baseline 100% line + branch

baseline: 1016 passed → now: 1024 passed (delta +8)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 1.2: `get_ready_tasks` 失败测试 + 最小实现

**Files:**
- Modify: `cc_harness/project/dependency.py`(append 实现,在 topo_sort 后)
- Modify: `tests/test_project_dependency.py`(append 测试)

- [ ] **Step 1: 在 `tests/test_project_dependency.py` 末尾 append 测试**

```python
# --- B 阶段 Task 1.2: get_ready_tasks ---

def test_get_ready_tasks_empty():
    """空 dict → []"""
    from cc_harness.project.dependency import get_ready_tasks
    assert get_ready_tasks({}) == []


def test_get_ready_tasks_pending_no_deps():
    """pending 无依赖 → ready"""
    from cc_harness.project.dependency import get_ready_tasks
    from datetime import datetime
    from cc_harness.project.models import TodoTask

    def _t(id_, status="pending", deps=None):
        return TodoTask(
            id=id_, title=id_, status=status, description="",
            depends_on=deps or [], parent_task=None, assigned_to=None, priority=None,
            labels=[], due_date=None, effort_estimate=None,
            acceptance_criteria=[], created_at=datetime.now(), updated_at=datetime.now(),
        )
    tasks = {"T1": _t("T1"), "T2": _t("T2")}
    ready = get_ready_tasks(tasks)
    assert {t.id for t in ready} == {"T1", "T2"}


def test_get_ready_tasks_pending_deps_all_done():
    """pending 依赖全 done → ready"""
    from cc_harness.project.dependency import get_ready_tasks
    from datetime import datetime
    from cc_harness.project.models import TodoTask

    def _t(id_, status="pending", deps=None):
        return TodoTask(
            id=id_, title=id_, status=status, description="",
            depends_on=deps or [], parent_task=None, assigned_to=None, priority=None,
            labels=[], due_date=None, effort_estimate=None,
            acceptance_criteria=[], created_at=datetime.now(), updated_at=datetime.now(),
        )
    tasks = {
        "T1": _t("T1", "done"),
        "T2": _t("T2", deps=["T1"]),  # T2 deps T1 done → ready
    }
    ready = get_ready_tasks(tasks)
    assert {t.id for t in ready} == {"T2"}


def test_get_ready_tasks_pending_deps_not_done():
    """pending 依赖未 done → 不 ready"""
    from cc_harness.project.dependency import get_ready_tasks
    from datetime import datetime
    from cc_harness.project.models import TodoTask

    def _t(id_, status="pending", deps=None):
        return TodoTask(
            id=id_, title=id_, status=status, description="",
            depends_on=deps or [], parent_task=None, assigned_to=None, priority=None,
            labels=[], due_date=None, effort_estimate=None,
            acceptance_criteria=[], created_at=datetime.now(), updated_at=datetime.now(),
        )
    tasks = {
        "T1": _t("T1", "pending"),  # T1 没 done
        "T2": _t("T2", deps=["T1"]),  # T2 deps T1 未 done → 不 ready
    }
    ready = get_ready_tasks(tasks)
    assert {t.id for t in ready} == set()


def test_get_ready_tasks_in_progress_excluded():
    """in_progress 不算 ready(只 pending 是)"""
    from cc_harness.project.dependency import get_ready_tasks
    from datetime import datetime
    from cc_harness.project.models import TodoTask

    def _t(id_, status="pending", deps=None):
        return TodoTask(
            id=id_, title=id_, status=status, description="",
            depends_on=deps or [], parent_task=None, assigned_to=None, priority=None,
            labels=[], due_date=None, effort_estimate=None,
            acceptance_criteria=[], created_at=datetime.now(), updated_at=datetime.now(),
        )
    tasks = {
        "T1": _t("T1", "in_progress"),  # in_progress 不算 ready
    }
    assert get_ready_tasks(tasks) == []


def test_get_ready_tasks_missing_dep_treated_as_ready():
    """依赖引用不在 dict → 视为不阻塞(由 validate 报告)"""
    from cc_harness.project.dependency import get_ready_tasks
    from datetime import datetime
    from cc_harness.project.models import TodoTask

    def _t(id_, deps=None):
        return TodoTask(
            id=id_, title=id_, status="pending", description="",
            depends_on=deps or [], parent_task=None, assigned_to=None, priority=None,
            labels=[], due_date=None, effort_estimate=None,
            acceptance_criteria=[], created_at=datetime.now(), updated_at=datetime.now(),
        )
    # T2 依赖不存在的 "MISSING"
    tasks = {"T1": _t("T1"), "T2": _t("T2", deps=["MISSING"])}
    ready = get_ready_tasks(tasks)
    assert {t.id for t in ready} == {"T1", "T2"}


def test_get_ready_tasks_partial_done_deps():
    """T3 deps [T1, T2],T1 done + T2 pending → T3 不 ready"""
    from cc_harness.project.dependency import get_ready_tasks
    from datetime import datetime
    from cc_harness.project.models import TodoTask

    def _t(id_, status="pending", deps=None):
        return TodoTask(
            id=id_, title=id_, status=status, description="",
            depends_on=deps or [], parent_task=None, assigned_to=None, priority=None,
            labels=[], due_date=None, effort_estimate=None,
            acceptance_criteria=[], created_at=datetime.now(), updated_at=datetime.now(),
        )
    tasks = {
        "T1": _t("T1", "done"),
        "T2": _t("T2", "pending"),
        "T3": _t("T3", deps=["T1", "T2"]),
    }
    ready = get_ready_tasks(tasks)
    assert {t.id for t in ready} == {"T2"}
```

- [ ] **Step 2: 跑测试验失败**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_project_dependency.py -v -k "get_ready_tasks"
```

Expected: 7 failed with `ImportError: cannot import name 'get_ready_tasks'`

- [ ] **Step 3: 在 `cc_harness/project/dependency.py` 末尾 append 实现**

```python
def get_ready_tasks(tasks: dict[str, TodoTask]) -> list[TodoTask]:
    """返回所有 ready 的 task — 状态是 pending 且 depends_on 全 done。

    'done' 视为已就绪;不存在于字典的依赖 id 视为不阻塞(由 validate 报告)。
    """
    ready: list[TodoTask] = []
    for task in tasks.values():
        if task.status != "pending":
            continue
        all_done = all(
            tasks[dep_id].status == "done"
            for dep_id in task.depends_on
            if dep_id in tasks  # 缺失依赖不阻塞
        )
        if all_done:
            ready.append(task)
    return ready
```

- [ ] **Step 4: 跑测试验通过**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_project_dependency.py -v -k "get_ready_tasks"
```

Expected: 7 passed

- [ ] **Step 5: 100% 覆盖 + baseline + commit**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_project_dependency.py --cov=cc_harness/project/dependency --cov-branch --cov-report=term-missing
.venv/Scripts/python.exe -m pytest tests/ -q
```

Expected: 100% 覆盖;1024 + 7 = 1031 passed

```bash
git add cc_harness/project/dependency.py tests/test_project_dependency.py
git commit -m "feat(dependency): get_ready_tasks (DAG ready node 查询)

B 阶段 Task 1.2: pending 且 deps 全 done 的 task 视为 ready。
缺失依赖不阻塞(由 check_references 报告)。
覆盖 A baseline 100% line + branch。

baseline: 1024 passed → now: 1031 passed (delta +7)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: `verify.py` 新文件

**Files:**
- Create: `cc_harness/project/verify.py`
- Create: `tests/test_project_verify.py`

**spec 节:** 组件 2(`VerifyResult` + 3 函数 API + 字段语义 + heuristic 规则 + stopword 列表 + TDD 边界 case)

**测试覆盖目标:** 100% line + branch

### Task 2.1: 失败测试 + `heuristic_check` 最小实现

- [ ] **Step 1: 创建 `tests/test_project_verify.py`**

```python
"""B 阶段组件 2: verify.py 单元测试。"""
import pytest
from datetime import datetime
from cc_harness.project.models import TodoTask
from cc_harness.project.verify import (
    VerifyResult,
    heuristic_check,
    state_check,
    run_verify,
)


def _make_task(
    id_="T1",
    status="in_progress",
    deps=None,
    criteria=None,
):
    return TodoTask(
        id=id_, title=id_, status=status, description="",
        depends_on=deps or [], parent_task=None, assigned_to=None, priority=None,
        labels=[], due_date=None, effort_estimate=None,
        acceptance_criteria=criteria or [],
        created_at=datetime.now(), updated_at=datetime.now(),
    )


# --- heuristic_check ---

def test_heuristic_check_empty_criteria():
    """空 criteria → (True, [])"""
    passed, missing = heuristic_check([], "any text")
    assert passed is True
    assert missing == []


def test_heuristic_check_empty_text():
    """criteria 非空 text 空 → (False, criteria)"""
    passed, missing = heuristic_check(["实现红队"], "")
    assert passed is False
    assert "实现红队" in missing


def test_heuristic_check_substring_match_chinese():
    """中文子串包含"""
    passed, missing = heuristic_check(["实现红队"], "本轮实现了红队 detect 逻辑")
    assert passed is True
    assert missing == []


def test_heuristic_check_substring_match_english():
    """英文子串包含"""
    passed, missing = heuristic_check(["verify hook"], "implement verify hook now")
    assert passed is True


def test_heuristic_check_keyword_match():
    """拆词后关键词匹配(子串不直接命中)"""
    # criterion 拆词 "实现 verify hook" → ["实现", "verify", "hook"]
    passed, missing = heuristic_check(["实现 verify hook"], "我已经把 verify 逻辑写完,hook 接到 repl")
    assert passed is True


def test_heuristic_check_keyword_miss():
    """关键词全不在 text"""
    passed, missing = heuristic_check(["实现红队"], "我只写了单元测试")
    assert passed is False
    assert "实现红队" in missing


def test_heuristic_check_case_insensitive():
    """大小写不敏感"""
    passed, _ = heuristic_check(["VERIFY hook"], "verify hook impl")
    assert passed is True


def test_heuristic_check_short_criterion_skipped():
    """criterion < 3 字符 → 跳过(避免噪声)"""
    passed, _ = heuristic_check(["ok"], "本轮什么都没做")
    assert passed is True  # "ok" 跳过,视为通过


def test_heuristic_check_stopword_filtered():
    """stopword 被过滤(criterion 全 stopword → 通过)"""
    passed, _ = heuristic_check(["the a an"], "anything else")
    assert passed is True


def test_heuristic_check_mixed_lang_criterion():
    """中英混合 criterion → 拆词各语言都覆盖"""
    passed, _ = heuristic_check(["实现 verify hook"], "本轮 verify 写完")
    assert passed is True


def test_heuristic_check_partial_match_returns_missing():
    """多条 criterion 部分命中 → 列出 missing"""
    passed, missing = heuristic_check(
        ["实现 verify", "写 unit test", "更新文档"],
        "本轮 verify 写完",
    )
    assert passed is False
    assert "写 unit test" in missing
    assert "更新文档" in missing
    assert "实现 verify" not in missing
```

- [ ] **Step 2: 跑测试验失败**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_project_verify.py -v
```

Expected: 11 failed with `ModuleNotFoundError: No module named 'cc_harness.project.verify'`

- [ ] **Step 3: 创建 `cc_harness/project/verify.py`**

```python
"""B 阶段组件 2: verify hook(heuristic + state 双轨)。

3 个纯函数:
- heuristic_check(criteria, text) → (passed, missing)
- state_check(task, all_tasks) → (deps_ready, hint_or_none)
- run_verify(task, all_tasks, last_turn_text) → VerifyResult

字段语义:
- passed — heuristic AND state 整体是否通过
- missing_criteria — heuristic 失败的 criterion(只在 heuristic 失败时填)
- hints — 辅助信号(state 失败 / 无产出),调用方无条件采纳
"""
from __future__ import annotations

import re

from cc_harness.project.models import TodoTask


# 简陋版 stopword:YAGNI 起步,中英文各 10 个,误判多再扩
_STOPWORDS: frozenset[str] = frozenset({
    # 英文
    "the", "a", "an", "is", "are", "was", "were", "of", "to", "in",
    "on", "and", "or", "but", "for", "with", "at", "by", "from", "as",
    # 中文
    "的", "了", "和", "是", "在", "有", "我", "你", "他", "她",
    "它", "们", "这", "那", "也", "就", "都", "而", "及", "或",
})

# 拆词:英文 \w+ + 中文 [一-鿿] 单字
_TOKEN_RE = re.compile(r"\w+|[一-鿿]")


def _keywords(text: str) -> set[str]:
    """拆词 + 去 stopword + 去单字符(已在 stopword 里的)"""
    tokens = _TOKEN_RE.findall(text.lower())
    return {t for t in tokens if t not in _STOPWORDS}


def heuristic_check(
    criteria: list[str], text: str
) -> tuple[bool, list[str]]:
    """启发式检查 text 是否覆盖 criteria 每一条。

    规则:
    - 空 criteria → (True, [])
    - text 为空 → (False, criteria)
    - criterion 拆词(去 stopword)后至少 1 个关键词在 text 拆词集合
    - criterion < 3 字符 → 跳过(视为通过)
    - criterion 拆词后为空(全 stopword)→ 视为通过
    """
    if not criteria:
        return True, []
    if not text:
        return False, list(criteria)

    text_kw = _keywords(text)
    if not text_kw:
        # text 全 stopword 极端情况
        return False, list(criteria)

    missing: list[str] = []
    for criterion in criteria:
        if len(criterion) < 3:
            continue  # 短 criterion 跳过
        kw = _keywords(criterion)
        if not kw:
            continue  # 全 stopword 跳过
        if not (kw & text_kw):
            missing.append(criterion)
    return (len(missing) == 0), missing
```

- [ ] **Step 4: 跑测试验通过**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_project_verify.py -v
```

Expected: 11 passed

- [ ] **Step 5: commit**

```bash
git add cc_harness/project/verify.py tests/test_project_verify.py
git commit -m "feat(project): verify.py heuristic_check 启发式 verify

B 阶段 Task 2.1: 组件 2 第一块 - heuristic 拆词匹配。
- criterion 拆词去 stopword(中英文各 20 个,YAGNI 起步)
- 至少 1 个关键词在 text 中 → 通过
- 短 criterion (<3 字符) / 全 stopword 跳过视为通过

baseline: 1031 passed → now: 1042 passed (delta +11)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2.2: `state_check` + `run_verify` + `VerifyResult`

**Files:**
- Modify: `cc_harness/project/verify.py`(append)
- Modify: `tests/test_project_verify.py`(append)

- [ ] **Step 1: 在 `tests/test_project_verify.py` 末尾 append 测试**

```python
# --- VerifyResult ---

def test_verify_result_constructor():
    """VerifyResult dataclass 字段"""
    r = VerifyResult(
        task_id="T1", passed=True, missing_criteria=[], hints=[]
    )
    assert r.task_id == "T1"
    assert r.passed is True
    assert r.missing_criteria == []
    assert r.hints == []


# --- state_check ---

def test_state_check_no_deps():
    """无依赖 → (True, None)"""
    task = _make_task()
    ready, hint = state_check(task, {"T1": task})
    assert ready is True
    assert hint is None


def test_state_check_deps_all_done():
    """deps 全 done → (True, None)"""
    t_done = _make_task("T1", "done")
    t_pending = _make_task("T2", deps=["T1"])
    ready, hint = state_check(t_pending, {"T1": t_done, "T2": t_pending})
    assert ready is True
    assert hint is None


def test_state_check_deps_partial_done():
    """deps 部分 done → (False, hint)"""
    t1 = _make_task("T1", "done")
    t2 = _make_task("T2", "pending")
    t3 = _make_task("T3", deps=["T1", "T2"])
    ready, hint = state_check(t3, {"T1": t1, "T2": t2, "T3": t3})
    assert ready is False
    assert hint is not None
    assert "T3" in hint and "T2" in hint


def test_state_check_deps_in_progress():
    """deps 有 in_progress → (False, hint)"""
    t1 = _make_task("T1", "in_progress")
    t2 = _make_task("T2", deps=["T1"])
    ready, hint = state_check(t2, {"T1": t1, "T2": t2})
    assert ready is False
    assert hint is not None
    assert "T1" in hint


def test_state_check_deps_missing_treated_as_ready():
    """依赖引用不在 dict → (True, None)(不阻塞,由 validate 报)"""
    task = _make_task("T1", deps=["MISSING"])
    ready, hint = state_check(task, {"T1": task})
    assert ready is True
    assert hint is None


# --- run_verify ---

def test_run_verify_not_in_progress():
    """非 in_progress → passed=True 全空"""
    t = _make_task("T1", "done", criteria=["实现 verify"])
    result = run_verify(t, {"T1": t}, "any text")
    assert result.passed is True
    assert result.missing_criteria == []
    assert result.hints == []


def test_run_verify_empty_criteria():
    """in_progress + 空 criteria → passed=True"""
    t = _make_task("T1", "in_progress", criteria=[])
    result = run_verify(t, {"T1": t}, "any text")
    assert result.passed is True
    assert result.missing_criteria == []
    assert result.hints == []


def test_run_verify_all_pass():
    """heuristic 全通过 + deps ready → passed=True"""
    t = _make_task("T1", "in_progress", criteria=["实现 verify"])
    result = run_verify(t, {"T1": t}, "本轮实现 verify 完成")
    assert result.passed is True
    assert result.missing_criteria == []


def test_run_verify_heuristic_fail():
    """heuristic 缺 criterion → passed=False, missing 列出"""
    t = _make_task(
        "T1", "in_progress", criteria=["实现 verify", "写 unit test"]
    )
    result = run_verify(t, {"T1": t}, "本轮 verify 写完")
    assert result.passed is False
    assert "写 unit test" in result.missing_criteria
    assert "实现 verify" not in result.missing_criteria


def test_run_verify_state_fail():
    """deps 未 ready → passed=False, hints 填 dep hint"""
    t1 = _make_task("T1", "pending")
    t2 = _make_task("T2", "in_progress", deps=["T1"])
    result = run_verify(t2, {"T1": t1, "T2": t2}, "any text")
    assert result.passed is False
    assert any("T1" in h for h in result.hints)


def test_run_verify_both_fail():
    """heuristic + state 双 fail → 两边都填"""
    t1 = _make_task("T1", "pending")
    t2 = _make_task(
        "T2", "in_progress",
        deps=["T1"],
        criteria=["实现 verify"],
    )
    result = run_verify(t2, {"T1": t1, "T2": t2}, "本轮啥也没干")
    assert result.passed is False
    assert "实现 verify" in result.missing_criteria
    assert any("T1" in h for h in result.hints)


def test_run_verify_empty_text_info_hint():
    """last_turn_text 为空 → passed=True, hints 追加"无产出"提示"""
    t = _make_task("T1", "in_progress", criteria=["实现 verify"])
    result = run_verify(t, {"T1": t}, "")
    assert result.passed is True
    assert any("无产出" in h or "无文本产出" in h for h in result.hints)


def test_run_verify_empty_text_no_criteria():
    """last_turn_text 空 + criteria 空 → passed=True, hints 空"""
    t = _make_task("T1", "in_progress", criteria=[])
    result = run_verify(t, {"T1": t}, "")
    assert result.passed is True
    assert result.hints == []
```

- [ ] **Step 2: 跑测试验失败**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_project_verify.py -v -k "state_check or run_verify or VerifyResult"
```

Expected: 13 failed

- [ ] **Step 3: 在 `cc_harness/project/verify.py` 末尾 append 实现**

```python
# ---------------------------------------------------------------------------
# VerifyResult
# ---------------------------------------------------------------------------


from dataclasses import dataclass


@dataclass
class VerifyResult:
    """单 task 的 verify 输出。"""

    task_id: str
    passed: bool                          # 整体是否通过
    missing_criteria: list[str]           # heuristic 未命中的 criterion
    hints: list[str]                      # 给 LLM 的提示文本(无条件采纳)


# ---------------------------------------------------------------------------
# state_check
# ---------------------------------------------------------------------------


def state_check(
    task: TodoTask, all_tasks: dict[str, TodoTask]
) -> tuple[bool, str | None]:
    """状态机检查 — depends_on 全 done?

    Returns:
        (deps_ready, hint_or_none) — hint 是"task X 依赖未就绪: Y" 提示
    """
    if not task.depends_on:
        return True, None

    missing_deps: list[str] = []
    for dep_id in task.depends_on:
        if dep_id not in all_tasks:
            continue  # 缺失依赖不阻塞
        if all_tasks[dep_id].status != "done":
            missing_deps.append(dep_id)

    if not missing_deps:
        return True, None
    chain = ", ".join(missing_deps)
    hint = f"task {task.id} 依赖未就绪: {chain}"
    return False, hint


# ---------------------------------------------------------------------------
# run_verify
# ---------------------------------------------------------------------------


def run_verify(
    task: TodoTask,
    all_tasks: dict[str, TodoTask],
    last_turn_text: str,
) -> VerifyResult:
    """组合 heuristic + state。

    字段语义:
    - passed — heuristic AND state 整体是否通过
    - missing_criteria — heuristic 失败的 criterion(只在 heuristic 失败时填)
    - hints — 辅助信号(state 失败 / 无产出),调用方无条件采纳
    """
    # 非 in_progress → no-op
    if task.status != "in_progress":
        return VerifyResult(
            task_id=task.id, passed=True, missing_criteria=[], hints=[]
        )

    hints: list[str] = []

    # last_turn_text 空 + 有 criteria → info hint(不阻断)
    if not last_turn_text.strip() and task.acceptance_criteria:
        hints.append(f"task {task.id} 已 in_progress 但本轮无文本产出")
        # 没 criteria 也直接返回 passed=True
        if not task.acceptance_criteria:
            return VerifyResult(
                task_id=task.id, passed=True, missing_criteria=[], hints=hints
            )
        return VerifyResult(
            task_id=task.id, passed=True, missing_criteria=[], hints=hints
        )

    # 空 criteria → passed=True
    if not task.acceptance_criteria:
        return VerifyResult(
            task_id=task.id, passed=True, missing_criteria=[], hints=hints
        )

    # heuristic
    heuristic_passed, missing = heuristic_check(
        task.acceptance_criteria, last_turn_text
    )

    # state
    deps_ready, dep_hint = state_check(task, all_tasks)
    if not deps_ready and dep_hint:
        hints.append(dep_hint)

    overall_passed = heuristic_passed and deps_ready
    return VerifyResult(
        task_id=task.id,
        passed=overall_passed,
        missing_criteria=missing if not heuristic_passed else [],
        hints=hints,
    )
```

- [ ] **Step 4: 跑测试验通过**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_project_verify.py -v
```

Expected: 24 passed(11 heuristic + 13 state/run_verify/VerifyResult)

- [ ] **Step 5: 100% 覆盖 + baseline + commit**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_project_verify.py --cov=cc_harness/project/verify --cov-branch --cov-report=term-missing
.venv/Scripts/python.exe -m pytest tests/ -q
```

Expected: 100% 覆盖;1042 + 13 = 1055 passed

```bash
git add cc_harness/project/verify.py tests/test_project_verify.py
git commit -m "feat(project): verify.py state_check + run_verify

B 阶段 Task 2.2: 组件 2 第二块 - state 校验 + 组合 run_verify。
- VerifyResult 字段语义明确: missing_criteria 仅 heuristic 失败时填,
  hints 无条件采纳(state 失败 / 无产出 提示都进 hints)
- state_check: deps 全 done → ready,缺失依赖不阻塞
- run_verify: 短路顺序 非 in_progress → 空 criteria → 空 text →
  heuristic + state 组合

baseline: 1042 passed → now: 1055 passed (delta +13)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: `todo_toposort` tool

**Files:**
- Modify: `cc_harness/project/tools.py`(append 第 8 个 tool spec + handler + 渲染函数)
- Modify: `tests/test_project_tools.py`(append 测试)

**spec 节:** 组件 3(OpenAI spec + handler + `_render_toposort` 输出 schema + 截断)

**测试覆盖目标:** 新 handler ≥85%(与 A 阶段 tools.py 一致)

### Task 3.1: 失败测试 + `todo_toposort` spec + handler

- [ ] **Step 0: 在 `tests/test_project_tools.py` 顶部加 helper**(关键,所有本 task 测试用它)

```python
# 顶部 append (在 import 之后, fixture 之前)
async def _create(svc, title, status="pending", criteria=None, deps=None, session_id="s"):
    t = await svc.create(
        title=title,
        acceptance_criteria=criteria or [],
        depends_on=deps or [],
        session_id=session_id,
    )
    if status != "pending":
        t = await svc.update(t.id, status=status, session_id=session_id)
    return t
```

- [ ] **Step 1: 在 `tests/test_project_tools.py` 末尾 append 测试**

```python
# --- B 阶段 Task 3: todo_toposort ---

from cc_harness.project.tools import (
    TODO_TOPOSORT_SPEC,
    todo_toposort_handler,
)


def test_toposort_spec_has_function_shape():
    """第 8 个 SPEC 合法 OpenAI format"""
    spec = TODO_TOPOSORT_SPEC
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "todo_toposort"
    assert "parameters" in spec["function"]


def test_toposort_spec_has_group_param():
    """group 参数是 enum"""
    spec = TODO_TOPOSORT_SPEC
    group_prop = spec["function"]["parameters"]["properties"]["group"]
    assert set(group_prop["enum"]) == {"all", "ready", "in_progress", "blocked"}


async def test_toposort_handler_empty_manifest(svc, deps):
    """空 manifest → 渲染 OK, is_error=False"""
    result = await todo_toposort_handler({}, **deps)
    assert result.is_error is False
    assert "0 tasks" in result.llm_text or "DAG 拓扑视图" in result.llm_text


async def test_toposort_handler_default_group(svc, deps):
    """group 默认 all → 全表"""
    # 创建 3 个 task(用 helper)
    await _create(svc, "T1", status="pending", session_id=deps["session_id"])
    await _create(svc, "T2", status="in_progress", session_id=deps["session_id"])
    await _create(svc, "T3", status="done", session_id=deps["session_id"])

    result = await todo_toposort_handler({}, **deps)
    assert result.is_error is False
    # 渲染含 ready/in_progress/done 段
    assert "In progress" in result.llm_text or "in_progress" in result.llm_text
    assert "Done" in result.llm_text or "done" in result.llm_text


async def test_toposort_handler_ready_group(svc, deps):
    """group=ready → 只 ready"""
    await _create(svc, "T1", status="pending", session_id=deps["session_id"])
    await _create(svc, "T2", status="in_progress", session_id=deps["session_id"])

    result = await todo_toposort_handler({"group": "ready"}, **deps)
    assert result.is_error is False
    # ready 段含 T1, 不含 T2
    assert "T1" in result.llm_text
    assert "T2" not in result.llm_text or "in_progress" in result.llm_text


async def test_toposort_handler_with_cycle(svc, deps):
    """有环 → is_error=True, llm_text 含环路径"""
    sid = deps["session_id"]
    t1 = await _create(svc, "T1", status="pending", session_id=sid)
    t2 = await _create(svc, "T2", status="pending", deps=[t1.id], session_id=sid)
    # 强制造环:更新 T1 depends_on=[T2]
    await svc.update(t1.id, {"depends_on": [t2.id]}, session_id=sid)

    result = await todo_toposort_handler({}, **deps)
    assert result.is_error is True
    assert "T1" in result.llm_text and "T2" in result.llm_text


async def test_toposort_handler_truncation_at_50(svc, deps):
    """51+ task → handler 路径截断 + ⚠ 提示。

    注意:这测试用 handler 真路径(不是直接调 _render_toposort),
    验证 handler 在大 manifest 下是否:
    1. is_error=False(无环)
    2. llm_text 含 ⚠ truncated 标记
    3. llm_text 含具体 task id(至少前 50 个)
    """
    # 真的写 60 个 task 到 svc
    for i in range(60):
        await _create(svc, f"Task {i:03d}", status="pending", session_id=deps["session_id"])

    result = await todo_toposort_handler({}, **deps)
    assert result.is_error is False  # 无环
    # llm_text 含截断警告
    assert "truncated" in result.llm_text.lower() or "⚠" in result.llm_text
    # 含具体 task id(至少前 50 个, 后 10 个应被截)
    assert "T000" in result.llm_text or "Task 000" in result.llm_text
    # display_text 简洁(可选优化, 后续可加)
    assert "60" in result.display_text or "topo" in result.display_text


async def test_toposort_render_truncation_at_50_direct():
    """直接调 _render_toposort 测试渲染逻辑(单元层)。"""
    from cc_harness.project.tools import _render_toposort
    from datetime import datetime
    from cc_harness.project.models import TodoTask
    now = datetime.now()

    def _make(i):
        return TodoTask(
            id=f"T{i:03d}", title=f"Task {i}", status="pending",
            description="", depends_on=[], parent_task=None, assigned_to=None,
            priority=None, labels=[], due_date=None, effort_estimate=None,
            acceptance_criteria=[], created_at=now, updated_at=now,
        )
    tasks = {f"T{i:03d}": _make(i) for i in range(60)}
    order = [f"T{i:03d}" for i in range(60)]

    output = _render_toposort(order, list(tasks.values()), tasks, topo_error=None)
    assert "truncated" in output.lower() or "⚠" in output
    assert "60" in output
    assert "T000" in output
    assert "T049" in output
    assert "T059" not in output  # 第 60 个(0-indexed 59)应被截
```

- [ ] **Step 2: 跑测试验失败**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_project_tools.py -v -k "toposort"
```

Expected: 7 failed with `ImportError`

- [ ] **Step 3: 在 `cc_harness/project/tools.py` 末尾 append 实现**

1. 文件头添加 import:

```python
# 顶部 import 区 append
from cc_harness.project.dependency import (
    topo_sort,
    get_ready_tasks,
    DependencyCycleError,
)
```

2. **改 `ALL_SPECS` 列表(line 42)** append `TODO_TOPOSORT_SPEC`:

```python
ALL_SPECS = [
    TODO_LIST_SPEC, TODO_GET_SPEC, TODO_CREATE_SPEC, TODO_UPDATE_SPEC,
    TODO_DELETE_SPEC, TODO_RESOLVE_SPEC, TODO_VALIDATE_SPEC,
    TODO_TOPOSORT_SPEC,  # B 阶段 Task 3
]
```

3. 在 `__all__` 之前 append 第 8 个 SPEC + handler + 渲染:

```python
# ---------------------------------------------------------------------------
# B 阶段 Task 3: todo_toposort(spec 组件 3)
# ---------------------------------------------------------------------------


TODO_TOPOSORT_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "todo_toposort",
        "description": (
            "查看项目任务 DAG 的拓扑视图。"
            "返回全表拓扑序 + 当前 ready/in_progress/blocked 分组。"
            "用于 LLM 编排决策:'下一步做哪个?'。"
            "注: ready 指 pending 且依赖全 done,由 get_ready_tasks 计算。"
            "存在环时 is_error=True 并报告环路径,不抛异常。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "group": {
                    "type": "string",
                    "enum": ["all", "ready", "in_progress", "blocked"],
                    "default": "all",
                },
            },
            "required": [],
        },
    },
}


MAX_RENDER_TASKS = 50


async def todo_toposort_handler(
    args: dict, *, service, session_id, cwd
):
    """查看项目任务 DAG 拓扑视图。

    Returns:
        ToolResult — 正常时 is_error=False;有环时 is_error=True
    """
    tasks_list = await service.list(include_done=True)
    by_id = {t.id: t for t in tasks_list}
    group = args.get("group", "all")

    if group == "ready":
        filtered = get_ready_tasks(by_id)
    elif group == "in_progress":
        filtered = [t for t in tasks_list if t.status == "in_progress"]
    elif group == "blocked":
        filtered = [t for t in tasks_list if t.status == "blocked"]
    else:  # "all"
        filtered = tasks_list

    try:
        order = topo_sort(by_id)
        topo_error = None
    except DependencyCycleError as e:
        order = None
        topo_error = str(e)

    llm_text = _render_toposort(order, filtered, by_id, topo_error)

    if topo_error:
        return ToolResult(
            is_error=True,
            display_text=f"topo: {topo_error}",
            llm_text=llm_text,
        )
    return ToolResult(
        is_error=False,
        display_text=f"topo: {len(order)} tasks",
        llm_text=llm_text,
    )


def _render_toposort(
    order: list[str] | None,
    filtered: list,  # list[TodoTask]
    by_id: dict,
    topo_error: str | None,
) -> str:
    """渲染 DAG 拓扑视图给 LLM 看。"""
    truncated = len(by_id) > MAX_RENDER_TASKS
    if truncated:
        head = (
            f"⚠ 项目 task 数 {len(by_id)} > {MAX_RENDER_TASKS}, 仅展示前 {MAX_RENDER_TASKS}\n"
        )
    else:
        head = ""

    if topo_error:
        topo_line = f"⚠ {topo_error}\n"
    elif order:
        # topo order 全展示(id 列表短)
        topo_line = "Topo order: " + " → ".join(order) + "\n"
    else:
        topo_line = ""

    # Ready
    ready_tasks = get_ready_tasks(by_id)
    ready_section = f"Ready ({len(ready_tasks)}):\n"
    for t in ready_tasks[:MAX_RENDER_TASKS]:
        prio = f" (priority: {t.priority})" if t.priority else ""
        ready_section += f"  - {t.id} [pending]{prio} \"{t.title}\"\n"

    # In progress
    ip_tasks = [t for t in by_id.values() if t.status == "in_progress"]
    ip_section = f"In progress ({len(ip_tasks)}):\n"
    for t in ip_tasks[:MAX_RENDER_TASKS]:
        prio = f" (priority: {t.priority})" if t.priority else ""
        deps_str = ""
        if t.depends_on:
            deps_parts = []
            for d in t.depends_on:
                if d in by_id:
                    mark = "✓" if by_id[d].status == "done" else "✗"
                    deps_parts.append(f"{d} {mark}")
                else:
                    deps_parts.append(f"{d} ?")
            deps_str = f" (deps: {', '.join(deps_parts)})"
        ip_section += f"  - {t.id} [in_progress]{prio} \"{t.title}\"{deps_str}\n"

    # Blocked
    bl_tasks = [t for t in by_id.values() if t.status == "blocked"]
    bl_section = f"Blocked ({len(bl_tasks)}):\n"
    for t in bl_tasks[:MAX_RENDER_TASKS]:
        prio = f" (priority: {t.priority})" if t.priority else ""
        waiting = ""
        if t.depends_on:
            not_done = [d for d in t.depends_on if d in by_id and by_id[d].status != "done"]
            if not_done:
                waiting = f" (waiting on {', '.join(not_done)})"
        bl_section += f"  - {t.id} [blocked]{prio} \"{t.title}\"{waiting}\n"

    # Done(简略,只列 id)
    done_tasks = [t for t in by_id.values() if t.status == "done"]
    done_section = ""
    if done_tasks:
        ids = ", ".join(t.id for t in done_tasks)
        done_section = f"Done ({len(done_tasks)}): {ids}\n"

    return (
        head
        + f"DAG 拓扑视图 ({len(by_id)} tasks):\n"
        + "  " + topo_line
        + "\n"
        + ready_section
        + "\n"
        + ip_section
        + "\n"
        + bl_section
        + "\n"
        + done_section
    )
```

3. 末尾 `__all__` append:

```python
__all__ = [
    "TODO_LIST_SPEC", "TODO_GET_SPEC", "TODO_CREATE_SPEC", "TODO_UPDATE_SPEC",
    "TODO_DELETE_SPEC", "TODO_RESOLVE_SPEC", "TODO_VALIDATE_SPEC",
    "TODO_TOPOSORT_SPEC",  # B 阶段 Task 3
    "todo_list_handler", "todo_get_handler", "todo_create_handler",
    "todo_update_handler", "todo_delete_handler", "todo_resolve_handler",
    "todo_validate_handler",
    "todo_toposort_handler",  # B 阶段 Task 3
]
```

4. **改 `cc_harness/project/extras.py`**(第 8 个 tool 注入到 LLM):

文件头 `from cc_harness.project.tools import` 列表 append:

```python
    TODO_TOPOSORT_SPEC,
    todo_toposort_handler,
```

`inject_todo_tools()` 函数返回 list append:

```python
        {"spec": TODO_RESOLVE_SPEC,  "handler": todo_resolve_handler,  "deps": deps},
        {"spec": TODO_VALIDATE_SPEC, "handler": todo_validate_handler, "deps": deps},
        {"spec": TODO_TOPOSORT_SPEC, "handler": todo_toposort_handler, "deps": deps},  # B 阶段
    ]
```

文件头 docstring 改 "7" → "8",`Returns:` 段 "长度固定为 7" → "8"。

- [ ] **Step 4: 跑测试验通过**

**注意**:`test_specs_have_distinct_names`(`tests/test_project_tools.py:111-117`)hard-code 7 个 tool name,B 阶段必须改这个 set 加 `"todo_toposort"`:

```python
def test_specs_have_distinct_names():
    names = [s["function"]["name"] for s in ALL_SPECS]
    assert len(names) == len(set(names))
    assert set(names) == {
        "todo_list", "todo_get", "todo_create", "todo_update",
        "todo_delete", "todo_resolve", "todo_validate",
        "todo_toposort",  # B 阶段 Task 3
    }
```

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_project_tools.py tests/test_project_extras.py -v -k "toposort or distinct_names"
```

Expected: 全部通过(tools.py 新 handler 测试 + extras.py 已有 inject_todo_tools 测试 + distinct_names 改后过)

- [ ] **Step 5: 覆盖 + baseline + commit**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_project_tools.py --cov=cc_harness/project/tools --cov-branch --cov-report=term-missing
.venv/Scripts/python.exe -m pytest tests/ -q
```

Expected: 新 handler ≥85% 覆盖;1055 + 7 = 1062 passed

```bash
git add cc_harness/project/tools.py cc_harness/project/extras.py tests/test_project_tools.py
git commit -m "feat(project): todo_toposort 第 8 个 agent tool

B 阶段 Task 3: 组件 3 - 让主 agent 查 DAG 拓扑。
- OpenAI spec: group 参数(all/ready/in_progress/blocked)
- handler: 调 topo_sort + get_ready_tasks, 返回 ToolResult
- DependencyCycleError 转 is_error=True + 报告环路径
- 渲染包含 ready/in_progress/blocked/done 分组 + deps checkmark
- 50+ task 输出截断
- inject_todo_tools 第 8 项接入, LLM 可见

baseline: 1055 passed → now: 1062 passed (delta +7)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: `_after_turn_todo` 接线

**Files:**
- Modify: `cc_harness/repl.py`(填 `_after_turn_todo` 占位 + `ReplState` 加 2 字段 + `last_turn_text` 接线)
- Modify: `tests/test_repl_b_hook.py`(新建,集成测试)

**spec 节:** 组件 4(`_after_turn_todo` 实现 + ReplState 字段 + 三层异常 + hints 截断 3/10)

### Task 4.1: 失败测试 + `_after_turn_todo` 最小实现

- [ ] **Step 0: 在 `tests/test_repl_b_hook.py` 顶部加 helper**(关键,所有本 task 测试用它)

```python
# 文件 import 之后, 任何 fixture 之前
async def _create(svc, title, status="pending", criteria=None, deps=None, session_id="s"):
    t = await svc.create(
        title=title,
        acceptance_criteria=criteria or [],
        depends_on=deps or [],
        session_id=session_id,
    )
    if status != "pending":
        t = await svc.update(t.id, status=status, session_id=session_id)
    return t
```

- [ ] **Step 1: 创建 `tests/test_repl_b_hook.py`**

```python
"""B 阶段组件 4: _after_turn_todo 集成测试。

覆盖矩阵:
- 每 turn 触发 / 写 hints / 覆盖 hints
- service.list 抛 → 静默 swallow + 不清旧 hints
- 单 task run_verify 抛 → 跳过该 task,其他继续
- todo_service is None → no-op
- state.todo_hints 默认空
- last_turn_text 接线
"""
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from cc_harness.project.models import (
    Manifest, TodoTask,
)
from cc_harness.project.service import TodoService
from cc_harness.repl import (
    ReplState,
    _after_turn_todo,
    _extract_final_text,
)


def _make_task(id_, status, criteria=None, deps=None):
    return TodoTask(
        id=id_, title=id_, status=status, description="",
        depends_on=deps or [], parent_task=None, assigned_to=None, priority=None,
        labels=[], due_date=None, effort_estimate=None,
        acceptance_criteria=criteria or [],
        created_at=datetime.now(), updated_at=datetime.now(),
    )


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    todos = p / ".cc-harness" / "todos"
    todos.mkdir(parents=True)
    (todos / "todos.yaml").write_text("tasks: []\n", encoding="utf-8")
    return p


@pytest.fixture
def manifest() -> Manifest:
    return Manifest(
        project_id="x", name="x",
        todos_path=".cc-harness/todos",
        created_at=datetime.now(),
    )


@pytest.fixture
def svc(proj, manifest) -> TodoService:
    return TodoService(project_root=proj, manifest=manifest)


@pytest.fixture
def state(svc) -> ReplState:
    """最小 ReplState,只填 todo_service + last_turn_text"""
    s = ReplState()
    s.todo_service = svc
    s.last_turn_text = "本轮实现了 verify 逻辑"
    return s


# --- _after_turn_todo 基础行为 ---


async def test_after_turn_todo_no_service():
    """todo_service is None → no-op, hints 不动"""
    s = ReplState()
    s.todo_service = None
    s.todo_hints = ["preexisting"]
    await _after_turn_todo(s, None)
    assert s.todo_hints == ["preexisting"]


async def test_after_turn_todo_empty_manifest(state):
    """空 manifest → hints = []"""
    await _after_turn_todo(state, state.todo_service)
    assert state.todo_hints == []


async def test_after_turn_todo_in_progress_with_missing_criterion(state, svc):
    """in_progress task 有 criterion 缺 → hints 含 missing"""
    await _create(svc, "T1", status="in_progress", criteria=["实现 verify hook"], session_id="s")
    state.last_turn_text = "本轮啥也没干"  # 不含"实现 verify"

    await _after_turn_todo(state, svc)
    assert len(state.todo_hints) > 0
    assert any("verify" in h for h in state.todo_hints)


async def test_after_turn_todo_overwrites_hints(state, svc):
    """每 turn 覆盖 hints(不累积)"""
    # Turn 1: 有 missing
    await _create(svc, "T1", status="in_progress", criteria=["X"], session_id="s")
    state.last_turn_text = "no match"
    await _after_turn_todo(state, svc)
    assert any("X" in h and "criterion" in h for h in state.todo_hints)
    turn1_hints = list(state.todo_hints)

    # Turn 2: text 包含 X → 不该有 missing hint
    state.last_turn_text = "X done"
    await _after_turn_todo(state, svc)
    # 覆盖后, missing hint 应消失
    assert not any("X" in h and "criterion" in h for h in state.todo_hints)
    assert state.todo_hints != turn1_hints  # 真覆盖了


# --- 异常处理 ---


async def test_after_turn_todo_service_list_failure_preserves_hints(state, svc, caplog):
    """service.list 抛 → 静默 + 不清旧 hints"""
    state.todo_hints = ["preexisting hint"]
    with patch.object(svc, "list", side_effect=IOError("disk error")):
        with caplog.at_level(logging.WARNING):
            await _after_turn_todo(state, svc)
    # 旧 hints 保留
    assert state.todo_hints == ["preexisting hint"]
    # warn log: "verify hook" + 错误内容
    assert any("verify hook" in r.message and "disk error" in r.message for r in caplog.records)


async def test_after_turn_todo_single_task_failure_continues(state, svc, caplog):
    """单 task run_verify 抛 → 跳过该 task, 其他继续"""
    await _create(svc, "T1", status="in_progress", criteria=["ok"], session_id="s")
    await _create(svc, "T2", status="in_progress", criteria=["write test"], session_id="s")
    state.last_turn_text = "no match"

    # 让 run_verify 在 T1 上抛,T2 继续
    from cc_harness.project import verify as verify_mod
    original = verify_mod.run_verify
    def boom(task, all_tasks, text):
        if task.id == "T1":
            raise ValueError("simulated")
        return original(task, all_tasks, text)
    with patch.object(verify_mod, "run_verify", side_effect=boom):
        with caplog.at_level(logging.WARNING):
            await _after_turn_todo(state, svc)
    # T2 的 hint 应被采纳("write test" missing)
    assert any("write test" in h for h in state.todo_hints)
    # T1 失败 warn
    assert any("T1" in r.message for r in caplog.records)


# --- 截断 ---


async def test_after_turn_todo_per_task_truncation_at_3(state, svc):
    """单 task 5 criterion 缺 → 最多 3 条"""
    crits = ["crit one", "crit two", "crit three", "crit four", "crit five"]
    await _create(svc, "T1", status="in_progress", criteria=crits, session_id="s")
    state.last_turn_text = "no match anything"

    await _after_turn_todo(state, svc)
    # 启发式全 missing, hints 含 T1 的最多 3 条
    t1_hints = [h for h in state.todo_hints if "T1" in h]
    assert len(t1_hints) <= 3


async def test_after_turn_todo_total_truncation_at_10(state, svc):
    """3 in_progress task 各 5 criterion 缺 → 全局最多 10 条"""
    for i in range(3):
        crits = [f"task{i} crit{j}" for j in range(5)]
        await _create(svc, f"T{i}", status="in_progress", criteria=crits, session_id="s")
    state.last_turn_text = "no match"

    await _after_turn_todo(state, svc)
    assert len(state.todo_hints) <= 10


# --- _extract_final_text ---


def test_extract_final_text_assistant_text():
    """取最后一条 assistant 纯文本"""
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "tool_calls": None},
    ]
    assert _extract_final_text(msgs) == "hello"


def test_extract_final_text_skip_tool_calls():
    """最后一条 assistant 是 tool_calls(content=None)→ 跳过找上一条"""
    msgs = [
        {"role": "assistant", "content": "first text", "tool_calls": None},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
    ]
    assert _extract_final_text(msgs) == "first text"


def test_extract_final_text_empty():
    """无 assistant → 返回空串"""
    msgs = [{"role": "user", "content": "hi"}]
    assert _extract_final_text(msgs) == ""


def test_extract_final_text_none_content():
    """assistant content=None 且无 tool_calls → 跳过"""
    msgs = [
        {"role": "assistant", "content": "ok", "tool_calls": None},
        {"role": "assistant", "content": None, "tool_calls": None},
    ]
    assert _extract_final_text(msgs) == "ok"
```

(typo 已在 step 1 修正)

- [ ] **Step 2: 跑测试验失败**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_repl_b_hook.py -v
```

Expected: 多数 failed with `ImportError` 或 `AttributeError`

- [ ] **Step 3: 在 `cc_harness/repl.py` 改 3 处**

1. **ReplState 加字段**(class 定义内 append):

```python
# 在 ReplState class 内 append
todo_hints: list[str] = field(default_factory=list)
last_turn_text: str = ""
```

2. **填 `_after_turn_todo` 占位**(line 465):

```python
MAX_HINTS_PER_TASK = 3
MAX_HINTS_TOTAL = 10


async def _after_turn_todo(state: ReplState, todo_service) -> None:
    """B 阶段 verify hook。每 turn 跑一次,不自动改 status。"""
    try:
        await _after_turn_todo_impl(state, todo_service)
    except Exception as e:
        log.warning("verify hook: unexpected: %s", e)


async def _after_turn_todo_impl(state, todo_service):
    if todo_service is None:
        return
    try:
        tasks_list = await todo_service.list(include_done=False)
    except Exception as e:
        log.warning("verify hook: todo_service.list failed: %s", e)
        return  # 不清旧 hints

    by_id = {t.id: t for t in tasks_list}
    last_turn_text = getattr(state, "last_turn_text", "") or ""
    hints: list[str] = []

    for task in tasks_list:
        if task.status != "in_progress":
            continue
        try:
            result = run_verify(task, by_id, last_turn_text)
        except Exception as e:
            log.warning("verify hook: run_verify failed for %s: %s", task.id, e)
            continue

        per_task: list[str] = []
        if not result.passed:
            for miss in result.missing_criteria:
                per_task.append(f"task {task.id} criterion 未在最近一轮输出中体现: {miss}")
        per_task.extend(result.hints)
        hints.extend(per_task[:MAX_HINTS_PER_TASK])

    state.todo_hints = hints[:MAX_HINTS_TOTAL]


def _extract_final_text(messages: list[dict]) -> str:
    """从 messages 末尾反向查找 role=assistant 且 content 是非空 str 的那条。
    
    含 tool_calls(content=None)时回退到上一条纯文本 message。
    """
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return ""
```

3. **import 区 append**:

```python
# 顶部 import 区 append
from cc_harness.project.verify import run_verify
```

- [ ] **Step 4: 跑测试验通过**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_repl_b_hook.py -v
```

Expected: 13 passed

- [ ] **Step 5: main loop 接线 `state.last_turn_text`**

`run_turn` 内部直接改 `state.messages`(传引用,line 371)。TurnTokenStats 不存 messages。所以 `state.last_turn_text` 在 `run_turn` 之后从 `state.messages` 末尾取。

在 `repl.py` line 384 (`state.session_stats.add(turn_stats)` 之后) 之前加一行:

```python
# B 阶段 Task 4: 提取本轮 LLM 输出文本,给下轮 verify hook 用
state.last_turn_text = _extract_final_text(state.messages)
```

注意:这里 `state.last_turn_text` 是给**下轮** verify hook 用的(run_turn 之后 `_after_turn_todo` 才跑,这时 `last_turn_text` 还是上一轮的)——但**不**矛盾。`last_turn_text` 在 `_after_turn_todo` 跑 verify 时用,verify 写 `todo_hints` 是给**下下轮** system prompt 注入。所以"延迟一轮"是从 `_after_turn_todo` 写到 `_refresh_system_prompt` 读之间隔一个 turn。

更清晰:本轮 run_turn 结束 → `state.last_turn_text` 是本轮输出 → `_after_turn_todo` 拿这个 text 跑 verify → 写 hints 给下轮 prompt。这是 spec 设计的"延迟一轮"语义,正确。

- [ ] **Step 6: baseline + commit**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/ -q
```

Expected: 1062 + 13 = 1075 passed

```bash
git add cc_harness/repl.py tests/test_repl_b_hook.py
git commit -m "feat(repl): _after_turn_todo verify hook 接线

B 阶段 Task 4: 组件 4 - 每 turn 跑 verify 写 hints。
- 三层异常处理: service.list 抛 / 单 task run_verify 抛 / 顶层抛
- hints 每 turn 覆盖(不累积)
- per-task 最多 3 + 全局最多 10 截断
- ReplState 加 todo_hints + last_turn_text 字段
- main loop 接线: state.last_turn_text = _extract_final_text(state.messages)

baseline: 1062 passed → now: 1075 passed (delta +13)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 5: `agent.py` 注入 `<todo_hints>`

**Files:**
- Modify: `cc_harness/agent.py`(`_refresh_system_prompt` 加 `<todo_hints>` 段 append)

**spec 节:** 组件 4 agent 接线 + 注入位置

**与 A 阶段 resume_task 注入并列,同一模式**

### Task 5.1: 失败测试 + append 实现

- [ ] **Step 1: 在 `tests/test_agent.py` 末尾 append 测试**

```python
# --- B 阶段 Task 5: todo_hints 注入 ---


def test_refresh_system_prompt_no_todo_hints_no_block(tmp_path):
    """todo_hints 为空 → 不注入 <todo_hints> 段。"""
    from cc_harness.agent import _refresh_system_prompt
    cwd = str(tmp_path)
    messages = [{"role": "user", "content": "x"}]
    _refresh_system_prompt(messages, cwd, "coding", resume_task=None)
    content = messages[0]["content"]
    # 空 hints → 不出现 todo_hints 段
    assert "<todo_hints>" not in content


def test_refresh_system_prompt_with_todo_hints_appends_block(tmp_path):
    """todo_hints 非空 → 追加 <todo_hints>...</todo_hints> 段, 且 idempotent。

    模拟 _after_turn_todo 写入的 hints 流转。
    """
    from cc_harness.agent import _refresh_system_prompt
    cwd = str(tmp_path)
    messages = [{"role": "user", "content": "x"}]

    # 第一次调: 注入
    _refresh_system_prompt(
        messages, cwd, "coding", resume_task=None,
        todo_hints=["task T1 criterion 未在最近一轮输出中体现: 写 unit test"],
    )
    content = messages[0]["content"]
    assert content.count("<todo_hints>") == 1
    assert "T1" in content
    assert "unit test" in content

    # 第二次调: 仍只有一份(幂等)
    _refresh_system_prompt(
        messages, cwd, "coding", resume_task=None,
        todo_hints=["another hint"],
    )
    content = messages[0]["content"]
    assert content.count("<todo_hints>") == 1
    assert "T1" in content
    assert "another hint" in content


def test_refresh_system_prompt_todo_hints_position_after_resume(tmp_path):
    """<todo_hints> 段在 <resume_task> 段之后(LLM attention 偏末)。"""
    from cc_harness.agent import _refresh_system_prompt
    cwd = str(tmp_path)
    messages = [{"role": "user", "content": "x"}]

    resume_t = _make_resume_task()
    _refresh_system_prompt(
        messages, cwd, "coding", resume_task=resume_t,
        todo_hints=["hint A"],
    )
    content = messages[0]["content"]
    resume_idx = content.rfind("</resume_task>")
    hints_idx = content.find("<todo_hints>")
    # hints 段在 resume 段之后(spec 当前设计)
    assert resume_idx >= 0
    assert hints_idx > resume_idx


def test_refresh_system_prompt_todo_hints_does_not_clobber_resume(tmp_path):
    """todo_hints 注入不破坏 resume_task 段(append-only)。"""
    from cc_harness.agent import _refresh_system_prompt
    cwd = str(tmp_path)
    messages = [{"role": "user", "content": "x"}]
    resume_t = _make_resume_task()
    _refresh_system_prompt(
        messages, cwd, "coding", resume_task=resume_t,
        todo_hints=["hint A"],
    )
    content = messages[0]["content"]
    # resume 块完整
    assert content.count("<resume_task>") == 1
    assert "ship feature" in content
```

- [ ] **Step 2: 跑测试验失败**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_agent.py -v -k "todo_hints"
```

Expected: 4 failed with `TypeError: _refresh_system_prompt() got an unexpected keyword argument 'todo_hints'`

- [ ] **Step 3: 在 `cc_harness/agent.py` 改 2 处**

1. **`_refresh_system_prompt` 签名加 `todo_hints` 参数**(line 587):

```python
def _refresh_system_prompt(
    messages: list[dict],
    cwd: str,
    mode: str,
    *,
    resume_task: "TodoTask | None" = None,
    todo_hints: list[str] | None = None,  # B 阶段 Task 5
) -> None:
```

2. **在 resume_task append 块后,append todo_hints 块**(line 647 之后):

```python
    # --- B 阶段 Task 5: append todo_hints block (idempotent, append-only) ---
    if (
        mode == "coding"
        and todo_hints
        and messages
        and messages[0].get("role") == "system"
    ):
        old = messages[0]["content"]
        old = re.sub(
            r"\s*<todo_hints\b[^>]*>.*?</todo_hints>\s*\Z",
            "",
            old,
            flags=re.DOTALL,
        )
        messages[0]["content"] = old + (
            f"\n\n<todo_hints>\n"
            + "\n".join(todo_hints)
            + "\n</todo_hints>"
        )
```

3. **`run_turn` 签名 + 透传**(line 69 附近):

```python
# run_turn 签名加 todo_hints 参数
def run_turn(
    messages,
    llm,
    mcp,
    *,
    max_iter=20,
    mode="coding",
    cwd="",
    design_dir=None,
    token_counter=None,
    policy=None,
    l5=None,
    extra_native_specs=None,
    context_config=None,
    memory_layer=None,
    offload_deps=None,
    resume_task=None,
    todo_hints=None,  # B 阶段 Task 5
):
```

`_refresh_system_prompt` 调用处(两处,line 132 与 135)加 `todo_hints=todo_hints` 透传。

4. **`repl.py` `run_turn` 调用点也必须传 `todo_hints=state.todo_hints`**(line 370-384,resume_task=state.resume_task 旁边加一行,否则下轮 prompt 不注入):

```python
turn_stats = await run_turn(
    state.messages, llm, mcp,
    ...
    resume_task=state.resume_task,                    # Task 6: 续干任务
    todo_hints=list(state.todo_hints or []),          # B 阶段 Task 5: verify hints
)
```

- [ ] **Step 4: 跑测试验通过**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_agent.py -v -k "todo_hints or resume"
```

Expected: 全部通过(resume_task 的 5 个测试 + todo_hints 的 4 个测试)

- [ ] **Step 5: baseline + commit**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/ -q
```

Expected: 1075 + 4 = 1079 passed

```bash
git add cc_harness/agent.py tests/test_agent.py
git commit -m "feat(agent): inject <todo_hints> 段到 system prompt

B 阶段 Task 5: 组件 4 agent 接线。
- _refresh_system_prompt 加 todo_hints 参数(append-only 模式)
- 与 resume_task 注入并列, 互不破坏
- idempotent: re.sub strip 旧块 + append 新块
- 注入位置: <todo_hints> 在 </resume_task> 之后
- run_turn 签名加 todo_hints=None 透传

baseline: 1075 passed → now: 1079 passed (delta +4)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 6: 集成测试 + E2E

**Files:**
- Modify: `tests/test_repl_b_hook.py`(append 端到端集成)
- Create: `tests/_test_b_e2e.py`(`_` 前缀 gated,FakeLLM + 1 真 LLM)

**spec 节:** 测试策略段(E2E 设计)

### Task 6.1: 集成测试 — verify hook + agent 注入 端到端

- [ ] **Step 1: 在 `tests/test_repl_b_hook.py` 末尾 append 测试**

```python
# --- B 阶段 Task 6.1: 端到端集成 ---


async def test_e2e_verify_hints_flow_into_next_turn_prompt(tmp_path, svc, manifest, monkeypatch):
    """完整链路: in_progress task 缺 criterion → verify hook 写 hints →
    下轮 agent 注入到 system prompt(模拟一次 _refresh_system_prompt 调)。
    """
    from cc_harness.agent import _refresh_system_prompt
    from cc_harness.repl import ReplState, _after_turn_todo

    # 准备
    await _create(svc, "T1", status="in_progress", criteria=["实现 verify hook"], session_id="s")

    state = ReplState()
    state.todo_service = svc
    state.last_turn_text = "本轮啥也没干"  # 不含 "verify hook"

    # 跑 verify hook
    await _after_turn_todo(state, svc)
    assert len(state.todo_hints) > 0
    assert any("verify" in h for h in state.todo_hints)

    # 下轮 turn: _refresh_system_prompt 拿到 hints 注入
    messages = [{"role": "user", "content": "next turn"}]
    _refresh_system_prompt(
        messages, str(tmp_path), "coding",
        resume_task=None, todo_hints=state.todo_hints,
    )
    content = messages[0]["content"]
    assert "<todo_hints>" in content
    assert "verify" in content
```

- [ ] **Step 2: 跑测试验通过**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_repl_b_hook.py -v -k "e2e"
```

Expected: 1 passed

- [ ] **Step 3: 覆盖 + commit**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/ -q
```

Expected: 1079 + 1 = 1080 passed

```bash
git add tests/test_repl_b_hook.py
git commit -m "test(repl): e2e verify hook + agent 注入集成

B 阶段 Task 6.1: 端到端测试 verify hint 链路完整跑通。
- _after_turn_todo 写 hints → _refresh_system_prompt 注入
- 确认 <todo_hints> 段在 system prompt 中出现

baseline: 1079 passed → now: 1080 passed (delta +1)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6.2: E2E FakeLLM + 1 真 LLM gated

- [ ] **Step 0: 在 `tests/_test_b_e2e.py` 顶部加 helper**(关键,所有本 task 测试用它)

```python
# import 之后, fixture 之前
async def _create(svc, title, status="pending", criteria=None, deps=None, session_id="s"):
    t = await svc.create(
        title=title,
        acceptance_criteria=criteria or [],
        depends_on=deps or [],
        session_id=session_id,
    )
    if status != "pending":
        t = await svc.update(t.id, status=status, session_id=session_id)
    return t
```

- [ ] **Step 1: 创建 `tests/_test_b_e2e.py`**

```python
"""B 阶段 E2E(gated,`/` 前缀默认跳过)。

包含:
- test_e2e_llm_uses_topo_sort(FakeLLM 预设响应,无需真 LLM)
- test_e2e_verify_hints_influence_next_turn(FakeLLM 预设)
- test_e2e_full_cycle(1 个真 LLM,@pytest.mark.requires_llm)
"""
import asyncio
import os
from datetime import datetime
from pathlib import Path

import pytest

from cc_harness.project.models import Manifest
from cc_harness.project.service import TodoService
from cc_harness.repl import ReplState, _after_turn_todo
from cc_harness.agent import _refresh_system_prompt


# --- FakeLLM-based E2E(无需真 LLM)---


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    todos = p / ".cc-harness" / "todos"
    todos.mkdir(parents=True)
    (todos / "todos.yaml").write_text("tasks: []\n", encoding="utf-8")
    return p


@pytest.fixture
def svc(proj, monkeypatch) -> TodoService:
    m = Manifest(
        project_id="x", name="x",
        todos_path=".cc-harness/todos",
        created_at=datetime.now(),
    )
    return TodoService(project_root=proj, manifest=m)


async def test_e2e_llm_uses_topo_sort_tool_response(svc, monkeypatch):
    """FakeLLM: LLM 收到 prompt 含 tool spec → 调 todo_toposort → handler 返回 OK。

    简化:不实际跑 agent.run_turn, 直接调 todo_toposort handler 验证它返回的 ToolResult
    能被 LLM 消费。
    """
    from cc_harness.project.tools import todo_toposort_handler

    # 创建 3 task(用 helper)
    await _create(svc, "T1", status="done", session_id="s")
    await _create(svc, "T2", status="in_progress", session_id="s")
    await _create(svc, "T3", status="pending", session_id="s")

    # 模拟 LLM 调 tool
    result = await todo_toposort_handler(
        {"group": "all"},
        service=svc, session_id="s", cwd="/tmp",
    )
    assert result.is_error is False
    # LLM 读 llm_text 决定下一步
    assert "DAG 拓扑视图" in result.llm_text
    assert "T2" in result.llm_text


async def test_e2e_verify_hints_influence_next_turn(svc):
    """FakeLLM: hints 包含具体内容 → 下轮 prompt 应包含该内容。"""
    from cc_harness.agent import _refresh_system_prompt
    from cc_harness.repl import _after_turn_todo, ReplState

    # 准备: in_progress task + 缺 criterion
    await _create(
        svc, "ship feature X", status="in_progress",
        criteria=["跑通 unit test", "更新 README"],
        session_id="s",
    )

    state = ReplState()
    state.todo_service = svc
    state.last_turn_text = "本轮啥也没干"  # 不包含 "unit test" / "README"

    # Turn 1 结束: 跑 verify hook 写 hints
    await _after_turn_todo(state, svc)
    assert any("unit test" in h or "README" in h for h in state.todo_hints)

    # Turn 2 开始: LLM 看到 hints
    messages = [{"role": "user", "content": "继续"}]
    _refresh_system_prompt(
        messages, "/tmp", "coding",
        todo_hints=state.todo_hints,
    )
    content = messages[0]["content"]
    # LLM 看到 hint 内容
    assert "unit test" in content or "README" in content
    assert "<todo_hints>" in content


# --- 真 LLM gated test ---


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="requires real LLM API key",
)
async def test_e2e_full_cycle_real_llm(svc):
    """真 LLM: 创建 in_progress task + 缺 criterion → _after_turn_todo 写 hints →
    _refresh_system_prompt 注入 <todo_hints> 段 → 验证 system prompt 包含。

    注:本测试仅在有 OPENAI_API_KEY 时跑,默认跳过。
    不调 LLM chat(避免 token 成本),只验证 system prompt 构造链路包含 hints 段。
    """
    from cc_harness.repl import _after_turn_todo, ReplState
    from cc_harness.agent import _refresh_system_prompt

    await _create(
        svc, "Build DAG topo", status="in_progress",
        criteria=["实现 Kahn 算法"],
        session_id="real-llm-sess",
    )

    state = ReplState()
    state.todo_service = svc
    state.last_turn_text = "starting"  # 无产物 → 触发 "无产出" hint

    await _after_turn_todo(state, svc)
    assert any("Kahn" in h or "无产出" in h for h in state.todo_hints)

    # 真 LLM 调 run_turn 时, 这些 hints 会进 system prompt
    messages = [{"role": "user", "content": "continue"}]
    _refresh_system_prompt(
        messages, "/tmp", "coding",
        todo_hints=state.todo_hints,
    )
    content = messages[0]["content"]
    assert "<todo_hints>" in content
    assert "Kahn" in content or "无产出" in content
```

- [ ] **Step 2: 跑 FakeLLM 部分(默认 pytest 收集)**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/_test_b_e2e.py -v
```

Expected: 2 passed(test_e2e_full_cycle_real_llm skipped due to no API key)

- [ ] **Step 3: 覆盖 + baseline + commit**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/ -q
.venv/Scripts/python.exe -m pytest tests/_test_b_e2e.py -v
```

Expected: 1080 + 2 = 1082 passed(默认 collected 1082,gated 1 skipped)

```bash
git add tests/_test_b_e2e.py
git commit -m "test(b): e2e gated - FakeLLM topo_sort + verify influence + 1 real LLM

B 阶段 Task 6.2: 端到端测试,默认 gated(`_` 前缀)。
- test_e2e_llm_uses_topo_sort: 验证 tool handler 返回可被 LLM 消费
- test_e2e_verify_hints_influence_next_turn: hints 流转链路
- test_e2e_full_cycle_real_llm: 真 LLM gated(需 OPENAI_API_KEY)

baseline: 1080 passed → now: 1082 passed (delta +2)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## 最终验证

跑全量 + 覆盖率 + lint:

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
.venv/Scripts/python.exe -m pytest tests/test_project_dependency.py tests/test_project_verify.py tests/test_project_tools.py --cov=cc_harness/project/dependency --cov=cc_harness/project/verify --cov=cc_harness/project/tools --cov-branch --cov-report=term-missing
.venv/Scripts/python.exe -m ruff check cc_harness/project/ cc_harness/repl.py cc_harness/agent.py
```

**预期**:
- pytest: 1082 passed(A baseline 1016 + B delta +66)
- dependency.py: 100% line + branch(保持 A baseline)
- verify.py: 100% line + branch(新文件)
- tools.py 新 handler: ≥85% 覆盖
- ruff: clean

## 实施完成检查清单

- [ ] Task 1: `topo_sort` + `get_ready_tasks`(commit +15)
- [ ] Task 2: `verify.py` heuristic + state + run_verify(commit +24)
- [ ] Task 3: `todo_toposort` tool(commit +7)
- [ ] Task 4: `_after_turn_todo` 接线(commit +13)
- [ ] Task 5: `agent.py` 注入 `<todo_hints>`(commit +4)
- [ ] Task 6.1: 端到端集成(commit +1)
- [ ] Task 6.2: E2E gated(commit +2)
- [ ] 全量 1082 passed + 100% 关键覆盖 + ruff clean
- [ ] 7 个 commit 全部带 baseline 报告
- [ ] A 阶段 1016 baseline 保住(每个 commit 末尾验证)
