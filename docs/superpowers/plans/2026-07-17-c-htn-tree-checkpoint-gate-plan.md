# Sub-project C:HTN 树(聚合语义)+ Checkpoint 软完成门 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) 或 superpowers:executing-plans 逐 task 实施。步骤用 checkbox(`- [ ]`)跟踪。

**Goal:** 给 cc-harness 的长程任务能力加 HTN 树聚合(parent done 要求直接 children 全 done)+ `todo_update(status="done")` 完成门(acceptance + 聚合校验,tool 层软拦,force 绕 acceptance 不绕聚合)。

**Architecture:** 纯增量,不改 A/B 的 service 层纯状态机。完成门全在 `todo_update_handler` tool 层(`_completion_gate` 辅助),acceptance 复用 B 的 `run_verify`,聚合用新纯函数 `children_all_done`(dependency.py)。deps 注入 `last_turn_text`(dispatch 统一 splat → 8 handler 全加形参兼容)。`todo_toposort` 加 `view=tree` HTN 缩进树渲染。

**Tech Stack:** Python 3.13 / asyncio / pytest / ruff。纯 stdlib,无新依赖。

**Spec source of truth:** `docs/superpowers/specs/2026-07-17-c-htn-tree-checkpoint-gate-design.md`(读它再动手)。

**baseline:** `pytest --collect-only` = **1116 tests**。每个 commit 末尾必须报 `baseline: 1116 passed → now: X passed (delta +N)`。

---

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `cc_harness/project/dependency.py` | 新增函数 | `children_all_done(tasks, parent_id)` 纯函数 |
| `cc_harness/project/tools.py` | 改 | ① 8 handler 签名加 `last_turn_text` ② `todo_update_handler` 完成门 + `_completion_gate` ③ `TODO_UPDATE_SPEC` 加 `force` ④ `todo_toposort` view=tree ⑤ `TODO_TOPOSORT_SPEC` 加 `view` ⑥ `_render_toposort` 加 tree 分支 |
| `cc_harness/project/extras.py` | 改 | `inject_todo_tools` deps 加 `last_turn_text` + 形参 |
| `cc_harness/repl.py` | 改 | `inject_todo_tools` 调用点(line ~279)传 `last_turn_text=state.last_turn_text` |
| `cc_harness/agent.py` | 改 | `_refresh_system_prompt` coding 段追加 `<todo_resolve_gate>` 静态提示 |
| `tests/test_dependency_c.py` | 新建 | `children_all_done` 单元测试 |
| `tests/test_completion_gate.py` | 新建 | `todo_update` 完成门单元测试 |
| `tests/test_toposort_tree.py` | 新建 | tree 视图渲染测试 |
| `tests/test_c_integration.py` | 新建 | FakeLLM 集成测试 |
| `tests/_test_c_e2e.py` | 新建 | `_` 前缀 gated 真 LLM E2E |

---

## 测试 API 约定(必读)

沿袭 B plan round-2 修复的 `_create` helper —— `TodoService.create()` 是 keyword-only 且无 `status` 字段,status 必须通过 `update` 设。**每个用到 TodoService 的测试文件顶部粘贴这个 helper**:

```python
async def _create(svc, title, status="pending", criteria=None, deps=None,
                  parent=None, session_id="s"):
    """create + (可选)update status。避免 keyword-only create 的 status 陷阱。"""
    t = await svc.create(
        title=title,
        acceptance_criteria=criteria or [],
        depends_on=deps or [],
        parent_task=parent,
        session_id=session_id,
    )
    if status != "pending":
        t = await svc.update(t.id, status=status, session_id=session_id)
    return t
```

`TodoTask` 构造(单元测试造 mock,不经过 service):见 `tests/test_verify.py` / `test_dependency_b.py` 现有用法,用 `dataclasses.replace` 或直接构造。**注意 status_guard**:`pending→done` 是合法转换,但 `pending→in_progress→done` 是典型路径,测试要尊重状态机(见 `service.py:status_guard`)。

---

## Task 1:`children_all_done`(dependency.py 纯函数)

**Files:**
- Modify: `cc_harness/project/dependency.py`(在 `get_ready_tasks` 之后新增)
- Test: `tests/test_dependency_c.py`(新建)

**spec 引用:** 组件 1(line ~95)+ decision 1/7。

- [ ] **Step 1: 写失败测试** `tests/test_dependency_c.py`

