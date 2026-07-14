# Plan A: 其一·长程任务 — Sub-project A 任务追踪底座落地

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 `2026-07-14-long-horizon-task-tracking-design.md` spec 的 10 个新组件 + 6 处既有接线,提供 Project 容器 + Todo 任务清单 + 跨 session resume。

**Architecture:** 按 spec 改动清单逐文件落地。新增 `cc_harness/project/` 子包(11 文件,数据/校验/服务/UI 四层职责分离)+ `cc_harness/cli/` 子包(4 文件,init/todo/resume 三组子命令)+ 4 处既有文件接线(`main.py` argparse / `repl.py:run_repl` / `agent.py:run_turn` + `_refresh_system_prompt`)。TDD:每 task 独立可测可 commit。

**Tech Stack:** Python 3.11 / pytest / pydantic / PyYAML / rich(Rich Live + Panel)

**关联 spec:**
- 设计源:`docs/superpowers/specs/2026-07-14-long-horizon-task-tracking-design.md`(857 行,本 plan 直接落地;实现者**必读该 spec**)
- 总设计:`docs/superpowers/specs/2026-07-10-assistant-chat-memory-compaction-eval-design.md`(子系统顶层)

**前置:** Plan 2/3/Q1/Q3/Q4 已落地(memory extras / context compaction / locomo eval),`build_memory_extras` 模式可复用

**后续:** Sub-project B(外层 plan-execute-verify-replan loop + DAG)、C(HTN + checkpoint 自检)在本 plan 完成后另立 plan

> ⚠ 本 plan 大量引用 spec 的数据契约、算法、决策记录。每个 Task 标注 spec 对应节,实现者读 spec 该节拿完整代码/算法。本 plan 只给 TDD 步骤编排 + 关键代码骨架。

---

## File Structure(Plan A 涉及,18 文件)

| 文件 | 责任 | spec 节 |
|---|---|---|
| `cc_harness/project/__init__.py` | 新:包标记,导出关键符号 | — |
| `cc_harness/project/models.py` | 新:TodoTask / ValidationIssue / TodoEvent / Manifest / RuleId Literal | 组件 2 数据类 |
| `cc_harness/project/manifest.py` | 新:Manifest load/save/validate | 组件 1 |
| `cc_harness/project/storage.py` | 新:yaml + md 读写 + 合并策略 + active_sessions prune | 组件 5 |
| `cc_harness/project/status.py` | 新:状态守卫(done 不可逆,其他自由) | 组件 3 |
| `cc_harness/project/dependency.py` | 新:引用完整性 + 全表环 + 子图环 | 组件 4 |
| `cc_harness/project/service.py` | 新:TodoService 7 操作 + subscribe + completion_capture 钩子 | 组件 2 |
| `cc_harness/project/memory_bridge.py` | 新:on_task_completion opt-in | 组件 10 |
| `cc_harness/project/live.py` | 新:TodoLivePanel(Rich Live,方案 B) | 组件 6 |
| `cc_harness/project/tools.py` | 新:7 个 tool specs + handlers + inject_todo_tools | 组件 7 |
| `cc_harness/project/extras.py` | 新:inject_todo_tools(session_id 显式) | 组件 7/9 |
| `cc_harness/cli/__init__.py` | 新:包标记 | — |
| `cc_harness/cli/init.py` | 新:init 交互模式 + init_noninteractive | 组件 8 |
| `cc_harness/cli/todo.py` | 新:9 个 todo 子命令 + _shared.py 输出 helper | 组件 8 |
| `cc_harness/cli/resume.py` | 新:--resume / --resume-id 处理 | 组件 8 |
| `cc_harness/cli/_shared.py` | 新:argparse + 输出 helper(load_manifest_or_exit / cli_session_id / JsonOrText printer) | 组件 8 |
| `cc_harness/main.py` | 改:argparse 接受 init/todo/--resume(CLI 模式 vs REPL 模式分派) | 接线点 |
| `cc_harness/repl.py` | 改:启动检测 + 自动 init + TodoService + Live + resume_task state + after-turn 钩子 | 组件 9 |
| `cc_harness/agent.py` | 改:run_turn 新增 resume_task 参数;_refresh_system_prompt 加 resume_task 渲染 SECTION_POOL | 组件 9 step 4 |

**测试文件**(11 个,fixtures 1 个):

| 文件 | 覆盖 | spec 节 |
|---|---|---|
| `tests/conftest.py` | 改:共享 fixture(tmp_project / tmp_project_with_tasks / fake_console / clean_env / fake_todo_service) | 测试策略 |
| `tests/fixtures/project_minimal/...` | 新:最小有效 manifest + 空 todos | 测试策略 |
| `tests/fixtures/project_with_tasks/...` | 新:6 个任务的样本 | 测试策略 |
| `tests/fixtures/project_invalid/...` | 新:故意缺字段(测错误恢复) | 测试策略 |
| `tests/test_project_models.py` | TodoTask / ValidationIssue / TodoEvent / RuleId / Manifest dataclass | 测试策略 |
| `tests/test_project_manifest.py` | load/save/validate + 未知字段 warn + schema_version fail-closed/warn | 测试策略 |
| `tests/test_project_storage.py` | yaml + md 读写 + 合并 policy + active_sessions prune + 原子写 | 测试策略 |
| `tests/test_project_status.py` | 状态守卫完整规则表(**100% 覆盖**) | 测试策略 |
| `tests/test_project_dependency.py` | 引用完整性 + 全表环 + 子图环(**100% 覆盖**) | 测试策略 |
| `tests/test_project_service.py` | TodoService 7 操作 + subscribe + completion_capture | 测试策略 |
| `tests/test_project_memory_bridge.py` | opt-in 钩子,session_id 一致 | 测试策略 |
| `tests/test_project_live.py` | 只测 _render() 函数(用 Console.record=True) | 测试策略 |
| `tests/test_project_tools.py` | 7 handler(用 stub TodoService) | 测试策略 |
| `tests/test_project_extras.py` | inject_todo_tools 返回签名 + deps 内容 | 测试策略 |
| `tests/test_cli_init.py` | init 交互 + 非交互 + 已存在 + 非 git | 测试策略 |
| `tests/test_cli_todo.py` | 7 个 todo 子命令 + --json + 退出码 | 测试策略 |
| `tests/test_cli_resume.py` | --resume / --resume-id / --no-resume | 测试策略 |
| `tests/test_project_resume.py` | _select_resume_task 规则 | 测试策略 |
| `tests/test_project_repl_integration.py` | REPL + TodoService + Live 集成(无 LLM) | 测试策略 |
| `tests/_test_project_e2e.py` | 跑真实 LLM 的 E2E(前缀下划线,默认跳过) | 测试策略 |

