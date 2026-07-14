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

- [ ] **Step 0: 准备 fixtures 目录**(基础设施,后续 task 复用)

```bash
mkdir -p tests/fixtures/project_minimal/.cc-harness/todos
mkdir -p tests/fixtures/project_with_tasks/.cc-harness/todos
mkdir -p tests/fixtures/project_invalid/.cc-harness/todos
```

同时在 `tests/conftest.py` 加 fixture factory:
```python
@pytest.fixture
def tmp_project(tmp_path):
    """Create minimal project layout. Returns Path to project root."""
    proj = tmp_path / "proj"
    proj.mkdir()
    cc = proj / ".cc-harness"
    cc.mkdir()
    (cc / "todos").mkdir()
    (cc / "todos" / "todos.yaml").write_text("tasks: []\n", encoding="utf-8")
    return proj

@pytest.fixture
def tmp_project_with_tasks(tmp_path):
    """Create project with 6 sample tasks. See Task 1.3 for fixture content."""
    # 由 Task 1.3 步骤产出
    ...
```

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
- md 文件 frontmatter:`---\nid: ...\ntitle: ...\n...\n---\n\n{description body}`(**save 时 frontmatter 写 T15 全字段**,便于人直接 cat 看)
- **save / load 行为对应**(单向):
  - **save**:md frontmatter = yaml T15 全字段(含 active_sessions / id / created_at / updated_at);body = description markdown 原文
  - **load**:yaml 是主索引,所有字段以 yaml 为准;md frontmatter **仅当 yaml 缺 description 字段时回退填入**(即 yaml.description 为空字符串 + md 存在 → 用 md.body 填充);md frontmatter 其他字段冲突 → warn log + 以 yaml 为准
- md 缺失 → warn + description="";md 多余(yaml 不引用)→ warn + 询问用户,绝不静默 prune
- 原子写:`todos.yaml.tmp` → `os.replace`;md 写前先 `mkdir -p todos/`
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

- [ ] **Step 1: 写失败测试(parametrize 全 19 转移)**

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

# 合法转移 13 个(spec 组件 3 完整规则表)
VALID_TRANSITIONS = [
    ("pending", "in_progress"), ("pending", "cancelled"), ("pending", "blocked"),
    ("in_progress", "pending"), ("in_progress", "done"), ("in_progress", "blocked"),
    ("in_progress", "cancelled"),
    ("blocked", "in_progress"), ("blocked", "cancelled"), ("blocked", "pending"),
    ("cancelled", "pending"),
    # 同状态 idempotent 也算合法(spec 隐含;实际实现允许)
    ("pending", "pending"), ("in_progress", "in_progress"),
]

@pytest.mark.parametrize("current,target", VALID_TRANSITIONS)
def test_status_guard_valid_transitions(current, target):
    status_guard(_task(current), target)  # OK,不抛

# 非法转移 5 个(done 终态 4 种 + unknown status 1 种)
INVALID_TRANSITIONS = [
    ("done", "pending"), ("done", "in_progress"), ("done", "blocked"), ("done", "cancelled"),
    ("pending", "garbage"),
]

@pytest.mark.parametrize("current,target", INVALID_TRANSITIONS)
def test_status_guard_invalid_transitions(current, target):
    with pytest.raises(StatusGuardError):
        status_guard(_task(current), target)

def test_status_guard_done_terminal_message():
    with pytest.raises(StatusGuardError, match="done is terminal"):
        status_guard(_task("done"), "pending")
```

- [ ] **Step 2-4**:失败 → 实现 → 通过

实现:按 spec 组件 3 规则表实现 `status_guard(current: TodoTask, new_status: str) -> None`,raise `StatusGuardError` on illegal transition。

- [ ] **Step 5: 100% 覆盖验证(branch coverage)**

Run: `.venv/Scripts/python.exe -m pytest tests/test_project_status.py --cov=cc_harness/project/status --cov-branch --cov-report=term-missing`
Expected: 100% line + branch coverage(parametrize 13 valid + 5 invalid + done_message = 19 cases 真正覆盖所有分支)

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

实现要点(spec 组件 2 + 7 + 10):
- `TodoService(project_root, manifest, llm=None, memory_service: MemoryService | None = None)` — **memory_service 是 opt-in completion_capture 钩子的依赖,REPL 接线时由 `state.mem_deps["service"]` 注入(见 Task 6.3 step 2)**
- 7 操作:list/get/create/update/delete/resolve/validate(签名照 spec)
- `subscribe(callback) / unsubscribe(callback)`(callback 收 TodoTask + TodoEvent)
- `update()` 内部:`prev_status != "done" and task.status == "done"` 时 await `self._on_completion(task)`(`_on_completion` 内部 await `on_task_completion(task, self.manifest, self.memory_service)`,memory_service 为 None 或 completion_capture=False 时立即 return)
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
    # ToolResult 是 dataclass,不是字符串;对齐 cc_harness/mcp_client.py:ToolResult
    assert "[todo_create]" in result.llm
    assert "hello" in result.llm

async def test_create_handler_error_message_llm_friendly(deps):
    a = await deps["service"].create(title="a")
    # 让 a 依赖自己 → 子图环检测抛 DependencyCycleError
    result = await todo_update_handler(
        {"task_id": a.id, "depends_on": [a.id]}, **deps)
    assert "DependencyCycleError" in result.llm
    assert "cycle" in result.llm.lower()  # 链式路径保留
```