```python
"""Sub-project C: children_all_done 纯函数测试。"""
from cc_harness.project.dependency import children_all_done
from cc_harness.project.models import TodoTask
from datetime import datetime, timezone

def _task(tid, status="pending", parent=None):
    now = datetime.now(timezone.utc)
    return TodoTask(id=tid, title=tid, status=status, description="",
                    depends_on=[], parent_task=parent, assigned_to=None,
                    priority=None, labels=[], due_date=None,
                    effort_estimate=None, acceptance_criteria=[],
                    created_at=now, updated_at=now, active_sessions=[])


def test_children_all_done_no_children():
    tasks = {"P": _task("P")}
    assert children_all_done(tasks, "P") == (True, [])


def test_children_all_done_all_done():
    tasks = {"P": _task("P"), "C1": _task("C1", "done", "P"),
             "C2": _task("C2", "done", "P")}
    assert children_all_done(tasks, "P") == (True, [])


def test_children_all_done_partial():
    tasks = {"P": _task("P"), "C1": _task("C1", "done", "P"),
             "C2": _task("C2", "pending", "P"), "C3": _task("C3", "in_progress", "P")}
    done, pending = children_all_done(tasks, "P")
    assert done is False
    assert pending == ["C2", "C3"]  # 字典序


def test_children_all_done_missing_ref_tolerated():
    # C2 的 parent=P 但 C2 不在 dict(理论上 create/update 已防)→ 容错
    tasks = {"P": _task("P"), "C1": _task("C1", "done", "P")}
    assert children_all_done(tasks, "P") == (True, [])


def test_children_all_done_deterministic_order():
    tasks = {}
    for tid in ["Z", "A", "M"]:
        tasks[tid] = _task(tid, "pending", "P")
    tasks["P"] = _task("P")
    _, pending = children_all_done(tasks, "P")
    assert pending == ["A", "M", "Z"]


def test_children_all_done_parent_missing():
    # parent_id 不在 dict → 视为无 children(交给上层 TaskNotFound)
    assert children_all_done({}, "ghost") == (True, [])
```

- [ ] **Step 2: 跑测试确认 RED**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_dependency_c.py -v
```
Expected: FAIL(`children_all_done` 未定义 / ImportError)

- [ ] **Step 3: 实现** `cc_harness/project/dependency.py`(在 `get_ready_tasks` 后追加)

```python
def children_all_done(
    tasks: dict[str, TodoTask], parent_id: str
) -> tuple[bool, list[str]]:
    """parent 的所有直接 children 是否全 done。

    Returns:
        (all_done, pending_child_ids)
        - 无 children / parent 不在 dict → (True, [])
        - children 引用缺失(不在 dict)→ 容错跳过,不阻塞
        - pending_child_ids 按 task.id 字典序(确定性)
    只看直接 children(一层);孙的聚合由孙自己的完成动作把关。
    """
    if parent_id not in tasks:
        return (True, [])
    pending = sorted(
        t.id for t in tasks.values()
        if t.parent_task == parent_id and t.status != "done"
    )
    return (len(pending) == 0, pending)
```

- [ ] **Step 4: 跑测试确认 GREEN + coverage**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_dependency_c.py -v \
  --cov=cc_harness.project.dependency --cov-branch --cov-report=term-missing
```
Expected: 6 passed,children_all_done 100%。

- [ ] **Step 5: 回归 + lint**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_dependency_b.py tests/test_dependency_c.py tests/test_project_dependency.py -q
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m ruff check cc_harness/project/dependency.py tests/test_dependency_c.py
```

- [ ] **Step 6: Commit**

```
feat(dependency): children_all_done 聚合校验纯函数

- 新增 children_all_done(tasks, parent_id) -> (all_done, pending_ids)
- 只看直接 children(一层);缺失引用容错;字典序确定性
- 为 C 阶段 todo_update 完成门的聚合校验铺路

baseline: 1116 passed → now: X passed (delta +6)
```

---

## Task 2:8 handler 签名兼容 + deps 注入 last_turn_text

**Files:**
- Modify: `cc_harness/project/tools.py`(8 个 handler 加 `last_turn_text` 形参)
- Modify: `cc_harness/project/extras.py`(`inject_todo_tools` deps 加 `last_turn_text`)
- Modify: `cc_harness/repl.py`(line ~279 调用点传参)
- Test: `tests/test_dependency_c.py` 追加接线测试 / 或新建 `tests/test_deps_wiring.py`

**spec 引用:** 组件 4 + 开放问题 #1(decision:全 8 handler 加 `last_turn_text: str = ""`)。

**关键背景:** `agent.py:247` 是 `h_kwargs = {"cwd": ..., **deps}`,deps 统一 splat 给所有 handler。deps 加 `last_turn_text` 后,**8 个 handler 签名都要能收**,否则 TypeError。

- [ ] **Step 1: 写失败测试**(验证 dispatch 能传 last_turn_text 不崩)

```python
# tests/test_deps_wiring.py
"""C Task 2: deps 注入 last_turn_text + 8 handler 签名兼容。"""
import pytest
from cc_harness.project.extras import inject_todo_tools
from cc_harness.project.service import TodoService
from cc_harness.project.storage import TodoStorage