---

## Task 1: 基础模型 + Manifest + Storage(数据层)

**Files:**
- Create: `cc_harness/project/__init__.py`
- Create: `cc_harness/project/models.py`
- Create: `cc_harness/project/manifest.py`
- Create: `cc_harness/project/storage.py`
- Create: `tests/conftest.py`(append,不动既有 fixture)
- Create: `tests/fixtures/project_minimal/.cc-harness/project.yaml`
- Create: `tests/fixtures/project_minimal/.cc-harness/todos/todos.yaml`
- Create: `tests/fixtures/project_with_tasks/.cc-harness/project.yaml`
- Create: `tests/fixtures/project_with_tasks/.cc-harness/todos/todos.yaml`
- Create: `tests/fixtures/project_with_tasks/.cc-harness/todos/<6 个任务 id>.md`(6 个)
- Create: `tests/fixtures/project_invalid/.cc-harness/project.yaml`
- Test: `tests/test_project_models.py`
- Test: `tests/test_project_manifest.py`
- Test: `tests/test_project_storage.py`

### Task 1.1: 数据类 models.py

- [ ] **Step 1: 写失败测试**

```python
# tests/test_project_models.py
from datetime import datetime
from cc_harness.project.models import TodoTask, ValidationIssue, TodoEvent, RuleId

def test_todo_task_required_fields():
    now = datetime.now()
    t = TodoTask(
        id="abc12345",
        title="test",
        status="pending",
        created_at=now,
        updated_at=now,
        description="",
        depends_on=[],
        parent_task=None,
        assigned_to=None,
        priority=None,
        labels=[],
        due_date=None,
        effort_estimate=None,
        acceptance_criteria=[],
        active_sessions=[],
    )
    assert t.id == "abc12345"
    assert t.status == "pending"
```

- [ ] **Step 2: 跑测试验失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_project_models.py::test_todo_task_required_fields -v`
Expected: FAIL (ImportError: No module named 'cc_harness.project.models')

- [ ] **Step 3: 实现 models.py**

按 spec 组件 2 数据类完整定义:`TodoTask` (T15) / `ValidationIssue` / `TodoEvent` / `RuleId` Literal / `Manifest` dataclass。

- [ ] **Step 4: 跑测试验通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_project_models.py -v`
Expected: PASS

- [ ] **Step 5: 补 ValidationIssue / TodoEvent / Manifest 测试**

```python
def test_validation_issue_rule_id_literal():
    issue = ValidationIssue(task_id="x", severity="error",
                            rule_id="missing_dependency", message="oops")
    assert issue.rule_id == "missing_dependency"

def test_todo_event_status_changed():
    e = TodoEvent(kind="status_changed", prev_status="pending")
    assert e.prev_status == "pending"

def test_manifest_minimal():
    m = Manifest(
        project_id="7f3a", name="x", todos_path=".cc-harness/todos",
        created_at=datetime.now(),
    )
    assert m.schema_version == 1
    assert m.resume_mode == "ask"
    assert m.memory.integration.completion_capture is False
```

- [ ] **Step 6: 跑 + commit**

```bash
git add cc_harness/project/__init__.py cc_harness/project/models.py tests/test_project_models.py
git commit -m "feat(project): data models TodoTask/ValidationIssue/TodoEvent/Manifest"
```

### Task 1.2: Manifest load/save/validate

- [ ] **Step 1: 写失败测试**

```python
# tests/test_project_manifest.py
from cc_harness.project.manifest import load_manifest, save_manifest, ManifestError

def test_load_manifest_minimal(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    cc = proj / ".cc-harness"
    cc.mkdir()
    (cc / "project.yaml").write_text("""
project_id: 7f3a
name: test
todos_path: .cc-harness/todos
created_at: 2026-07-14T10:00:00Z
""", encoding="utf-8")
    m = load_manifest(proj)
    assert m.project_id == "7f3a"
    assert m.resume_mode == "ask"

def test_load_manifest_missing_required():
    # tests/fixtures/project_invalid/project.yaml(故意缺 created_at)
    pass  # 实现后跑
```

- [ ] **Step 2-5**:跑测试验失败 → 实现 → 跑通过

实现要点(spec 组件 1 约束):
- PyYAML `safe_load`
- 必填字段缺 → ManifestError
- schema_version 已知但缺省 → 默认 1;未知(> 1) → ManifestError fail-closed;< 1 → warn
- 未知字段 → warn log,不报错
- `save_manifest(proj, manifest)` 写 yaml,UTF-8 + 2-space 缩进