> **注**:handler 返回 `cc_harness.mcp_client.ToolResult(llm=..., display=...)`(不是字符串或 `.llm_text`)。`llm` 字段给 LLM 看的紧凑文本,`display` 给 Rich 渲染用(可省略)。

- [ ] **Step 2-4**:失败 → 实现 → 通过

实现要点(spec 组件 7):
- 7 个 SPEC dict 完整定义(参数 schema 照 spec)
- 7 个 handler:`async def xxx_handler(args, *, service, session_id, cwd) -> ToolResult`
  - 成功例:```ToolResult(llm=f"[todo_create] ✓ created task {t.id}\ntitle: {t.title}\nstatus: {t.status}\nid: {t.id}", display=None)```
  - 错误例:```ToolResult(llm=f"[todo_create] ✗ {type(e).__name__}: {detail}\n  {chain_path}", display=None)```(保留 spec 示例的循环路径 `jkl-012 -> xyz-789 -> jkl-012`)
- `inject_todo_tools(service, session_id) -> list[dict]`:返 `[{"spec": ..., "handler": ..., "deps": {"service": service, "session_id": session_id}}, ...]`

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
from unittest.mock import patch
from cc_harness.cli.init import init_noninteractive

def test_init_noninteractive_creates_files(tmp_path):
    m = init_noninteractive(tmp_path, name="myapp")
    assert (tmp_path / ".cc-harness" / "project.yaml").exists()
    assert (tmp_path / ".cc-harness" / "todos" / "todos.yaml").exists()
    assert m.name == "myapp"
    assert m.project_id  # uuid 生成

def test_init_noninteractive_no_git_skips_gitignore(tmp_path):
    """tmp_path 不是 git repo → 不写 .gitignore。"""
    init_noninteractive(tmp_path, name="x")
    assert not (tmp_path / ".gitignore").exists()

def test_init_noninteractive_in_git_writes_gitignore(tmp_path):
    """git 探测成功 → 自动写 .gitignore(只排除 .md,保留 project.yaml)。"""
    # mock git rev-parse 返回 success
    with patch("subprocess.run") as mock_run:
        from unittest.mock import MagicMock
        mock_run.return_value = MagicMock(returncode=0, stdout="true", stderr="")
        init_noninteractive(tmp_path, name="x")
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".cc-harness/todos/*.md" in gitignore
    assert ".cc-harness/project.yaml" not in gitignore  # manifest 不排除