@pytest.mark.asyncio
async def test_inject_todo_tools_passes_last_turn_text(tmp_path):
    svc = TodoService(TodoStorage(tmp_path / "t.yaml"))
    extras = inject_todo_tools(svc, "s", cwd=".", last_turn_text="hello")
    # 8 个 entry,每个 deps 含 last_turn_text
    assert len(extras) == 8
    for e in extras:
        assert e["deps"]["last_turn_text"] == "hello"


@pytest.mark.asyncio
async def test_each_handler_accepts_last_turn_text_kwarg(tmp_path):
    """8 handler 都能收 last_turn_text kwarg 不 TypeError(dispatch splat 模拟)。"""
    from cc_harness.project import tools
    svc = TodoService(TodoStorage(tmp_path / "t.yaml"))
    # 先建一个 task 让 handler 有东西可操作
    t = await svc.create(title="x", session_id="s")
    handlers = [
        (tools.todo_list_handler, {}),
        (tools.todo_get_handler, {"task_id": t.id}),
        (tools.todo_toposort_handler, {}),
    ]
    for h, args in handlers:
        # 模拟 dispatch: handler(args, cwd=".", service=svc, session_id="s", last_turn_text="x")
        r = await h(args, cwd=".", service=svc, session_id="s", last_turn_text="x")
        assert r is not None  # 不 TypeError 即过
```

- [ ] **Step 2: 跑确认 RED**(`inject_todo_tools` 不接受 last_turn_text / handler 签名不收)

- [ ] **Step 3: 改 8 handler 签名** —— 每个 `async def xxx_handler(args, *, service, session_id, cwd)` 加 `, last_turn_text: str = ""`。未用的 handler 在体内 `del last_turn_text`(与现有 `del cwd` 风格一致)。**只有 `todo_update_handler` 在 Task 3 真正用 last_turn_text**,其他 del。

涉及行(`tools.py`):361(list)/ 424(get)/ 460(create)/ 497(update)/ 541(delete)/ 563(resolve)/ 612(validate)/ 667(toposort)。

- [ ] **Step 4: 改 `extras.py:inject_todo_tools`**

```python
def inject_todo_tools(
    service: TodoService, session_id: str, cwd: str = "",
    last_turn_text: str = "",
) -> list[dict]:
    deps: dict = {"service": service, "session_id": session_id,
                  "cwd": cwd, "last_turn_text": last_turn_text}
    return [ ... 8 entry 不变 ... ]
```

- [ ] **Step 5: 改 `repl.py` line ~279 调用点** 传 `last_turn_text=state.last_turn_text`(读上下文确认变量名,可能是 `state.last_turn_text`)。

- [ ] **Step 6: 跑 GREEN + 回归**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_deps_wiring.py tests/test_project_tools.py tests/test_project_extras.py tests/test_project_repl_integration.py -q
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m ruff check cc_harness/project/tools.py cc_harness/project/extras.py cc_harness/repl.py
```
注意:`test_project_repl_integration.py` 可能跑真 REPL 慢,超时可单独跑。

- [ ] **Step 7: Commit**

```
feat(tools): 8 handler 兼容 last_turn_text + deps 注入接线

- dispatch 统一 splat deps(agent.py:247)→ 8 handler 全加 last_turn_text 形参
- inject_todo_tools deps 加 last_turn_text
- repl.py 调用点传 state.last_turn_text
- 为 Task 3 todo_update 完成门 acceptance 校验铺路

baseline: 1122 passed → now: X passed (delta +N)
```

---

## Task 3:`todo_update` 完成门 + force

**Files:**
- Modify: `cc_harness/project/tools.py`(`todo_update_handler` 插完成门 + 新增 `_completion_gate` + `TODO_UPDATE_SPEC` 加 `force`)
- Test: `tests/test_completion_gate.py`(新建)

**spec 引用:** 组件 2 + decision 2 + 错误处理表。**这是 C 的核心,最详细。**

- [ ] **Step 1: 写失败测试** `tests/test_completion_gate.py`(顶部粘 `_create` helper)