- [ ] **Step 6: 补测试 + commit**

```bash
git add cc_harness/project/manifest.py tests/test_project_manifest.py tests/fixtures/project_minimal tests/fixtures/project_with_tasks tests/fixtures/project_invalid
git commit -m "feat(project): manifest load/save/validate + fixtures"
```

### Task 1.3: Storage yaml + md 读写

- [ ] **Step 1: 写失败测试**

```python
# tests/test_project_storage.py
import pytest
from cc_harness.project.storage import TodoStorage
from cc_harness.project.models import TodoTask, Manifest
from datetime import datetime

@pytest.fixture
def storage(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    cc = proj / ".cc-harness" / "todos"
    cc.mkdir(parents=True)
    (cc / "todos.yaml").write_text("tasks: []\n", encoding="utf-8")
    manifest = Manifest(project_id="x", name="x", todos_path=".cc-harness/todos", created_at=datetime.now())
    return TodoStorage(proj, manifest)

def test_storage_load_empty(storage):
    assert storage.load_all() == []

def test_storage_save_and_load(storage):
    now = datetime.now()
    task = TodoTask(id="abc12345", title="test", status="pending",
                    created_at=now, updated_at=now, description="hello",
                    depends_on=[], parent_task=None, assigned_to=None,
                    priority=None, labels=[], due_date=None,
                    effort_estimate=None, acceptance_criteria=[],
                    active_sessions=["sess-A"])
    storage.save_all([task])
    loaded = storage.load_all()
    assert len(loaded) == 1
    assert loaded[0].id == "abc12345"
    assert loaded[0].description == "hello"
```

- [ ] **Step 2-5**:失败 → 实现 → 通过

实现要点(spec 组件 5):
- `todos.yaml` 格式:`tasks:\n  - id: ...\n    title: ...\n    ...`(单层 list)
- md 文件 frontmatter:```---\nid: ...\ntitle: ...\n...\n---\n\n{description body}```
- 合并 policy:`load_all` 时以 yaml 为主索引,md frontmatter 仅作 description 来源;md 缺失 → warn + description="";md 多余 → warn + 不引入
- 原子写:`todos.yaml.tmp` → `os.replace`
- `active_sessions` prune:长度 > 50 → 截断为最近 50 + `# earlier N truncated at {ts}`

- [ ] **Step 6: 补 md frontmatter 序列化测试 + commit**

```bash
git add cc_harness/project/storage.py tests/test_project_storage.py
git commit -m "feat(project): TodoStorage yaml+md 双文件,合并策略,active_sessions prune"
```

---

## Task 2: 状态守卫 + 依赖校验

**Files:**
- Create: `cc_harness/project/status.py`
- Create: `cc_harness/project/dependency.py`
- Test: `tests/test_project_status.py`(**100% 覆盖**)
- Test: `tests/test_project_dependency.py`(**100% 覆盖**)

### Task 2.1: 状态守卫 status.py

- [ ] **Step 1: 写失败测试**

```python
# tests/test_project_status.py
import pytest
from cc_harness.project.status import status_guard, StatusGuardError
from cc_harness.project.models import TodoTask
from datetime import datetime

def _task(status="pending"):
    now = datetime.now()
    return TodoTask(id="abc12345", title="t", status=status,
                    created_at=now, updated_at=now, description="",
                    depends_on=[], parent_task=None, assigned_to=None,
                    priority=None, labels=[], due_date=None,
                    effort_estimate=None, acceptance_criteria=[],
                    active_sessions=[])

def test_status_guard_pending_to_in_progress():
    status_guard(_task("pending"), "in_progress")  # OK,不抛

def test_status_guard_done_is_terminal():
    with pytest.raises(StatusGuardError, match="done is terminal"):
        status_guard(_task("done"), "pending")

def test_status_guard_pending_to_cancelled():
    status_guard(_task("pending"), "cancelled")

def test_status_guard_in_progress_to_blocked():
    status_guard(_task("in_progress"), "blocked")

def test_status_guard_cancelled_to_pending():
    status_guard(_task("cancelled"), "pending")

def test_status_guard_unknown_status():
    with pytest.raises(StatusGuardError, match="unknown status"):
        status_guard(_task("pending"), "garbage")
```

- [ ] **Step 2-4**:失败 → 实现 → 通过

实现:按 spec 组件 3 规则表实现 `status_guard(current: TodoTask, new_status: str) -> None`,raise `StatusGuardError` on illegal transition。

- [ ] **Step 5: 100% 覆盖验证**

Run: `.venv/Scripts/python.exe -m pytest tests/test_project_status.py --cov=cc_harness/project/status --cov-report=term-missing`
Expected: 100% line + branch coverage

- [ ] **Step 6: commit**

```bash
git add cc_harness/project/status.py tests/test_project_status.py
git commit -m "feat(project): status_guard 状态机 + 100% 覆盖"
```

### Task 2.2: 依赖校验 dependency.py

- [ ] **Step 1: 写失败测试**