```

- [ ] **Step 2-4**:失败 → 实现 → 通过

实现要点:
- `init_noninteractive(cwd, name) -> Manifest`:写 project.yaml + 空 todos.yaml + todos/.gitkeep
  - **git 探测**:`subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd, capture_output=True)` 命中则**自动写** `.gitignore`(追加 `.cc-harness/todos/*.md`、`!*.yaml`、保留 `project.yaml`);非 git → skip
- `init_interactive(cwd)` 用 `rich.prompt` 询问 name/description/resume-mode/live/gitignore;存在时询问 re-init/merge/abort

- [ ] **Step 5: commit**

```bash
git add cc_harness/cli/init.py tests/test_cli_init.py
git commit -m "feat(cli): init 交互 + 非交互 + 已存在时询问"
```

### Task 5.3: todo 子命令(7 个)

- [ ] **Step 1: 写失败测试(in-process,不用 subprocess)**

```python
# tests/test_cli_todo.py
import pytest
from cc_harness.cli.init import init_noninteractive
from cc_harness.cli.todo import cmd_todo

@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    init_noninteractive(proj, name="t")
    monkeypatch.chdir(proj)
    return proj

def test_cli_todo_create_and_list(project_dir, capsys):
    from argparse import Namespace
    # create
    args = Namespace(
        subcommand="create", title="hello",
        description="", depends_on=None, parent=None,
        assigned_to=None, priority=None, label=None,
        due_date=None, effort_estimate=None,
        acceptance_criteria=None, json=False,
    )
    rc = cmd_todo(args, project_dir)
    out = capsys.readouterr().out
    assert rc == 0
    assert "[todo_create]" in out
    # list
    args2 = Namespace(
        subcommand="list", status=None, parent=None,
        no_done=False, json=False, format="table",
        sort="status", limit=20,
    )
    rc = cmd_todo(args2, project_dir)
    out = capsys.readouterr().out
    assert rc == 0
    assert "[todo_list]" in out
    assert "hello" in out
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

- [ ] **Step 1: 写失败测试(render + _read_user monkeypatch 兼容)**

```python
# tests/test_project_live.py
import asyncio
from rich.console import Console
from io import StringIO
from unittest.mock import patch, AsyncMock
from cc_harness.project.live import TodoLivePanel
from cc_harness.project.service import TodoService
from cc_harness.project.models import Manifest, TodoTask
from datetime import datetime

def _make_manifest():
    return Manifest(project_id="abc", name="x", todos_path=".cc-harness/todos",
                    created_at=datetime.now())

def test_render_empty():
    console = Console(file=StringIO(), force_terminal=True, width=80)
    panel = TodoLivePanel._render_static(console, tasks=[], project_name="x", project_id="abc")
    text = console.file.getvalue()
    assert "x" in text
    assert "abc" in text
    assert "no tasks" in text.lower() or "0 tasks" in text

def test_render_with_tasks():
    console = Console(file=StringIO(), force_terminal=True, width=80)
    now = datetime.now()
    tasks = [
        TodoTask(id="aaa11111", title="done task", status="done",
                 created_at=now, updated_at=now, description="",
                 depends_on=[], parent_task=None, assigned_to=None,
                 priority=None, labels=[], due_date=None,
                 effort_estimate=None, acceptance_criteria=[], active_sessions=[]),
        TodoTask(id="bbb22222", title="active task", status="in_progress",
                 created_at=now, updated_at=now, description="",
                 depends_on=[], parent_task=None, assigned_to=None,
                 priority="high", labels=[], due_date=None,
                 effort_estimate=None, acceptance_criteria=[], active_sessions=[]),
    ]
    TodoLivePanel._render_static(console, tasks, project_name="x", project_id="abc")
    text = console.file.getvalue()
    assert "done task" in text
    assert "active task" in text
    assert "high" in text

async def test_live_panel_does_not_break_read_user(tmp_path):
    """Live panel start 后, monkeypatch _read_user 仍能被调(REPL 测试兼容)。
    验证方式:起 Live → 调 _read_user mock → Live.stop,确认无异常。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".cc-harness" / "todos").mkdir(parents=True)
    (proj / ".cc-harness" / "todos" / "todos.yaml").write_text("tasks: []\n")
    svc = TodoService(project_root=proj, manifest=_make_manifest())
    panel = TodoLivePanel(Console(), svc, _make_manifest())
    panel.start()
    # 模拟 _read_user 返回 "exit"
    with patch("cc_harness.repl._read_user", new=AsyncMock(return_value="exit")):
        from cc_harness.repl import _read_user as mocked
        result = await mocked()
        assert result == "exit"
    panel.stop()  # 必须能正常 stop,不抛
```

- [ ] **Step 2-4**:失败 → 实现 → 通过

实现:
- `TodoLivePanel._render_static(console, tasks, project_name, project_id) -> Panel`(**纯函数**,可单测)
- `TodoLivePanel.start()` / `.stop()` / `_on_change(task, event)` 围绕 Rich `Live` context manager(方案 B)
- `_on_change` 内部:`_tasks = asyncio.run_coroutine_threadsafe(service.list(), get_event_loop()).result()` + `self._live.update(self._render())`
- **monkeypatch 兼容**:`_read_user` 用 `asyncio.to_thread(input)` 时,Rich Live 自动让出 stdout 控制权;现有 `tests/test_repl.py` 的 `monkeypatch.setattr(repl_mod, "_read_user", ...)` 仍生效(Task 6.3 step 3 测试 + 该 test 共同验证)
- **conftest fixture 提供 no-op Live 模式**:`fake_todo_service` 测试不走真 Live,只过订阅路径

- [ ] **Step 5: 补多任务 + 折叠 + 截断测试 + commit**

```bash
git add cc_harness/project/live.py tests/test_project_live.py
git commit -m "feat(project): TodoLivePanel _render + Live context(方案 B)"
```

### Task 6.2: agent.py — run_turn + _refresh_system_prompt

- [ ] **Step 1: 改 run_turn 签名 + PromptComposer 注入路径**

修改 `cc_harness/agent.py`:
- `run_turn(..., resume_task: TodoTask | None = None)` 新增参数(末尾参数,不破坏现有调用)
- `_refresh_system_prompt(messages, cwd, mode, resume_task=None)` 加参数
- **SECTION_POOL 注入路径**(关键,spec 没完全写明):
  ```python
  # 在 _refresh_system_prompt 末尾:
  if mode == "coding" and resume_task is not None:
      from cc_harness.prompts import Section
      resume_section = Section(
          name="resume_task",
          content=f"## Resume Task (跨 session 续干)\n"
                  f"id:    {resume_task.id}\n"
                  f"title: {resume_task.title}\n"
                  f"status:{resume_task.status}\n"
                  f"priority:{resume_task.priority or 'none'}\n"
                  f"active_sessions: {resume_task.active_sessions}\n\n"
                  f"## Acceptance Criteria\n"
                  + "\n".join(f"- {c}" for c in resume_task.acceptance_criteria),
          priority=30,
          conditions=("mode==coding",),
      )
      messages[0]["content"] = build_system_prompt(
          cwd, mode, extra_sections=[resume_section]
      )
  ```
- 改 `build_system_prompt(cwd, mode, extra_sections=None)` 加可选参数,内部传给 `PromptComposer(..., extra=extra_sections)`

- [ ] **Step 2: 测试验证不破坏现有**

Run: `.venv/Scripts/python.exe -m pytest tests/test_agent.py -v`
Expected: PASS(没改默认行为,新参数默认 None)

- [ ] **Step 3: commit**

```bash
git add cc_harness/agent.py cc_harness/prompts.py
git commit -m "feat(agent): run_turn + _refresh_system_prompt 支持 resume_task (SECTION_POOL 注入)"
```

### Task 6.3: repl.py — 6 处接线

- [ ] **Step 1: 改 ReplState + session_id 格式**

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
    session_id: str = ""  # 改为 "repl-{int(time.time())}-{uuid4().hex[:8]}"(spec 开放问题 8)
    mem_deps: dict | None = None
    # Plan A 新增:
    project_root: Path | None = None        # 统一锚点(避免裸 cwd 漂移)
    manifest: object | None = None         # cc_harness.project.models.Manifest
    todo_service: object | None = None     # cc_harness.project.service.TodoService
    todo_extras: list = field(default_factory=list)  # Plan A task tools
    live_panel: object | None = None       # cc_harness.project.live.TodoLivePanel
    resume_task: object | None = None      # cc_harness.project.models.TodoTask | None
```

- [ ] **Step 2: run_repl 接线**

按 spec 组件 9 步骤 1-5 改 `run_repl()`:
0. **统一锚点**:`state.project_root = Path(cwd).resolve()`,TodoService / TodoStorage / policy / executor 全部用 `state.project_root`(避免裸 cwd 漂移导致 sandbox mount 边界错位)
1. **启动检测 + 自动 init**:无 manifest → `init_noninteractive(state.project_root, name=state.project_root.name)`
2. **加载 TodoService**(关键:注入 memory_service):
   ```python
   memory_svc = state.mem_deps.get("service") if state.mem_deps else None
   state.todo_service = TodoService(
       project_root=state.project_root, manifest=manifest,
       memory_service=memory_svc,  # opt-in completion_capture 钩子依赖
   )
   state.live_panel = TodoLivePanel(console, state.todo_service, manifest)
   state.live_panel.start()
   ```
3. `state.todo_extras = inject_todo_tools(state.todo_service, session_id=state.session_id)`,拼接 `extra_native_specs` 时 **None-safe**:`_all_extras = list(state.memory_extras or []) + list(state.todo_extras or []); extra_native_specs=_all_extras or None`
4. resume 询问(只 `resume_mode == "ask"` 且 `turns == 0`)→ `_select_resume_task(tasks)`(规则:max(updated_at) among in_progress,None if 0)→ 设 `state.resume_task`
5. `run_turn` 调用传 `resume_task=state.resume_task`
6. after-turn:`_after_turn_todo(state, todo_service)`(占位 pass,B 阶段填 verify)
7. exit 时 `await shutdown_session_executor()` 前 `await state.live_panel.stop()`

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

- [ ] **Step 1: 改 argparse(sub-commands pattern,向后兼容)**

```python
# cc_harness/main.py:_parse_args()
# 关键:用 sub-commands 避免 positional 冲突 + 保留 --mode/--design-dir 兜底 REPL 入口
sub = p.add_subparsers(dest="command")

p_init = sub.add_parser("init", help="Initialize .cc-harness/project.yaml")
p_init.add_argument("--name", type=str)
p_init.add_argument("--no-prompt", action="store_true")
p_init.add_argument("--resume-mode", choices=("ask", "auto", "manual"))
p_init.add_argument("--no-live", action="store_true")
p_init.add_argument("--force-reinit", action="store_true")

p_todo = sub.add_parser("todo", help="Manage todos")
p_todo.add_argument("subcommand", choices=("list", "get", "create", "update", "delete", "resolve", "validate"))
# 各种 --flag ...

p_resume = sub.add_parser("resume", help="Resume in-progress task")
p_resume.add_argument("--resume-id", type=str)
p_resume.add_argument("--no-resume", action="store_true")

# 保留 REPL 默认入口(无 command 时):
p.add_argument("--mode", choices=("coding", "plan", "design", "chat"), default="coding")
p.add_argument("--design-dir", type=Path, default=None)
```

**向后兼容守卫**:
- `python main.py` 无参数 → 走 REPL(原有行为)
- `python main.py --mode coding` → 走 REPL(原有行为)
- `python main.py init` → CLI 分派
- `python main.py todo create "x"` → CLI 分派
- `python main.py resume` → CLI 分派

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
    else:
        # 走原 REPL 逻辑(不破坏)
        ...
```

- [ ] **Step 3: 跑现有测试 + commit**

```bash
git add cc_harness/main.py
git commit -m "feat(main): argparse sub-commands(init/todo/resume) + REPL 向后兼容守卫"
```

---

## Task 7: E2E 集成测试(慢,`_test_*` 前缀)

**Files:**
- Create: `tests/_test_project_e2e.py`

- [ ] **Step 1: 写最小 E2E**

```python
# tests/_test_project_e2e.py
"""E2E 跑真实 LLM。手动跑:`pytest tests/_test_project_e2e.py --no-header -s`

默认跳过(无 OPENAI_API_KEY 时)。前缀下划线,pytest 默认不收集。
"""
import os
import pytest
from cc_harness.cli.init import init_noninteractive
from cc_harness.project.service import TodoService
from cc_harness.project.models import Manifest
from datetime import datetime

pytestmark = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="needs OPENAI_API_KEY for real LLM",
)

def _manifest(tmp_path) -> Manifest:
    return init_noninteractive(tmp_path, name="e2e")

@pytest.mark.asyncio
async def test_service_lifecycle_full(tmp_path):
    """纯 Service 层 E2E(无 REPL / LLM):完整 create → update → delete 循环。"""
    manifest = _manifest(tmp_path)
    svc = TodoService(project_root=tmp_path, manifest=manifest)
    t1 = await svc.create(title="task 1")
    t2 = await svc.create(title="task 2", depends_on=[t1.id])
    # 完成 t1,触发 completion_capture(若 opt-in),完成 t2
    await svc.update(t1.id, status="done")
    await svc.update(t2.id, status="in_progress")
    # 验证 active_sessions 累计
    fetched1 = await svc.get(t1.id)
    fetched2 = await svc.get(t2.id)
    assert fetched1.status == "done"
    assert fetched2.status == "in_progress"
    assert t2.id in (await svc.resolve(t2.id))[0].id  # resolve 含自己
    # 删 t1(force,因 t2 depends_on)
    await svc.delete(t1.id, force=True)
    issues = await svc.validate()
    assert any(i.rule_id == "missing_dependency" for i in issues)


@pytest.mark.asyncio
async def test_repl_with_real_llm(tmp_path):
    """完整 REPL + 真实 LLM E2E:模拟用户输入,LLM 调 todo tool,验证落盘。"""
    # 复用 cc_harness.eval.locomo.runner 的 REPL 启动模式 + FakeLLM 预编程
    # 实际实现见 locomo runner 风格;这里给骨架
    from unittest.mock import AsyncMock, patch
    manifest = _manifest(tmp_path)
    # ... build llm with tool_calls emit todo_create + todo_update
    # ... run repl with monkeypatched _read_user yielding prompts
    # ... assert (tmp_path / ".cc-harness/todos/todos.yaml") contains expected tasks
    pass
```

- [ ] **Step 2: 跑通(若 OPENAI_API_KEY 已配)+ commit**

```bash
git add tests/_test_project_e2e.py
git commit -m "test(project): E2E Service 完整 lifecycle + REPL 骨架(默认跳过)"
```

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