```python
"""C Task 3: todo_update 完成门(聚合 + acceptance + force)。"""
import pytest
from cc_harness.project.service import TodoService
from cc_harness.project.storage import TodoStorage

async def _create(svc, title, status="pending", criteria=None, deps=None,
                  parent=None, session_id="s"):
    t = await svc.create(title=title, acceptance_criteria=criteria or [],
                         depends_on=deps or [], parent_task=parent,
                         session_id=session_id)
    if status != "pending":
        t = await svc.update(t.id, status=status, session_id=session_id)
    return t

async def _update_done(svc, task_id, force=False, last_turn_text=""):
    from cc_harness.project.tools import todo_update_handler
    args = {"task_id": task_id, "status": "done"}
    if force:
        args["force"] = True
    return await todo_update_handler(args, service=svc, session_id="s",
                                     cwd=".", last_turn_text=last_turn_text)


@pytest.mark.asyncio
async def test_gate_blocks_when_children_pending(tmp_path):
    svc = TodoService(TodoStorage(tmp_path / "t.yaml"))
    p = await _create(svc, "parent", status="in_progress")
    c = await _create(svc, "child", parent=p.id)  # pending child
    r = await _update_done(svc, p.id)
    assert r.is_error is True
    assert c.id in r.llm_text
    assert "子任务" in r.llm_text


@pytest.mark.asyncio
async def test_gate_blocks_acceptance_not_met(tmp_path):
    svc = TodoService(TodoStorage(tmp_path / "t.yaml"))
    t = await _create(svc, "t", status="in_progress",
                      criteria=["必须包含 unit test"])
    r = await _update_done(svc, t.id, last_turn_text="我改了代码")
    assert r.is_error is True
    assert "acceptance" in r.llm_text or "criterion" in r.llm_text
    # task 仍 in_progress(没真 update)
    assert (await svc.get(t.id)).status == "in_progress"


@pytest.mark.asyncio
async def test_gate_both_errors_reported(tmp_path):
    svc = TodoService(TodoStorage(tmp_path / "t.yaml"))
    p = await _create(svc, "p", status="in_progress", criteria=["要 AC1"])
    await _create(svc, "c", parent=p.id)  # pending child
    r = await _update_done(svc, p.id, last_turn_text="nope")
    assert r.is_error is True
    assert "子任务" in r.llm_text
    assert "acceptance" in r.llm_text or "criterion" in r.llm_text


@pytest.mark.asyncio
async def test_gate_force_bypasses_acceptance(tmp_path):
    svc = TodoService(TodoStorage(tmp_path / "t.yaml"))
    t = await _create(svc, "t", status="in_progress", criteria=["要 AC1"])
    r = await _update_done(svc, t.id, force=True, last_turn_text="nope")
    assert r.is_error is False
    assert (await svc.get(t.id)).status == "done"


@pytest.mark.asyncio
async def test_gate_force_does_not_bypass_aggregation(tmp_path):
    svc = TodoService(TodoStorage(tmp_path / "t.yaml"))
    p = await _create(svc, "p", status="in_progress")
    await _create(svc, "c", parent=p.id)  # pending child
    r = await _update_done(svc, p.id, force=True)
    assert r.is_error is True
    assert "子任务" in r.llm_text


@pytest.mark.asyncio
async def test_gate_empty_criteria_skips_acceptance(tmp_path):
    svc = TodoService(TodoStorage(tmp_path / "t.yaml"))
    t = await _create(svc, "t", status="in_progress")  # 无 criteria
    r = await _update_done(svc, t.id)
    assert r.is_error is False


@pytest.mark.asyncio
async def test_gate_passes_when_all_good(tmp_path):
    svc = TodoService(TodoStorage(tmp_path / "t.yaml"))
    t = await _create(svc, "t", status="in_progress", criteria=["要 AC1"])
    r = await _update_done(svc, t.id, last_turn_text="我写了 AC1 的 unit test")
    assert r.is_error is False
    assert (await svc.get(t.id)).status == "done"


@pytest.mark.asyncio
async def test_gate_not_triggered_for_non_done_update(tmp_path):
    """改 title 等非 status=done 的 update 完全不触发 gate(回归保护)。"""
    from cc_harness.project.tools import todo_update_handler
    svc = TodoService(TodoStorage(tmp_path / "t.yaml"))
    t = await _create(svc, "t", status="in_progress", criteria=["要 AC1"])
    r = await todo_update_handler({"task_id": t.id, "title": "new title"},
                                  service=svc, session_id="s", cwd=".",
                                  last_turn_text="")
    assert r.is_error is False
    assert (await svc.get(t.id)).title == "new title"


@pytest.mark.asyncio
async def test_gate_idempotent_already_done(tmp_path):
    """task 已 done 再设 done 不触发 gate。"""
    svc = TodoService(TodoStorage(tmp_path / "t.yaml"))
    t = await _create(svc, "t", status="in_progress", criteria=["要 AC1"])
    await _update_done(svc, t.id, force=True)  # 先标 done
    r = await _update_done(svc, t.id, last_turn_text="nope")  # 再设 done
    assert r.is_error is False  # 已 done,放行


@pytest.mark.asyncio
async def test_gate_failsoft_on_service_list_error(tmp_path, monkeypatch):
    """service.list 抛 → fail-soft 放行(warn log)。"""
    svc = TodoService(TodoStorage(tmp_path / "t.yaml"))
    t = await _create(svc, "t", status="in_progress", criteria=["AC1"])
    async def boom(*a, **k): raise RuntimeError("boom")
    monkeypatch.setattr(svc, "list", boom)
    r = await _update_done(svc, t.id, last_turn_text="nope")
    assert r.is_error is False  # fail-soft 放行
```