```python
# tests/test_project_dependency.py
import pytest
from cc_harness.project.dependency import check_references, check_no_cycle, dep_check, DependencyCycleError
from cc_harness.project.models import TodoTask
from datetime import datetime

def _task(id, status="pending", depends_on=None):
    now = datetime.now()
    return TodoTask(id=id, title=id, status=status,
                    created_at=now, updated_at=now, description="",
                    depends_on=depends_on or [], parent_task=None,
                    assigned_to=None, priority=None, labels=[],
                    due_date=None, effort_estimate=None,
                    acceptance_criteria=[], active_sessions=[])

def test_check_references_missing():
    a = _task("aaa", depends_on=["bbb"])
    issues = check_references(a, {"aaa": a})
    assert any(i.rule_id == "missing_dependency" for i in issues)

def test_check_references_self_parent():
    a = _task("aaa")
    a.parent_task = "aaa"
    issues = check_references(a, {"aaa": a})
    assert any(i.rule_id == "self_parent" for i in issues)

def test_check_no_cycle_detects():
    a = _task("aaa", depends_on=["bbb"])
    b = _task("bbb", depends_on=["aaa"])
    issues = check_no_cycle([a, b])
    assert any(i.rule_id == "cycle" for i in issues)

def test_check_no_cycle_clean():
    a = _task("aaa")
    b = _task("bbb")
    issues = check_no_cycle([a, b])
    assert issues == []

def test_dep_check_blocks_subgraph_cycle():
    a = _task("aaa")
    b = _task("bbb", depends_on=["aaa"])
    with pytest.raises(DependencyCycleError):
        dep_check("aaa", ["bbb"], {"aaa": a, "bbb": b})
```

- [ ] **Step 2-4**:失败 → 实现 → 通过

实现:spec 组件 4 三种校验:
- `check_references(task, all_tasks) -> list[ValidationIssue]` — 引用完整性 + self_parent
- `check_no_cycle(tasks) -> list[ValidationIssue]` — DFS white/gray/black 全表环检测
- `dep_check(task_id, new_depends_on, all_tasks) -> None` — 子图环检测,raise `DependencyCycleError`

- [ ] **Step 5: 100% 覆盖验证 + commit**

```bash
git add cc_harness/project/dependency.py tests/test_project_dependency.py
git commit -m "feat(project): 依赖校验 引用/全表环/子图环 + 100% 覆盖"
```

---

## Task 3: TodoService + Memory Bridge

**Files:**
- Create: `cc_harness/project/service.py`
- Create: `cc_harness/project/memory_bridge.py`
- Test: `tests/test_project_service.py`
- Test: `tests/test_project_memory_bridge.py`

### Task 3.1: TodoService CRUD + subscribe

- [ ] **Step 1: 写失败测试**

```python
# tests/test_project_service.py
import pytest
from cc_harness.project.service import TodoService
from cc_harness.project.models import Manifest, TaskNotFound, StatusGuardError
from datetime import datetime

@pytest.fixture
def svc(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".cc-harness" / "todos").mkdir(parents=True)
    (proj / ".cc-harness" / "todos" / "todos.yaml").write_text("tasks: []\n", encoding="utf-8")
    manifest = Manifest(project_id="x", name="x", todos_path=".cc-harness/todos",
                        created_at=datetime.now())
    return TodoService(project_root=proj, manifest=manifest)

async def test_create_and_get(svc):
    t = await svc.create(title="hello")
    assert t.title == "hello"
    assert t.status == "pending"
    fetched = await svc.get(t.id)
    assert fetched.id == t.id

async def test_create_with_depends_on_validates(svc):
    a = await svc.create(title="a")
    b = await svc.create(title="b", depends_on=[a.id])
    assert a.id in b.depends_on

async def test_update_status_calls_guard(svc):
    t = await svc.create(title="x")
    updated = await svc.update(t.id, status="done")
    assert updated.status == "done"
    with pytest.raises(StatusGuardError):
        await svc.update(t.id, status="pending")

async def test_delete_force_with_dependents_creates_dangling(svc):
    a = await svc.create(title="a")
    b = await svc.create(title="b", depends_on=[a.id])
    await svc.delete(a.id, force=True)
    issues = await svc.validate()
    assert any("missing_dependency" in i.rule_id for i in issues)

async def test_subscribe_fires_on_create(svc):
    events = []
    svc.subscribe(lambda t, e: events.append((t.id, e.kind)))
    t = await svc.create(title="x")
    assert (t.id, "created") in events

async def test_resolve_returns_chain(svc):
    a = await svc.create(title="a")
    b = await svc.create(title="b", depends_on=[a.id])
    chain = await svc.resolve(b.id)
    ids = [t.id for t in chain]
    assert a.id in ids and b.id in ids
```

- [ ] **Step 2-4**:失败 → 实现 → 通过

实现要点(spec 组件 2 + 7):
- `TodoService(project_root, manifest, llm=None, memory_service=None)`
- 7 操作:list/get/create/update/delete/resolve/validate(签名照 spec)
- `subscribe(callback) / unsubscribe(callback)`(callback 收 TodoTask + TodoEvent)
- `update()` 内部:`prev_status != "done" and task.status == "done"` 时 await `_on_completion(task)`(Task 3.2 接通)
- `create()` 自动 gen id(uuid4 hex[:8])+ created_at + updated_at;session_id append 到 active_sessions
- `update(task_id, *, session_id=None, **fields)` 走状态守卫 + 依赖子图校验
- `delete(task_id, force=False)`:force=False 拒绝 done/有 dependents;force=True 强制,产生 dangling refs(不级联删)
- `resolve(task_id)`:BFS 传递依赖链,返 [task, *ancestors]
- `validate()`:跑所有引用完整性 + 全表环检测 + active_sessions prune

- [ ] **Step 5: 补 validate / resolve / list 测试 + commit**

```bash
git add cc_harness/project/service.py tests/test_project_service.py
git commit -m "feat(project): TodoService 7 操作 + subscribe + completion_capture hook 点"
```