- [ ] **Step 2: 跑确认 RED**

- [ ] **Step 3: 实现 `_completion_gate`** `tools.py`(模块级,放在 `todo_update_handler` 之前)

照搬 spec 组件 2 的 `_completion_gate` pseudocode(line ~155-205),关键点:
- `service.list` 异常 → `log.warning(...)` + `return None`(放行)
- task 已 done → `return None`
- 聚合:`children_all_done(by_id, task_id)` → pending → 收 error(**force 也不跳**)
- acceptance:`task.acceptance_criteria and not force` → `run_verify`;run_verify 异常 → warn + 跳过该检查(result=None)
- errors 空 → `return None`;非空 → `ToolResult(is_error=True, ...)`,hint 文案按 has_acceptance_err / has_children_err 组合

- [ ] **Step 4: 改 `todo_update_handler`**(line 497)插完成门

在 `fields` 提取后、`service.update` 调用前:
```python
force = bool(args.get("force", False))
if fields.get("status") == "done":
    gate = await _completion_gate(service, task_id, force, last_turn_text)
    if gate is not None:
        return gate
```
**注意**:现有字段提取是 inline(line 509-525),不强制抽 helper(见 spec round-2 advisory)。`force` 提取后,**不要**放进 `fields`(force 不是 T11 字段,不能传给 service.update)。

- [ ] **Step 5: 改 `TODO_UPDATE_SPEC`**(line 175)parameters 加:
```python
"force": {"type": "boolean", "default": False,
          "description": "status=done 时绕过 acceptance 校验(子任务聚合不可绕)"},
```
description 补:"status=done 时触发完成门(子任务聚合 + acceptance_criteria 校验)。"

- [ ] **Step 6: 跑 GREEN + coverage**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_completion_gate.py -v \
  --cov=cc_harness.project.tools --cov-branch --cov-report=term-missing
```
Expected: 10 passed,`_completion_gate` + `todo_update_handler` 完成门路径 ≥85%。

- [ ] **Step 7: 回归 + lint**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_project_tools.py tests/test_completion_gate.py tests/test_deps_wiring.py -q
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m ruff check cc_harness/project/tools.py tests/test_completion_gate.py
```

- [ ] **Step 8: Commit**

```
feat(tools): todo_update 完成门 + force 绕过 acceptance

- 新增 _completion_gate:聚合(children_all_done,force 不绕)+ acceptance(run_verify,force 绕)
- todo_update_handler status=done 转换时触发 gate,不过 is_error 不 update
- TODO_UPDATE_SPEC 加 force 参数 + description
- fail-soft:service.list / run_verify 异常放行,不卡死 update

baseline: X passed → now: Y passed (delta +10)
```

---

## Task 4:`todo_toposort` view=tree

**Files:**
- Modify: `cc_harness/project/tools.py`(`TODO_TOPOSORT_SPEC` 加 `view` + `todo_toposort_handler` 透传 + `_render_toposort` 加 tree 分支)
- Test: `tests/test_toposort_tree.py`(新建)

**spec 引用:** 组件 3 + decision 5/6。

- [ ] **Step 1: 写失败测试** `tests/test_toposort_tree.py`

```python
"""C Task 4: todo_toposort view=tree HTN 缩进树渲染。"""
import pytest
from cc_harness.project.tools import _render_toposort, todo_toposort_handler
from cc_harness.project.models import TodoTask
from datetime import datetime, timezone

def _task(tid, status="pending", parent=None):
    now = datetime.now(timezone.utc)
    return TodoTask(id=tid, title=f"title-{tid}", status=status, description="",
                    depends_on=[], parent_task=parent, assigned_to=None,
                    priority=None, labels=[], due_date=None,
                    effort_estimate=None, acceptance_criteria=[],
                    created_at=now, updated_at=now, active_sessions=[])


def test_render_tree_single_level():
    by_id = {"P": _task("P", "in_progress"),
             "C1": _task("C1", "done", "P"), "C2": _task("C2", "pending", "P")}
    out = _render_toposort(["P", "C1", "C2"], list(by_id.values()), by_id,
                           None, group="all", view="tree")
    assert "HTN 树视图" in out
    assert "P" in out and "C1" in out and "C2" in out
    # child 比 parent 缩进更多(具体缩进符由实现定,断言 child 行前导空白 > parent)
    lines = {ln.strip().split()[0]: ln for ln in out.splitlines() if ln.strip()}
    assert len(lines["C1:"] if "C1:" in lines else lines.get("C1","")) > len(lines.get("P","")) \
        or out.count("    ") >= 1  # 至少有缩进


def test_render_tree_nested_grandchildren():
    by_id = {"P": _task("P"), "C": _task("C", parent="P"),
             "G": _task("G", parent="C")}
    out = _render_toposort(["P", "C", "G"], list(by_id.values()), by_id,
                           None, group="all", view="tree")
    assert "P" in out and "C" in out and "G" in out
    # G 比 C 深,C 比 P 深(层级递增)


def test_render_tree_cycle_visited_safeguard():
    # parent 环:P→C→P(理论构造),visited 不崩
    by_id = {"P": _task("P", parent="C"), "C": _task("C", parent="P")}
    out = _render_toposort(["P", "C"], list(by_id.values()), by_id,
                           None, group="all", view="tree")
    assert "cycle" in out.lower() or "⚠" in out  # 标环,不崩


def test_render_tree_mixed_top_level_and_children():
    by_id = {"P": _task("P"), "C": _task("C", parent="P"),
             "T": _task("T")}  # T 是另一顶层
    out = _render_toposort(["P", "C", "T"], list(by_id.values()), by_id,
                           None, group="all", view="tree")
    assert "T" in out


def test_render_flat_default_unchanged():
    """view=flat(默认)回归:跟 B 现状一致,不破。"""
    by_id = {"P": _task("P"), "C": _task("C", parent="P")}
    out_flat = _render_toposort(["P", "C"], list(by_id.values()), by_id,
                                None, group="all", view="flat")
    out_default = _render_toposort(["P", "C"], list(by_id.values()), by_id,
                                   None, group="all")
    assert out_flat == out_default
    assert "Topo order" in out_flat  # flat 的标志段


@pytest.mark.asyncio
async def test_toposort_handler_view_tree(tmp_path):
    from cc_harness.project.service import TodoService
    from cc_harness.project.storage import TodoStorage
    svc = TodoService(TodoStorage(tmp_path / "t.yaml"))
    p = await svc.create(title="parent", session_id="s")
    await svc.create(title="child", parent_task=p.id, session_id="s")
    r = await todo_toposort_handler({"view": "tree"}, service=svc,
                                    session_id="s", cwd=".", last_turn_text="")
    assert r.is_error is False
    assert "HTN 树视图" in r.llm_text
```

- [ ] **Step 2: 跑确认 RED**(`_render_toposort` 不收 `view` kwarg → TypeError)

- [ ] **Step 3: 改 `TODO_TOPOSORT_SPEC`**(line 281)parameters 加 `view`:
```python
"view": {"type": "string", "enum": ["flat", "tree"], "default": "flat",
         "description": "flat=拓扑+分组;tree=HTN 缩进树"},
```

- [ ] **Step 4: 改 `todo_toposort_handler`**(line 667)透传 `view`:
```python
view = args.get("view", "flat")
# ... 调 _render_toposort(..., view=view)
```

- [ ] **Step 5: 改 `_render_toposort`**(line 729)加 `view: str = "flat"` 形参,函数体:
```python
if view == "tree":
    return _render_tree(order, by_id, topo_error, group, filtered)
# ... 现有 flat 逻辑不动 ...
```

- [ ] **Step 6: 实现 `_render_tree`**(新模块级函数,tools.py):
- 顶层 = `parent_task is None` 的 task
- DFS:每个 task → 递归 children(`by_id.values()` 里 `parent_task == current.id`),缩进 +2/层
- **visited set 防环**:进函数前 set,递归前检查 node 是否 visited,已 visited → 输出 `⚠ cycle: {id}` 截断
- 同层 children 按 topo order(order 列表的顺序)排
- 截断 MAX_RENDER_TASKS=50(沿用)
- header `HTN 树视图 (N tasks):`
- 孤儿(parent 不在 by_id)当顶层 + 标注