### Task 3.2: Memory Bridge

- [ ] **Step 1: 写失败测试**

```python
# tests/test_project_memory_bridge.py
import pytest
from cc_harness.project.memory_bridge import on_task_completion
from cc_harness.project.models import Manifest, TodoTask
from datetime import datetime
from unittest.mock import AsyncMock

def _task():
    now = datetime.now()
    return TodoTask(id="abc12345", title="hello", status="done",
                    created_at=now, updated_at=now, description="",
                    depends_on=[], parent_task=None, assigned_to=None,
                    priority=None, labels=[], due_date=None,
                    effort_estimate=None, acceptance_criteria=[],
                    active_sessions=["sess-A"])

async def test_completion_capture_disabled_returns():
    m = Manifest(project_id="x", name="x", todos_path=".",
                  created_at=datetime.now())  # completion_capture 默认 False
    mem = AsyncMock()
    await on_task_completion(_task(), m, mem)
    mem.save.assert_not_called()

async def test_completion_capture_enabled_calls_save():
    m = Manifest(project_id="x", name="x", todos_path=".",
                  created_at=datetime.now(),
                  memory={"integration": {"completion_capture": True}})
    mem = AsyncMock()
    t = _task()
    await on_task_completion(t, m, mem)
    mem.save.assert_awaited_once()
    call_args = mem.save.await_args
    assert "abc12345" in call_args.kwargs["text"]
    assert call_args.kwargs["source"] == "todo/completion"
    assert call_args.kwargs["session_id"] == "sess-A"
```

- [ ] **Step 2-4**:失败 → 实现 → 通过

- [ ] **Step 5: commit**

```bash
git add cc_harness/project/memory_bridge.py tests/test_project_memory_bridge.py
git commit -m "feat(project): memory_bridge opt-in completion_capture"
```

---

## Task 4: Agent tools(7 个 tool spec + handlers + extras)

**Files:**
- Create: `cc_harness/project/tools.py`
- Create: `cc_harness/project/extras.py`
- Test: `tests/test_project_tools.py`
- Test: `tests/test_project_extras.py`

### Task 4.1: 7 tool specs + handlers

- [ ] **Step 1: 写失败测试**

```python
# tests/test_project_tools.py
import pytest
from cc_harness.project.tools import (
    TODO_LIST_SPEC, TODO_GET_SPEC, TODO_CREATE_SPEC, TODO_UPDATE_SPEC,
    TODO_DELETE_SPEC, TODO_RESOLVE_SPEC, TODO_VALIDATE_SPEC,
    todo_list_handler, todo_get_handler, todo_create_handler,
    todo_update_handler, todo_delete_handler, todo_resolve_handler,
    todo_validate_handler,
)
from cc_harness.project.models import Manifest
from datetime import datetime

@pytest.fixture
def deps(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".cc-harness" / "todos").mkdir(parents=True)
    (proj / ".cc-harness" / "todos" / "todos.yaml").write_text("tasks: []\n", encoding="utf-8")
    from cc_harness.project.service import TodoService
    manifest = Manifest(project_id="x", name="x", todos_path=".cc-harness/todos",
                        created_at=datetime.now())
    svc = TodoService(project_root=proj, manifest=manifest)
    return {"service": svc, "session_id": "test-session"}

def test_all_specs_have_function():
    for s in [TODO_LIST_SPEC, TODO_GET_SPEC, TODO_CREATE_SPEC, TODO_UPDATE_SPEC,
              TODO_DELETE_SPEC, TODO_RESOLVE_SPEC, TODO_VALIDATE_SPEC]:
        assert s["type"] == "function"
        assert "name" in s["function"]
        assert "parameters" in s["function"]

async def test_create_handler_returns_llm_visible_text(deps):
    result = await todo_create_handler({"title": "hello"}, **deps)
    text = result.llm_text  # 或 handler 返回的字符串
    assert "[todo_create]" in text
    assert "hello" in text
    assert "abc" not in text  # id 应该出现

async def test_create_handler_error_message_llm_friendly(deps):
    # create 一个会形成环的 task → handler 应返回 LLM 友好错误
    a = await deps["service"].create(title="a")
    result = await todo_create_handler(
        {"title": "b", "depends_on": [a.id]}, **deps)
    # 第二次 create 让 a 依赖 b 会成环,模拟
    result = await todo_update_handler({"task_id": a.id, "depends_on": [a.id]}, **deps)
    text = result.llm_text if hasattr(result, "llm_text") else str(result)
    assert "cycle" in text.lower() or "DependencyCycleError" in text
```

> **注**:handler 返回格式与现有 `run_command` 兼容(mcp_client.ToolResult),具体签名照 spec 组件 7 文本示例。

- [ ] **Step 2-4**:失败 → 实现 → 通过

实现要点(spec 组件 7):
- 7 个 SPEC dict 完整定义(参数 schema 照 spec)
- 7 个 handler:`async def xxx_handler(args, *, service, session_id, cwd)` 返回 `ToolResult`(看 `cc_harness/tools.py:run_command` 现有签名)
- `inject_todo_tools(service, session_id) -> list[dict]`:返 `[{"spec": ..., "handler": ..., "deps": {"service": service, "session_id": session_id}}, ...]`
- 错误格式:`[todo_xxx] ✗ {ExceptionName}: {detail}`

- [ ] **Step 5: commit**

```bash
git add cc_harness/project/tools.py cc_harness/project/extras.py tests/test_project_tools.py tests/test_project_extras.py
git commit -m "feat(project): 7 agent tools + inject_todo_tools"
```