- [ ] **Step 7: 跑 GREEN + lint**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_toposort_tree.py tests/test_toposort_tool.py -v
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m ruff check cc_harness/project/tools.py tests/test_toposort_tree.py
```
Expected: 6 new + 17(B) existing all pass。

- [ ] **Step 8: Commit**

```
feat(tools): todo_toposort view=tree HTN 缩进树

- TODO_TOPOSORT_SPEC 加 view=flat|tree
- _render_toposort 加 view 形参,tree 分支调 _render_tree
- _render_tree:DFS 缩进,visited 防环兜底,孤儿当顶层,截断 50
- view=flat 默认回归不变

baseline: X passed → now: Y passed (delta +6)
```

---

## Task 5:`agent.py` `<todo_resolve_gate>` 静态提示

**Files:**
- Modify: `cc_harness/agent.py`(`_refresh_system_prompt` coding 段追加静态提示)
- Test: `tests/test_agent_hints.py`(B 已建)追加 / 或新建 `tests/test_agent_gate_prompt.py`

**spec 引用:** 组件 5。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_agent_gate_prompt.py
"""C Task 5: <todo_resolve_gate> 静态提示注入(coding mode)。"""
from cc_harness.agent import _refresh_system_prompt

def test_gate_prompt_injected_in_coding_mode(tmp_path):
    messages = [{"role": "user", "content": "x"}]
    _refresh_system_prompt(messages, str(tmp_path), "coding")
    assert "<todo_resolve_gate>" in messages[0]["content"]
    assert "force=true" in messages[0]["content"] or "force" in messages[0]["content"]

def test_gate_prompt_not_injected_in_plan_mode(tmp_path):
    messages = [{"role": "user", "content": "x"}]
    _refresh_system_prompt(messages, str(tmp_path), "plan")
    assert "<todo_resolve_gate>" not in messages[0]["content"]

def test_gate_prompt_idempotent(tmp_path):
    """重复 refresh 不重复注入。"""
    messages = [{"role": "user", "content": "x"}]
    _refresh_system_prompt(messages, str(tmp_path), "coding")
    once = messages[0]["content"].count("<todo_resolve_gate>")
    _refresh_system_prompt(messages, str(tmp_path), "coding")
    twice = messages[0]["content"].count("<todo_resolve_gate>")
    assert once == twice == 1
```

- [ ] **Step 2: 跑确认 RED**

- [ ] **Step 3: 实现** `agent.py:_refresh_system_prompt` 在 `<todo_hints>` 注入逻辑附近(B 落的),coding mode 追加:
```python
if mode == "coding" and messages and messages[0].get("role") == "system":
    # 先 strip 旧的(幂等,anchored to end)
    old = re.sub(r"\s*<todo_resolve_gate\b[^>]*>.*?</todo_resolve_gate>\s*\Z",
                 "", messages[0]["content"], flags=re.DOTALL)
    messages[0]["content"] = old + (
        "\n\n<todo_resolve_gate>\n"
        "标 task 为 done(todo_update status=done)前,系统会校验:"
        "① 所有直接子任务(parent_task)已 done;② acceptance_criteria 在最近输出中体现。\n"
        "- 子任务聚合校验不可绕过(数据一致性)。\n"
        "- acceptance 校验可用 todo_update(status=done, force=true) 绕过(仅在确认启发式误判时)。\n"
        "</todo_resolve_gate>"
    )
```
**注意位置**:与 `<todo_hints>` 注入并列(都在 coding gate 内)。参考 B 阶段的 `<todo_hints>` 注入写法(`agent.py` 现有,~line 660-688)。

- [ ] **Step 4: 跑 GREEN + 回归**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_agent_gate_prompt.py tests/test_agent_hints.py tests/test_agent.py -q
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m ruff check cc_harness/agent.py tests/test_agent_gate_prompt.py
```

- [ ] **Step 5: Commit**

```
feat(agent): <todo_resolve_gate> 静态提示注入(coding mode)

- _refresh_system_prompt coding 段追加 <todo_resolve_gate>
- 告知 agent:标 done 前校验子任务聚合 + acceptance,force 绕 acceptance
- idempotent:re.sub strip 旧块再 append
- plan/design mode 不注入