---

## Task 5: CLI(init + todo 子命令 + resume)

**Files:**
- Create: `cc_harness/cli/__init__.py`
- Create: `cc_harness/cli/_shared.py`
- Create: `cc_harness/cli/init.py`
- Create: `cc_harness/cli/todo.py`
- Create: `cc_harness/cli/resume.py`
- Test: `tests/test_cli_init.py`
- Test: `tests/test_cli_todo.py`
- Test: `tests/test_cli_resume.py`

### Task 5.1: CLI 共享 helper _shared.py

- [ ] **Step 1: 写失败测试**

```python
# tests/test_cli_init.py 起始 + 后续 todo/resume 测试
import pytest
from cc_harness.cli._shared import load_manifest_or_exit, cli_session_id

def test_cli_session_id_format():
    sid = cli_session_id()
    assert sid.startswith("cli-")
    assert len(sid) >= 12
```

- [ ] **Step 2-4**:失败 → 实现 → 通过

实现:`load_manifest_or_exit(cwd) -> Manifest`(无 manifest → 提示 `cc-harness init` 并 sys.exit 1,**CLI 模式不自动 init**),`cli_session_id() -> str` 返 `cli-{ts}-{hex[:8]}`。

- [ ] **Step 5: commit**

```bash
git add cc_harness/cli/__init__.py cc_harness/cli/_shared.py
git commit -m "feat(cli): shared helpers — load_manifest_or_exit + cli_session_id"
```

### Task 5.2: init(交互 + 非交互)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_cli_init.py
import pytest
from pathlib import Path
from cc_harness.cli.init import init_noninteractive

def test_init_noninteractive_creates_files(tmp_path):
    m = init_noninteractive(tmp_path, name="myapp")
    assert (tmp_path / ".cc-harness" / "project.yaml").exists()
    assert (tmp_path / ".cc-harness" / "todos" / "todos.yaml").exists()
    assert m.name == "myapp"
    assert m.project_id  # uuid 生成
```

- [ ] **Step 2-4**:失败 → 实现 → 通过

实现:
- `init_noninteractive(cwd, name) -> Manifest`:写 project.yaml + 空 todos.yaml + 空 todos/.gitkeep;不写 .gitignore(spec:非交互模式 default,git 探测后再决定写)
- `init_interactive(cwd)` 用 `rich.prompt` 询问 name/description/resume-mode;存在则询问 re-init

- [ ] **Step 5: commit**

```bash
git add cc_harness/cli/init.py tests/test_cli_init.py
git commit -m "feat(cli): init 交互 + 非交互 + 已存在时询问"
```

### Task 5.3: todo 子命令(7 个)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_cli_todo.py
import subprocess, sys, os
from pathlib import Path
import pytest

@pytest.fixture
def project_dir(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    # 用 init_noninteractive 初始化
    from cc_harness.cli.init import init_noninteractive
    init_noninteractive(proj, name="t")
    return proj

def test_cli_todo_create_and_list(project_dir):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent)}
    # create
    r = subprocess.run(
        [sys.executable, "-m", "cc_harness.main", "todo", "create", "hello"],
        cwd=project_dir, env=env, capture_output=True, text=True)
    assert r.returncode == 0
    assert "[todo_create]" in r.stdout
    # list
    r = subprocess.run(
        [sys.executable, "-m", "cc_harness.main", "todo", "list"],
        cwd=project_dir, env=env, capture_output=True, text=True)
    assert "[todo_list]" in r.stdout
    assert "hello" in r.stdout
```

- [ ] **Step 2-4**:失败 → 实现 → 通过

实现:`cc_harness/cli/todo.py` 提供 `cmd_todo(args)` 函数,argparse 子命令 `list / get / create / update / delete / resolve / validate`,各自组装 TodoService 调用并打印 spec 定义的输出格式。TTY 检测:`sys.stdout.isatty()` 决定 Rich 表格 vs 纯文本。

- [ ] **Step 5: commit**

```bash
git add cc_harness/cli/todo.py tests/test_cli_todo.py
git commit -m "feat(cli): todo 7 子命令(list/get/create/update/delete/resolve/validate)"
```

### Task 5.4: --resume / --resume-id

- [ ] **Step 1: 写失败测试**

```python
# tests/test_cli_resume.py
def test_cli_resume_no_resume_flag(project_dir):
    # --no-resume 应跳过自动 resume
    pass
```

- [ ] **Step 2-4**:失败 → 实现 → 通过

实现:`cc_harness/cli/resume.py:cmd_resume(args)` 处理 `--resume` / `--resume-id` / `--no-resume`。

- [ ] **Step 5: commit**

```bash
git add cc_harness/cli/resume.py tests/test_cli_resume.py
git commit -m "feat(cli): resume --resume / --resume-id / --no-resume"
```

---

## Task 6: Live 组件 + REPL 集成

**Files:**
- Create: `cc_harness/project/live.py`
- Modify: `cc_harness/repl.py`(6 处接线)
- Modify: `cc_harness/agent.py`(run_turn + _refresh_system_prompt)
- Modify: `cc_harness/main.py`(argparse CLI 分派)
- Test: `tests/test_project_live.py`
- Test: `tests/test_project_repl_integration.py`

### Task 6.1: TodoLivePanel._render()(单测先)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_project_live.py
from rich.console import Console
from io import StringIO
from cc_harness.project.live import TodoLivePanel
from cc_harness.project.models import Manifest, TodoTask
from datetime import datetime

def test_render_empty():
    console = Console(file=StringIO(), force_terminal=True, width=80)
    panel = TodoLivePanel._render_static(console, tasks=[], project_name="x", project_id="abc")
    text = console.file.getvalue()
    assert "x" in text
    assert "abc" in text
    assert "no tasks" in text.lower() or "0 tasks" in text
```

- [ ] **Step 2-4**:失败 → 实现 → 通过

实现:`TodoLivePanel._render_static(console, tasks, project_name, project_id) -> Panel`(纯函数,可单测);`start()` / `stop()` / `_on_change(task, event)` 围绕 Rich `Live` context manager(方案 B)。

- [ ] **Step 5: 补多任务 + 折叠 + 截断测试 + commit**

```bash
git add cc_harness/project/live.py tests/test_project_live.py
git commit -m "feat(project): TodoLivePanel _render + Live context(方案 B)"
```

### Task 6.2: agent.py — run_turn + _refresh_system_prompt

- [ ] **Step 1: 改 run_turn 签名**

修改 `cc_harness/agent.py`:
- `run_turn(..., resume_task: TodoTask | None = None)` 新增参数
- `_refresh_system_prompt(messages, cwd, mode, resume_task=None)` 加参数
- `_refresh_system_prompt` 末尾:若 `mode == "coding" and resume_task is not None`,追加 SECTION_POOL 一段(`## Resume Task (跨 session 续干)\n...` 照 spec)

- [ ] **Step 2: 测试验证不破坏现有**

Run: `.venv/Scripts/python.exe -m pytest tests/test_agent.py -v`
Expected: PASS(没改默认行为,新参数默认 None)

- [ ] **Step 3: commit**

```bash
git add cc_harness/agent.py
git commit -m "feat(agent): run_turn + _refresh_system_prompt 支持 resume_task"
```

### Task 6.3: repl.py — 6 处接线

- [ ] **Step 1: 改 ReplState**

```python
# cc_harness/repl.py
@dataclass
class ReplState:
    mode: str = "coding"
    messages: list[dict] = field(default_factory=list)
    session_stats: SessionTokenStats = field(default_factory=SessionTokenStats)
    token_counter: TokenCounter = field(default_factory=TokenCounter)
    memory_extras: list = field(default_factory=list)
    context_config: ContextConfig = field(default_factory=ContextConfig)
    session_id: str = ""
    mem_deps: dict | None = None
    # Plan A 新增:
    manifest: object | None = None         # cc_harness.project.models.Manifest
    todo_service: object | None = None     # cc_harness.project.service.TodoService
    todo_extras: list = field(default_factory=list)  # Plan A task tools
    live_panel: object | None = None       # cc_harness.project.live.TodoLivePanel
    resume_task: object | None = None      # cc_harness.project.models.TodoTask | None
```

- [ ] **Step 2: run_repl 接线**

按 spec 组件 9 步骤 1-5 改 `run_repl()`:
1. 启动检测 + 自动 init(无 manifest → `init_noninteractive`)
2. 加载 TodoService + Live.start()
3. `state.todo_extras = inject_todo_tools(svc, session_id)`,拼接 `extra_native_specs` 时 None-safe
4. resume 询问(只 `resume_mode == "ask"` 且 `turns == 0`)→ `_select_resume_task` → 设 `state.resume_task`
5. `run_turn` 调用传 `resume_task=state.resume_task`
6. after-turn:`_after_turn_todo(state, todo_service)`(占位 pass)
7. exit 时 `await shutdown_session_executor()` 前 `live_panel.stop()`

- [ ] **Step 3: 测试**

```python
# tests/test_project_repl_integration.py
import asyncio
from unittest.mock import patch, MagicMock
from cc_harness.repl import run_repl, ReplState
from cc_harness.project.models import Manifest

async def test_run_repl_auto_init(tmp_path, monkeypatch):
    # mock llm + mcp,验证自动 init + Live 启动
    monkeypatch.chdir(tmp_path)
    llm = MagicMock(); mcp = MagicMock()
    mcp.list_tools.return_value = []
    mcp.shutdown = AsyncMock()
    # 不让 input() 阻塞
    monkeypatch.setattr("asyncio.to_thread", lambda fn, *a: asyncio.Future())
    # 跑一帧然后退出
    asyncio.create_task(run_repl(llm, mcp, cwd=str(tmp_path), default_mode="coding"))
    await asyncio.sleep(0.1)
    assert (tmp_path / ".cc-harness" / "project.yaml").exists()
```

- [ ] **Step 4: 跑全套验证不破坏现有**

Run: `.venv/Scripts/python.exe -m pytest tests/ -v --ignore=tests/_test_*.py`
Expected: PASS(包含现有 ~200 tests)

- [ ] **Step 5: commit**

```bash
git add cc_harness/repl.py tests/test_project_repl_integration.py
git commit -m "feat(repl): Plan A 6 处接线 — 自动 init + Live + resume_task + after-turn 钩子"
```

### Task 6.4: main.py — argparse CLI 分派

- [ ] **Step 1: 改 argparse**

```python
# cc_harness/main.py:_parse_args()
p.add_argument("command", nargs="?", choices=("init", "todo", "resume"), default=None)
p.add_argument("subcommand", nargs="?", default=None)
p.add_argument("--resume-id", type=str, default=None)
p.add_argument("--no-resume", action="store_true")
p.add_argument("--no-live", action="store_true")
# ... 既有 --mode / --design-dir 不动
```

- [ ] **Step 2: main() 分派**