baseline: X passed → now: Y passed (delta +3)
```

---

## Task 6:集成测试 + E2E

**Files:**
- Create: `tests/test_c_integration.py`
- Create: `tests/_test_c_e2e.py`(`_` 前缀 gated)

**spec 引用:** 测试策略 · 集成 + E2E 段。

- [ ] **Step 1: 写集成测试** `tests/test_c_integration.py`(FakeLLM,顶部粘 `_create`)

参考 B 的 `tests/test_b_integration.py` 的 FakeLLM/FakeMCP 复用模式(import,不重定义)。核心 case:

```python
@pytest.mark.asyncio
async def test_c_agent_update_done_blocked_then_pass(tmp_path):
    """agent update done 被拦 → 补齐 → 再 update done 成功(完整 turn 流)。"""
    # FakeLLM turn1: update done(criteria 未满足)→ 收 error
    # FakeLLM turn2: update done(last_turn_text 命中 criteria)→ 成功
    # assert turn1 后 task 仍 in_progress,turn2 后 done

@pytest.mark.asyncio
async def test_c_agent_parent_blocked_until_children_done(tmp_path):
    """parent update done 被 children 拦 → 完成 children → parent 成功。"""

@pytest.mark.asyncio
async def test_c_force_bypass_e2e(tmp_path):
    """agent force=true 绕 acceptance update done 成功。"""

@pytest.mark.asyncio
async def test_c_deps_last_turn_text_wired(tmp_path):
    """deps 注入 last_turn_text 到 handler(repl 调用点接线验证)。"""

@pytest.mark.asyncio
async def test_c_toposort_tree_after_decompose(tmp_path):
    """agent 连续 create 挂 parent + todo_toposort view=tree 看到树。"""
```

- [ ] **Step 2: 写 E2E** `tests/_test_c_e2e.py`(gated,参考 B 的 `_test_b_e2e.py`)

```python
@pytest.mark.requires_llm
@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY")
    or os.environ.get("CC_HARNESS_RUN_REAL_LLM") != "1",
    reason="real LLM gated: OPENAI_API_KEY + CC_HARNESS_RUN_REAL_LLM=1",
)
def test_c_e2e_real_llm_parent_children_resolve(tmp_path):
    """真 REPL:创建 parent+children,完成 children,update parent done,聚合校验生效。"""
    # subprocess 跑 main.py --mode coding,喂指令,assert 输出含完成门反馈或成功
```

- [ ] **Step 3: 跑集成测试**(E2E 默认 skip)

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_c_integration.py -v
```

- [ ] **Step 4: 全 B + C 阶段测试一起跑(冒烟)**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest \
  tests/test_dependency_c.py tests/test_completion_gate.py tests/test_toposort_tree.py \
  tests/test_deps_wiring.py tests/test_agent_gate_prompt.py tests/test_c_integration.py \
  tests/test_b_integration.py tests/test_verify.py tests/test_dependency_b.py -q
```

- [ ] **Step 5: lint**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m ruff check tests/test_c_integration.py tests/_test_c_e2e.py
```

- [ ] **Step 6: Commit**

```
test(c): 集成 + E2E 覆盖完成门 + HTN 树

- tests/test_c_integration.py: 5 FakeLLM 集成(update done 拦/放/force/deps/tree)
- tests/_test_c_e2e.py: 1 个 requires_llm gated E2E
- 顶部 _create helper 防 keyword-only create 陷阱(B plan round-2 沿袭)

baseline: X passed → now: Y passed (delta +N)
```

---

## Final verification

- [ ] **全量 collect**:`python -m pytest tests/ --collect-only` → 应为 1116 + C 新增(~25)= ~1141
- [ ] **全量跑(可选,慢)**:`python -m pytest tests/ --ignore=tests/_test_*` 全绿(已知 11 个 retired eval/redteam 失败除外)
- [ ] **lint 全**:`python -m ruff check cc_harness/ tests/`
- [ ] **手动 smoke**:起 REPL `main.py --mode coding`,创建 parent + child,试 update parent done(应被拦)→ 完成 child → update parent done(应成功);`todo_toposort view=tree` 看树
- [ ] **更新 memory**:`b-outer-loop-dag-landed.md` 旁加 C 落地记录

## 依赖图

```
Task 1 (children_all_done) ──┐
                             ├──► Task 3 (完成门 + force) ──► Task 5 (prompt)
Task 2 (签名兼容 + deps) ────┘            │
                                         ├──► Task 6 (集成 + E2E)
Task 4 (view=tree) ──────────────────────┘
```

Task 1/2 可并行起点;Task 4 与 Task 3 独立可并行;Task 5 依赖 3;Task 6 最后。