```python
def main():
    args = _parse_args()
    if args.command == "init":
        from cc_harness.cli.init import cmd_init
        sys.exit(cmd_init(args, PROJECT_ROOT))
    elif args.command == "todo":
        from cc_harness.cli.todo import cmd_todo
        sys.exit(cmd_todo(args, PROJECT_ROOT))
    elif args.command == "resume":
        from cc_harness.cli.resume import cmd_resume
        sys.exit(cmd_resume(args, PROJECT_ROOT))
    # else: REPL 模式(原有逻辑)
    ...
```

- [ ] **Step 3: 跑现有测试 + commit**

```bash
git add cc_harness/main.py
git commit -m "feat(main): argparse 接受 init/todo/resume CLI 子命令分派"
```

---

## Task 7: E2E 集成测试(慢,`_test_*` 前缀)

**Files:**
- Create: `tests/_test_project_e2e.py`

- [ ] **Step 1: 写最小 E2E**

```python
# tests/_test_project_e2e.py
"""E2E 跑真实 LLM。手动跑:`pytest tests/_test_project_e2e.py --no-header -s`"""
import pytest, os

pytestmark = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="needs OPENAI_API_KEY for real LLM"
)

@pytest.mark.asyncio
async def test_agent_creates_and_completes_todo(tmp_path):
    # 1. init project
    # 2. 跑 REPL(用 FakeLLM 预编程 emit todo_create + todo_update)
    # 3. 验证 todos.yaml 落盘 + Live 显示更新
    pass  # 实现细节见 cc_harness.eval.locomo.runner 模式
```

- [ ] **Step 2: 跑通(若 OPENAI_API_KEY 已配) + commit**

```bash
git add tests/_test_project_e2e.py
git commit -m "test(project): E2E 真实 LLM todo lifecycle(默认跳过)"
```

---

## Task 8: 最终验证 + 覆盖率检查

- [ ] **Step 1: 跑全套测试**

```bash
.venv/Scripts/python.exe -m pytest tests/ -v --ignore=tests/_test_*.py --cov=cc_harness/project --cov=cc_harness/cli --cov-report=term-missing
```

Expected:
- 所有 tests PASS
- `status.py` + `dependency.py`:100% coverage
- `cc_harness/project` + `cc_harness/cli` 总:≥ 85% coverage
- Lint:`ruff check cc_harness/project cc_harness/cli`

- [ ] **Step 2: 手动 smoke**

```bash
# 新建 tmp 项目,跑 init → 创建 todo → 更新状态 → 验证
cd /tmp/smoke
cc-harness init --name smoke
cc-harness todo create "hello"
cc-harness todo list
cc-harness
> [coding] _
# (REPL 启动,Live 显示,resumeprompt 出现)
```

- [ ] **Step 3: 评估 / 跨 session smoke**

```bash
# session 1:建 todo 标 in_progress,退出
cc-harness
> [coding] /exit
# session 2:--resume,验证续干询问
cc-harness --resume
```

- [ ] **Step 4: commit(若有调整)**

---

## 自检清单(implementation 完成前必须全 ✓)

- [ ] `status.py` 100% 覆盖
- [ ] `dependency.py` 100% 覆盖
- [ ] `cc_harness/project` + `cc_harness/cli` 总 ≥ 85% 覆盖
- [ ] `ruff check cc_harness/project cc_harness/cli` 0 issue
- [ ] `pytest tests/ --ignore=tests/_test_*.py` 全 PASS
- [ ] 现有 ~200 个 test 不破(autoin-init 在 conftest fixture 里也兜底)
- [ ] 手动 smoke 6 步通过(init / create / list / update / resume / Live 显示)
- [ ] 跨 session smoke(session 1 exit → session 2 --resume 续干)通过

## 后续 plan(本 A 完成后再立)

- **Sub-project B**:外层 plan-execute-verify-replan loop + DAG 调度(`topo_sort` 在 dependency.py 补实现)
- **Sub-project C**:手动目标拆解(HTN)+ Checkpoint 自检(填 `_after_turn_todo` 占位)
- **Sub-project 其二**:SubAgent + Agent Team(独立 plan)

## 开放问题(本 plan 阶段回答)

spec 列了 8 条开放问题,本 plan 阶段的答案:

1. **init 行为最终选 "自动 init" 还是 "adhoc fallback"?** —— **自动 init**(spec 决策,Task 6.3 已落地)
2. **`cc-harness init` 二次执行是否提供 "merge" 选项?** —— **否**,二次 init 默认 `--force-reinit` 覆盖现有(用户主动行为);不实现 merge(避免 init 命令复杂度爆炸)
3. **`--resume-id <id>` flag 是否允许非交互模式?** —— **是**(`Task 5.4` 落地)
4. **todo_update 触发 status=done 时,是否联动 `completion_capture`?** —— **是,完成即触发**(`Task 3.1` + `Task 3.2` 已落地)
5. **并发 session 写入同一 yaml 的 conflict 解决?** —— **LWW 不加锁**;YAGNI(spec 决策);git 冲突走 git 解决流程
6. **Live panel 的 stderr/stdout 路由?** —— **stdout tty**;`_read_user` 用 `asyncio.to_thread(input)`,Rich Live 自动让出控制权(待 Task 6.1 真实跑验证一次;若失败改显式 stop/start)
7. **plan/design mode 下 todo_create 是否允许?** —— **CLI 可手动创建**;agent tool 在 plan/design 不注入(spec 决策)
8. **session_id 长度限制?** —— `repl-{int(time.time())}-{uuid4 hex[:8]}`(加 hex 后缀避免同秒重复,共 ~25 字符)