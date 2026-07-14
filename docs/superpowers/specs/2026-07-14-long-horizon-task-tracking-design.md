# 其一·长程任务 — Sub-project A 任务追踪底座设计

> **范围**:cc-harness AI 工程目标"其一·长程任务" 5 红 + 3 黄中的 **A 子集**——任务追踪底座(Project 容器 + Todo 任务清单 + 跨 session resume)。B(外层 loop + DAG)/ C(HTN + checkpoint 自检)在 A 完成后另立 spec。
>
> **字段计数约定**(全文统一):TodoTask 共 **15 字段** = **13 用户可控**(title / description / status / depends_on / parent_task / assigned_to / priority / labels / due_date / effort_estimate / acceptance_criteria + id / created_at / updated_at 由系统自动生成)+ **1 系统**(active_sessions)。全文出现 "13" 指**用户可控字段**;"14" 指**用户可控 + 自动生成**;"15" 指**完整 dataclass**。下文用"T13" / "T14" / "T15"区分以避免歧义。
>
> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。

## Goal

把当前缺失的 **3 红**(Todo / 项目容器 / 跨 session resume)一次落地,提供:

1. **项目容器** —— `.cc-harness/project.yaml` manifest,跨 session/跨机器稳定识别项目身份
2. **Todo 任务清单** —— 文件式持久化(yaml 主索引 + 每任务 md),T13 字段完整建模,人/agent 双通道读写
3. **跨 session resume** —— 启动检测 + 询问续干上次 in_progress 任务,session_id 累加到 task 的 `active_sessions`

完成 A 后,B/C 阶段不用改 schema 直接接入(字段集已预留 `depends_on` / `parent_task` / `acceptance_criteria` / `assigned_to`)。

## 现有代码事实(spec 写入时核实)

| 文件 | 现状 | A 处置 |
|---|---|---|
| `cc_harness/agent.py` | 单层 ReAct `run_turn` max_iter=20;`extra_native_specs` 已支持注入 tool | 新增 7 个 todo tool 注入 |
| `cc_harness/repl.py` | `ReplState` 含 `session_id` / `mem_deps` / `memory_extras`;`_after_turn_memory` 已实现 memory after-turn 钩子 | 启动时检测 manifest + 加载 TodoService + 启动 Live;`_after_turn_todo` 新增钩子 |
| `cc_harness/memory/extras.py` | `build_memory_extras` 构造 memory tools + deps | **不直接合并**;新建 `cc_harness/project/extras.py` 同模式构造 todo tools |
| `cc_harness/memory/offload/mermaid.py` | Mermaid canvas 累积节点+边链 | **不冲突**,A 是另一个维度 |
| `cc_harness/config.py` | `load_config` 读 .env + mcp.json | **不动**,project manifest 走独立模块 |
| `cc_harness/policy.py` | L4 权限闸门 allow/ask | **不动**,todo 操作走自己 service |
| `tests/test_agent.py` | FakeLLM / FakeMCP | 复用,A 阶段不重写 |
| `docs/superpowers/specs/2026-07-12-layered-memory-design.md` | L0-L3 记忆 spec | **互补不冲突**,memory 管"知识",todo 管"任务" |

## 关键决策(brainstorm 9 题确认 + 本 spec 核实)

1. **A → B → C 串行**:本 spec 只做 A,B/C 在 A 完成后独立 spec
2. **Project = 显式 manifest**(`.cc-harness/project.yaml`),不动 cwd 隐式方案
3. **Todo 字段集 = T13 字段**(用户可控 13 字段;id/created_at/updated_at 由系统生成 + active_sessions 追加,共 T15)
4. **Todo 文件 = 混合**(主索引 `todos.yaml` + 每任务 `todos/<id>.md` frontmatter)
5. **Todo 操作集 = 完整集**(list/get/create/update/delete/resolve/validate + 内部状态守卫 + 依赖校验)
6. **提供方式 = CLI + agent tool 双通道**,共享 TodoService
7. **进度展示 = Rich Live 组件**,REPL 顶部常驻,spinner + 列表 + 进度条
8. **Resume 触发 = 检测 + 询问**,不静默自动续干
9. **状态守卫 = done 不可逆**,其他自由
10. **session↔task 映射 = `task.active_sessions` append-only 数组**
11. **Todo ↔ memory = 默认互不写入**,仅 `completion_capture` opt-in 钩子
12. **session_id = 三系统统一**(REPL / memory / todo 共享 `state.session_id`)
13. **YAGNI 边界**:**不**做 B/C 阶段的事(HTN 调度器、verify hook、自动 plan-execute loop、acceptance 自动抽取)

## 架构概览

```
┌─────────────────────────────────────────────────┐
│              cc-harness CLI / REPL              │
│  (main.py / repl.py)                            │
└─────────────┬─────────────────────┬─────────────┘
              │                     │
              ▼                     ▼
┌─────────────────────┐  ┌──────────────────────┐
│  cc-harness todo    │  │  TodoService         │
│  (CLI:init/list/    │◄─┤  (单一真相源)        │
│   create/update)    │  │  - list/get          │
│                     │  │  - create/update/del │
│  cc-harness resume  │  │  - resolve/validate  │
│  (CLI: --resume)    │  │  - status_guard      │
│                     │  │  - dep_check         │
└─────────────────────┘  └───┬──────────┬───────┘
                             │          │
                             ▼          ▼
              ┌──────────────────┐  ┌──────────────────┐
              │ .cc-harness/     │  │ .cc-harness/     │
              │ project.yaml     │  │ todos.yaml       │
              │ (manifest)       │  │ todos/<id>.md    │
              └──────────────────┘  └──────────────────┘
                                          ▲
                                          │ 读
                                          │
              ┌──────────────────────────┴──┐
              │     TodoLivePanel           │
              │     (Rich Live 组件)        │
              │     spinner + 列表          │
              │     REPL 顶部常驻           │
              └─────────────────────────────┘
                          ▲
                          │ 推
              ┌───────────┴───────────────┐
              │  agent tool 集成          │
              │  (extra_native_specs)     │
              │  todo_list/get/create/    │
              │  update/delete/           │
              │  resolve/validate         │
              └───────────────────────────┘
```

## 数据流(3 条)

### 流 1:启动 → 检测项目 → resume

```
main.py 启动
  → 检查 cwd 是否有 .cc-harness/project.yaml
    → 无:提示 cc-harness init(进入 init wizard)
    → 有:加载 manifest
      → TodoService 读 todos.yaml + todos/*.md
        → TodoLivePanel 启动(spinner + 列表)
          → 打印: "📂 Project: cc-harness (id=xxx)"
                  "📋 5 tasks: 3 done / 1 in_progress / 1 pending"
          → 询问: "上次 in_progress 是 [abc-123] 完成 hello.py,继续?(y/n/选其他)"
            → y:继续该 task,agent 在 system prompt 注入该 task 上下文
            → n:用户选其他或新建
```

### 流 2:agent 在 ReAct 循环里操作 todo

```
LLM 判断: "我该把当前 task 标 done,创建下一个"
  → emit tool_calls: [
      todo_update(id="abc-123", status="done"),
      todo_create(title="写测试", depends_on=["def-456"])
    ]
  → run_turn 派发到 TodoService
    → TodoService.update → 写 todos.yaml + 更新 todos/<id>.md
    → TodoService.create → 加新行到 todos.yaml + 新建 todos/<id>.md
      → TodoLivePanel 收到推送,刷新显示(spinner 移到新 task)
```

### 流 3:跨 session 恢复

```
昨天 session-A:
  → todo_create(id="abc-123", active_sessions=["session-A"])
  → todo_update(id="abc-123", status="in_progress")
  → session 退出,manifest 和 todos 都已落盘

今天 session-B 启动:
  → 加载 todos
    → 看到 abc-123 in_progress,active_sessions 包含 session-A(已死)
      → 询问: "abc-123 还在 in_progress,继续吗?是否更新 active_sessions?"
        → y:add session-B to active_sessions
        → n:选其他
```

## 组件详细定义

### 组件 1:Manifest schema(`cc_harness/project/manifest.py`)

`.cc-harness/project.yaml` 字段:

```yaml
# === 必填 ===
project_id: 7f3a-2b8c-a91d        # UUID v4,稳定唯一,跨 session/机器
name: cc-harness                   # 人类可读名

# === 必填 ===
todos_path: .cc-harness/todos      # todo 文件目录(相对项目根)
                                 # 内部约定:
                                 #   {todos_path}/todos.yaml    # 主索引
                                 #   {todos_path}/<id>.md       # 每任务 md

# === 必填 ===
created_at: 2026-07-14T10:00:00Z  # ISO 8601 UTC

# === 可选 ===
schema_version: 1                  # 当前 1
memory:
  db_path: logs/memory.db          # 引用 memory db 路径
  integration:
    completion_capture: false      # todo 完成 → memory 钩子(默认关)
resume_mode: ask                   # ask | auto | manual
live:
  position: top                    # top | bottom | off
  max_height: 10
  spinner_style: dots
  show_progress_bar: true
  fold_done: 5
  colors:
    done: green
    in_progress: cyan
    pending: dim
    blocked: yellow
    cancelled: grey50
```

**约束**:
1. `project_id` 不可改(防止跨 session 引用断裂)
2. 启动时校验:
   - `project_id` / `name` / `todos_path` / `created_at` 必填
   - `schema_version` 已知(未知 → `ManifestError`,fail-closed,提示升级 cc-harness)
   - `resume_mode` 合法(枚举内)
   - **未知字段** → warn log,**不报错**(`extra='ignore'` 风格,允许用户手编加 `experimental: true` 类字段)
   - **可选字段缺省** → 走默认,绝不抛错
3. `project.yaml` 用 PyYAML `safe_load`,UTF-8,2-space 缩进(与现有 pyproject 一致)

### 组件 2:TodoService(`cc_harness/project/service.py`)

**数据类**(`cc_harness/project/models.py`):

```python
@dataclass
class TodoTask:
    # T13 用户可控字段
    id: str                              # 系统生成(UUID 短码 8 hex)
    title: str                           # 必填
    status: Literal["pending","in_progress","done","blocked","cancelled"]  # 必填
    description: str                     # markdown
    depends_on: list[str]                # task id 列表
    parent_task: str | None              # 父 task id(给 HTN 嵌套留口)
    assigned_to: str | None              # "user" 或 agent name(给 SubAgent 留口)
    priority: Literal["low","medium","high","critical"] | None
    labels: list[str]
    due_date: datetime | None
    effort_estimate: float | None
    acceptance_criteria: list[str]       # 给 checkpoint 自检留口

    # T14 自动生成
    created_at: datetime                 # 系统生成
    updated_at: datetime                 # 系统生成

    # T15 系统字段
    active_sessions: list[str]           # append-only,最多 50 条(超过自动 prune)
```

**辅助数据类**:

```python
@dataclass
class ValidationIssue:
    task_id: str | None                  # 哪个 task;None 表示全局
    severity: Literal["error", "warning"]
    rule_id: str                         # 'missing_dependency' / 'cycle' / 'self_parent' / ...
    message: str                         # 人类/LLM 友好的描述
```

**Service API**:

```python
class TodoService:
    def __init__(self, project_root: Path, manifest: Manifest, llm: LLMClient | None = None): ...

    # 读操作
    async def list(self, *, status=None, parent_task=None, include_done=True) -> list[TodoTask]: ...
    async def get(self, task_id: str) -> TodoTask: ...                              # raise TaskNotFound
    async def resolve(self, task_id: str, *, include_done=True) -> list[TodoTask]: ...
    async def validate(self) -> list[ValidationIssue]: ...

    # 写操作
    async def create(self, *, title, description="", depends_on=None,
                     parent_task=None, assigned_to=None, priority=None,
                     labels=None, due_date=None, effort_estimate=None,
                     acceptance_criteria=None, session_id=None) -> TodoTask: ...
    async def update(self, task_id: str, *, session_id=None, **fields) -> TodoTask: ...
    async def delete(self, task_id: str, *, force=False) -> None: ...

    # 事件订阅(Live 组件用)
    def subscribe(self, callback: Callable[[TodoTask, str], None]) -> None: ...
    def unsubscribe(self, callback) -> None: ...
```

**异常体系**:

```python
class TodoError(Exception): ...
class TaskNotFound(TodoError): ...
class TaskAlreadyExists(TodoError): ...
class StatusGuardError(TodoError): ...
class DependencyCycleError(TodoError): ...
class InvalidFieldError(TodoError): ...
class ManifestError(TodoError): ...
```

### 组件 3:状态守卫(`cc_harness/project/status.py`)

**done 不可逆,其他自由**。完整规则表:

| 当前状态 | 允许的目标状态 |
|---|---|
| `pending` | `in_progress`, `cancelled`, `blocked` |
| `in_progress` | `pending`, `done`, `blocked`, `cancelled` |
| `blocked` | `in_progress`, `cancelled`, `pending` |
| `cancelled` | `pending` |
| **`done`** | —(**终态**) |

### 组件 4:依赖校验(`cc_harness/project/dependency.py`)

三种校验:

1. **引用完整性**(`_check_references`):`depends_on` / `parent_task` 引用的 task 必须存在;parent 不能 self
2. **全表环检测**(`_check_no_cycle`):DFS white/gray/black,O(V+E),`validate()` 时跑
3. **子图环检测**(`_dep_check`):`create()` / `update(depends_on=)` 时跑,只看相关子图

**A 阶段不做的事**(明确 YAGNI):
- ❌ **拓扑排序** `topo_sort()` —— A 不实现。B spec 在 `dependency.py` 里加 `# TODO: B 阶段实现 Kahn` 注释占位,A 阶段不要写不会被调用的代码。

### 组件 5:Storage(`cc_harness/project/storage.py`)

负责 yaml + md 文件读写:

```python
class TodoStorage:
    def __init__(self, project_root: Path, todos_path: str): ...
    async def load_all(self) -> list[TodoTask]: ...
    async def save_all(self, tasks: list[TodoTask]) -> None: ...
    async def load_task_md(self, task_id: str) -> str: ...
    async def save_task_md(self, task_id: str, content: str) -> None: ...
```

**约束**:
1. **单源真相**——只有 TodoService 调 Storage,不允许 CLI / tool 直接读写
2. **原子写**——yaml 写前 `.tmp` + `os.replace`
3. **md 文件 frontmatter 序列化** T13 字段(用户可控字段)+ id/created_at/updated_at/active_sessions;body = description markdown
4. **空目录创建**:`todos/` 不存在 → 自动创建 + 空 `todos.yaml`
5. **active_sessions 自动 prune**:`_add_active_session()` 时若长度 > 50 → 截断为最近 50 条 + 一行注释 `# earlier N sessions truncated at <timestamp>`。防止无界增长导致 yaml/md 膨胀。

**yaml ↔ md 合并策略**(A 阶段定义,防止外部编辑导致漂移):

- **load 时**:
  - yaml 是主索引,所有字段以 yaml 为准
  - md frontmatter 仅作 description 字段的来源(md frontmatter 的其他字段若与 yaml 冲突 → warning log,以 yaml 为准)
  - yaml 中引用但 md 文件不存在 → warning log + 把 description 字段置空字符串,**不报错**(允许只有 yaml 索引,无 md 文件)
  - md 文件存在但 yaml 不引用 → warning log + 询问用户(LLM tool 中返回 issue;CLI 中询问);**绝不静默 prune**

- **save 时**:
  - 任何 update/create 后,Service 自动同步 yaml + 对应 md 文件
  - 写 md 时:frontmatter = yaml 全字段(T15);body = description markdown
  - 删除 task 时(force=True 且无依赖):同步删 yaml 行 + md 文件

- **冲突检测**:`validate()` 检查"yaml 引用但 md 缺失"和"md 文件无 yaml 引用",返回 ValidationIssue

### 组件 6:Live 组件(`cc_harness/project/live.py`)

Rich `Live` 上下文管理器,REPL 顶部常驻。

**渲染策略决策**(spec 范围内选定 — `方案 B:Live + input 显式 stop/start`):

| 方案 | 优劣 | 选择 |
|---|---|---|
| A. `Console.status` 单行 spinner | 简单,但只能显示当前 task,不能显示完整列表 | ❌ 不符合"列表 + 进度条"需求 |
| **B. Rich Live 占顶部 N 行 + `input()` 期间显式 stop/start** | Live 全功能 + REPL 兼容,实现稍复杂 | ✅ **A 阶段选定** |
| C. 不持续刷新,每 turn 打印一次状态摘要 | 最简单,但无 spinner 动画 | ❌ 不符合"Claude Code 体验" |

**方案 B 实现要点**:
```python
class TodoLivePanel:
    def __enter__(self): self.start()
    def __exit__(self, *a): self.stop()

# REPL 主循环中:
with TodoLivePanel(...) as panel:
    while True:
        raw = await _read_user(prompt)   # Live 仍 active,Rich 管光标位置
        if raw.startswith("/"): ...       # slash 命令不走 LLM,Live 暂停显示
        await run_turn(...)              # 输出流向 Live 下方的 buffer
        panel.refresh()                  # 显式 refresh
```

**实测**:`_read_user` 用 `asyncio.to_thread(input, prompt)` 阻塞 stdin,Rich Live 自动让出控制权给 prompt;run_turn 输出到 stdout 通过 Live 的 refresh 区间**之外**,不会与 Live 重叠。spec 范围内验证一次。

> **后续 issue 追踪**:Windows 终端无 SIGWINCH,resize 不自动重渲染(spec 不解决);B 阶段如需可加 stdin 轮询 width。

**视觉布局**:

```
┌─ 📂 cc-harness (id: 7f3a-2b8c-a91d) ──────────────────┐
│ Progress: ████████░░░░ 4/6 (67%)                       │
│                                                         │
│ ✓ abc-123  完成 hello.py                          [done]│
│ ✓ def-456  设计新 API                              [done]│
│ ⠋ jkl-012  实现 todo 持久化       [high] [in_progress] │
│ ○ mno-345  添加单元测试            [medium]    [pending]│
│ ○ pqr-678  集成 Locomo eval                   [pending] │
└─────────────────────────────────────────────────────────┘
```

**关键实现**:
```python
class TodoLivePanel:
    def __init__(self, console: Console, service: TodoService, manifest: Manifest): ...
    def start(self) -> None: ...     # 进入 Live context,subscribe service
    def stop(self) -> None: ...      # 退出 Live context,unsubscribe
    def _render(self) -> Panel: ...  # 单测覆盖此方法
```

**状态图标**:
| status | 图标 | 颜色 |
|---|---|---|
| `done` | `✓` | green |
| `in_progress` | `⠋`(spinner) | cyan |
| `pending` | `○` | dim |
| `blocked` | `!` | yellow |
| `cancelled` | `✗` | grey50 |

**边界 case**:
- 任务 0 个 → "📋 no tasks yet"
- 任务 > 终端高度 → 按状态优先级折叠,底部 `... +N more`
- done 任务超过 `fold_done` → 折叠前 N 个
- 标题超长 → 自动截断 + `…`
- 终端 resize → Rich Live 自动重渲染

### 组件 7:Agent tools(`cc_harness/project/tools.py`)

7 个 tool specs,通过 `extra_native_specs` 注入 `run_turn`:

1. **todo_list**:列出(可按 status/parent_task 过滤,默认 limit=20)
2. **todo_get**:单个任务详情
3. **todo_create**:创建(T13 字段 + 系统自动生成 id/created_at/updated_at + active_sessions append)
4. **todo_update**:更新任一字段
5. **todo_delete**:删除(`--force` 才能删 done / 受依赖的)
6. **todo_resolve**:依赖链解析(返回 task + 所有传递依赖)
7. **todo_validate**:全表校验

**session_id 注入**(显式,不靠 env var):

```python
# cc_harness/project/tools.py
def inject_todo_tools(service: TodoService, session_id: str) -> list[dict]:
    """Return extra_native_specs entries. session_id 显式传入,handler 内通过 deps['session_id'] 读。"""
    deps = {"service": service, "session_id": session_id}
    return [
        {"spec": TODO_LIST_SPEC,       "handler": todo_list_handler,       "deps": deps},
        {"spec": TODO_GET_SPEC,        "handler": todo_get_handler,        "deps": deps},
        {"spec": TODO_CREATE_SPEC,     "handler": todo_create_handler,     "deps": deps},
        {"spec": TODO_UPDATE_SPEC,     "handler": todo_update_handler,     "deps": deps},
        {"spec": TODO_DELETE_SPEC,     "handler": todo_delete_handler,     "deps": deps},
        {"spec": TODO_RESOLVE_SPEC,    "handler": todo_resolve_handler,    "deps": deps},
        {"spec": TODO_VALIDATE_SPEC,   "handler": todo_validate_handler,   "deps": deps},
    ]
```

CLI 模式(无 REPL)由 `cc_harness/cli/_shared.py` 生成一次性 session_id:`cli-{int(time.time())}-{uuid4().hex[:8]}`。

**handler 返回文本格式**(LLM 可见):

```
# todo_create 成功
[todo_create] ✓ created task xyz-789
title:    添加单元测试
status:   pending
id:       xyz-789

# todo_list(默认 limit=20,按 status 优先级 + updated_at desc)
[todo_list] 6 tasks (2 done / 1 in_progress / 3 pending)
✓ abc-123  完成 hello.py                          [done]
✓ def-456  设计新 API                              [done]
⠋ jkl-012  实现 todo 持久化          [high]   [in_progress]
○ mno-345  添加单元测试              [medium]     [pending]
○ pqr-678  集成 Locomo eval                         [pending]
○ stu-901  写文档                                   [pending]
# (limit=20 后省略更早任务,LLM 用 status filter 再取)

# todo_resolve(target=jkl-012,BFS 传递依赖,indented)
[todo_resolve] jkl-012 chain (3 tasks)
✓ abc-123  完成 hello.py                  [done]    ← depth 2
✓ def-456  设计新 API                       [done]    ← depth 1
⠋ jkl-012  实现 todo 持久化    [high]   [in_progress]  ← target

Ready to work: all upstream done.

# todo_validate
[todo_validate] ✓ all 6 tasks valid
# 或:
[todo_validate] ✗ 2 issues found:
  - [error] jkl-012: missing_dependency 'abc-123' (refers to non-existent task)
  - [error] mno-345: cycle 'mno-345 -> pqr-678 -> mno-345'
Fix with: cc-harness todo update <id> --depends-on ...
```

**错误消息格式**(LLM 友好):
```
[todo_create] ✗ DependencyCycleError: would create cycle
  jkl-012 -> xyz-789 -> jkl-012
  Remove one edge.
```

**todo_list 默认 limit**:20(防爆 context window)。LLM 想看更多 → 显式传 `limit=N` 或 `status=` 过滤。LLM 想看 done → 用 `todo_get` 单取或 `status=done` 过滤。

### 组件 8:CLI 命令(`cc_harness/cli/`)

| 命令 | 用途 |
|---|---|
| `cc-harness init` | 创建 `.cc-harness/project.yaml` |
| `cc-harness todo list [filters]` | 列出任务 |
| `cc-harness todo get <id>` | 单个详情 |
| `cc-harness todo create [flags]` | 创建(支持 13 字段 flags) |
| `cc-harness todo update <id> [flags]` | 更新 |
| `cc-harness todo delete <id> [--force]` | 删除 |
| `cc-harness todo resolve <id>` | 依赖链 |
| `cc-harness todo validate` | 全表校验 |
| `cc-harness --resume [--resume-id <id>]` | 显式 resume |

**TTY vs pipe**:
- TTY → Rich 彩色 + 表格
- pipe → 纯文本(默认)或 JSON(`--json`)

**退出码**:成功 0 / 业务错 1 / 系统错 2

### 组件 9:REPL 集成(`cc_harness/repl.py`)

修改点:

1. **启动时检测 manifest** —— **Breaking change**:`python main.py` 现要求 `.cc-harness/project.yaml` 存在。详见"风险与缓解"章节的迁移计划。

   ```python
   from cc_harness.project.manifest import load_manifest
   manifest = load_manifest(cwd)
   if manifest is None:
       print_info(console, "No .cc-harness/project.yaml found. Run: cc-harness init")
       sys.exit(1)
   ```

2. **加载 TodoService + Live**:
   ```python
   todo_service = TodoService(project_root=Path(cwd), manifest=manifest)
   live_panel = TodoLivePanel(console, todo_service, manifest)
   live_panel.start()
   ```

3. **注入 todo tools**(同 memory 模式,但 `extras` 拼接需 None-safe):
   ```python
   from cc_harness.project.extras import inject_todo_tools
   state.todo_extras = inject_todo_tools(todo_service, session_id=state.session_id)
   # extra_native_specs 必须 None-safe(plan/design 模式时 None)
   _all_extras = list(state.memory_extras or []) + list(state.todo_extras or [])
   extra_native_specs=_all_extras or None
   ```

4. **Resume 注入走 SECTION_POOL**,**不** append 到 messages:
   ```python
   if state.session_stats.turns == 0 and manifest.resume_mode == "ask":
       in_progress = _select_resume_task(tasks)   # 见下面策略
       if in_progress:
           state.resume_task = in_progress         # ReplState 新增字段
   # agent._refresh_system_prompt(messages, cwd, mode) 检测 state.resume_task,
   # 追加 SECTION_POOL 一段(满足 mode==coding and resume_task is not None 条件),
   # 自然 rebuild 不会污染 messages 末尾
   ```

   **`_select_resume_task` 规则**(决定性):
   ```python
   def _select_resume_task(tasks: list[TodoTask]) -> TodoTask | None:
       """选择规则:updated_at desc 最新的 in_progress task。
       若 0 个 in_progress → 返回 None(resume prompt 跳过,直接进 REPL)。
       若多个 in_progress → 取 updated_at 最新(假设最近动的那个最相关)。
       """
       in_progress = [t for t in tasks if t.status == "in_progress"]
       if not in_progress:
           return None
       return max(in_progress, key=lambda t: t.updated_at)
   ```

5. **After-turn 钩子**(暂不实现 L2 scenario 自动覆盖):
   ```python
   async def _after_turn_todo(state: ReplState, todo_service: TodoService) -> None:
       """暂为空函数,B 阶段填 verify hook"""
       pass
   ```

6. **mode 适配**:plan / design mode 下 `tool_specs=None` 已由现有 agent.py 处理(物理屏蔽 tool_calls);但 todo_create 在 plan mode 是合法的("设计阶段也要记 todo")。具体:plan/design 模式下 `state.todo_extras` 仍注入,但 LLM 看不到(因为 tool_specs=None),**用户**可通过 CLI 创建。A 不特殊处理。

### 组件 10:Memory 集成(`cc_harness/project/memory_bridge.py`)

**默认:互不写入**(可选 opt-in `completion_capture`)。

```python
async def on_task_completion(task: TodoTask, manifest: Manifest, memory_service: MemoryService | None) -> None:
    if not manifest.memory.integration.completion_capture:
        return
    if memory_service is None:
        return
    text = f"[task done] {task.id}: {task.title}"
    session_id = task.active_sessions[-1] if task.active_sessions else None
    await memory_service.save(text, source="todo/completion", session_id=session_id)
```

## 触发参数

### manifest 字段(`project.yaml`)

```yaml
schema_version: 1
resume_mode: ask          # ask | auto | manual
memory:
  integration:
    completion_capture: false  # opt-in
live:
  position: top
  max_height: 10
  spinner_style: dots
  show_progress_bar: true
  fold_done: 5
```

### 环境变量

| 变量 | 用途 | 来源 |
|---|---|---|
| `CC_HARNESS_SESSION_ID` | 跨工具传递 session_id | REPL 启动时设入 |

## 文件清单(新增)

```
.cc-harness/                                  # 项目本地(不提交除非用户主动)
├── project.yaml                              # manifest
└── todos/
    ├── todos.yaml                            # 主索引
    └── <id>.md                               # 每任务 md

cc_harness/
├── project/
│   ├── __init__.py
│   ├── manifest.py                           # Manifest load/save/validate
│   ├── models.py                             # TodoTask / ValidationIssue / Manifest dataclass
│   ├── service.py                            # TodoService (单源)
│   ├── storage.py                            # yaml + md 读写
│   ├── status.py                             # 状态守卫
│   ├── dependency.py                         # 依赖校验 + topo
│   ├── live.py                               # TodoLivePanel
│   ├── tools.py                              # 7 个 tool specs + handlers
│   ├── extras.py                             # inject_todo_tools
│   └── memory_bridge.py                      # opt-in completion_capture
└── cli/
    ├── __init__.py
    ├── init.py                               # cc-harness init
    ├── todo.py                               # cc-harness todo <subcmd>
    ├── resume.py                             # cc-harness --resume
    └── _shared.py                            # arg parsing + 输出 helper

tests/
├── test_project_manifest.py
├── test_project_models.py
├── test_project_storage.py
├── test_project_status.py                    # 状态守卫 100% 覆盖
├── test_project_dependency.py                # 环检测 100% 覆盖
├── test_project_service.py
├── test_project_live.py                      # 只测 _render
├── test_project_tools.py
├── test_project_extras.py
├── test_project_memory_bridge.py
├── test_cli_init.py
├── test_cli_todo.py
├── test_cli_resume.py
├── test_cli_exit_codes.py
├── test_project_resume.py
├── test_project_repl_integration.py
├── conftest.py                               # 共享 fixture
└── fixtures/
    ├── project_minimal/
    │   └── .cc-harness/
    │       ├── project.yaml
    │       └── todos/todos.yaml
    ├── project_with_tasks/
    │   └── .cc-harness/...
    └── project_invalid/
        └── .cc-harness/project.yaml

docs/superpowers/specs/
└── 2026-07-14-long-horizon-task-tracking-design.md   # 本 spec

docs/superpowers/plans/
└── 2026-07-14-long-horizon-task-tracking-plan.md     # writing-plans 阶段产出
```

## 测试策略

### 分层

| 层 | 文件 | LLM | 收集 |
|---|---|---|---|
| 单元 | `tests/test_project_*.py` | 否 | pytest 默认 |
| 集成 | 同上目录 | 否 | pytest 默认 |
| E2E | `tests/_test_project_*.py` | **是** | 默认跳过,手动 |

### 关键边界 case(必须覆盖)

| case | 期望 |
|---|---|
| 并发两个 update 同一 task | 后写赢(LWW),不引入锁 |
| todo_update 给未注册字段 | 静默忽略 + warn |
| session_id 含特殊字符 | 自动 sanitize 为 `[a-z0-9-]` |
| project_id 试图改 | 拒绝,必须 `--force-reinit` |
| 已 done 任务 status 改 in_progress | StatusGuardError |
| depends_on 含自身 | DependencyCycleError |
| md 文件外部编辑改 | 下次 TodoService 读合并回 yaml |
| Live 启动时无任务 | "📋 no tasks yet" |
| 1000 任务全表 validate | <100ms |
| 1000 任务 Live 渲染 | <50ms/帧 |

### 覆盖率目标

| 模块 | 目标 |
|---|---|
| `status.py` | **100%** |
| `dependency.py` | **100%** |
| 其他 project/ 模块 | ≥ 80% |
| `cli/` | ≥ 75% |
| **整体 project/ 包** | **≥ 85%** |

## 接线点(integration points)

### 与现有代码的集成

| 接线点 | 改动 |
|---|---|
| `cc_harness/repl.py:run_repl` | 启动检测 manifest + 加载 TodoService + 启动 Live + 询问 resume |
| `cc_harness/agent.py:run_turn` | `extra_native_specs` 接收 todo tools(已支持) |
| `cc_harness/main.py` | argparse 接受 init/todo/--resume(CLI 模式) |
| `cc_harness/memory/extras.py` | **不动**,A 是独立 module |
| `cc_harness/policy.py` | **不动**,todo 操作走自己 service |

### 与未来 B/C 阶段的接口预留

| B/C 需求 | A 预留字段 |
|---|---|
| B:DAG 调度器 | `depends_on` 字段 + `topo_sort()` 函数 |
| C:HTN 嵌套 | `parent_task` 字段 |
| C:checkpoint 自检 | `acceptance_criteria` 字段 |
| 其二:SubAgent 派活 | `assigned_to` 字段 |

## Out of scope(明确不做)

- ❌ 外层 plan-execute-verify-replan loop(Sub-project B)
- ❌ 手动目标拆解 / HTN 规划器(Sub-project C)
- ❌ Checkpoint 自检 + 失败 replan(Sub-project C)
- ❌ SubAgent / Agent Team(引擎其二)
- ❌ Self-Play / 数据工厂 / 自改代码(引擎其三,用户暂不管)
- ❌ Todo 双向同步到 L1(避免污染,只做 opt-in completion_capture)
- ❌ L2 scenario 自动覆盖 todo
- ❌ L3 persona 驱动 priority
- ❌ Acceptance criteria 自动从 memory 抽取
- ❌ Todo 操作走 policy 闸门(A 阶段 todo 是用户/agent 自管,不走 L4)
- ❌ 实时多 session 协作(每个 REPL session 独立读写,通过 git/fs 自然同步)

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| **Breaking change:`python main.py` 现要求 project.yaml** | 详见下方"迁移计划"段 |
| 并发写同一 task 导致数据竞争 | LWW(后写赢)+ git 自然冲突检测 + validate() 给清晰错误 |
| md 文件外部编辑改导致 yaml 索引漂移 | TodoService.update 自动重写 yaml 索引;`validate()` 报"yaml 引用但 md 缺失" / "md 文件无 yaml 引用"两类 issue(详见组件 5 合并策略) |
| Live 组件与 Rich 现有 REPL 输出冲突 | **方案 B**:Live 占顶部 N 行 + `_read_user` 期间 stop/start + `panel.refresh()` 显式触发。`_print_disk_changes` 也走 Live 下方 buffer,不重叠。单元测覆盖 _render 隔离。 |
| agent 滥用 todo_create 大量创建任务 | 不在 A 范围硬限;LLM 用 token 成本自约束;B 阶段加 DAG 拓扑约束 |
| session_id 漂移 | **显式注入**(TodoService constructor + handler deps),不靠 env var;CLI 模式生成 `cli-{ts}-{hex}` |
| yaml 损坏/格式错误 | Storage 层 try/except → 启动报错 + 提示修复,绝不静默 fallback 到空表 |
| .gitignore 默认行为 | init 时询问用户(默认开);git repo 写 `.gitignore` 排除 `.cc-harness/todos/*.md`(只 track `project.yaml`);**非 git repo → skip .gitignore 步骤**(一次性 sandbox / sub-project) |
| active_sessions 无界增长 | 自动 prune:超过 50 条 → 截断为最近 50 + 注释行 |
| terminal resize | **A 阶段不处理**(Windows 无 SIGWINCH);spec 范围内不实现,B 阶段按需 |
| manifest schema_version > 已知 | `ManifestError` fail-closed,提示升级 cc-harness(不静默降级) |
| 未知 manifest 字段 | warn log 不报错(`extra='ignore'`),允许手编加 `experimental: true` 类字段 |

### 迁移计划(Breaking change)

**影响面**:
- `eval/locomo/runner.py`(eval 入口)
- `run_verify.py`(Phase-1 回归)
- `eval/promptfoo/wrappers/cc_harness.py`(红队 wrapper)
- 现有用户 clone 仓库后直接跑 `python main.py` 会失败

**迁移方案**:
1. **eval 路径**:在每个 eval 入口脚本(`runner.py`、`run_verify.py`、`cc_harness.py`)开头加 2 行:
   ```python
   from pathlib import Path
   _manifest = Path(".cc-harness/project.yaml")
   if not _manifest.exists():
       from cc_harness.cli.init import init_noninteractive
       init_noninteractive(Path.cwd(), name=Path.cwd().name)
   ```
2. **新用户友好**:`python main.py` 缺 manifest 时,**不 sys.exit**,而是自动调 `init_noninteractive`(默认 name=目录名,resume_mode=ask,启用 Live)→ 创建 manifest → 继续启动 REPL。这样新用户零摩擦。
3. **显式 init 仍支持**:`cc-harness init`(交互模式)用于想自定义的。
4. **CI / sandbox**:用 `--no-live --no-resume` flag 跳交互环节(spec 包含此 flag)。
5. **旧 `python main.py` 行为**变更为"自动 init + 进 REPL",对外黑盒;所有现有测试需更新但不需要改业务逻辑。

**backout**:若 breaking change 影响太大,fallback 是 `manifest = load_manifest(cwd) or Manifest.default_ephemeral()`(adhoc 模式,只读 todos 不持久),允许 REPL 在无 manifest 时运行。**A 阶段倾向于"自动 init"** 方案,backout 留作 escape hatch。

## 实施优先级(供 writing-plans 阶段参考)

A 阶段拆解为以下 6 个实现 task(按数据/逻辑/集成层次),由 writing-plans 阶段细化:

1. **manifest + models + storage** — 数据层基础
2. **status + dependency** — 校验逻辑(100% 覆盖)
3. **TodoService** — 单一真相源 + 事件订阅
4. **7 个 tool specs** — agent tool 通道
5. **CLI 命令** — 9 个子命令
6. **Live 组件 + REPL 接线 + resume 询问** — UI 集成

依赖链:1 → 2 → 3 → {4, 5, 6 并行} → 集成测试。

## 开放问题(写作 plan 时必须答)

1. **init 行为最终选 "自动 init" 还是 "adhoc fallback"?** —— spec 推荐自动 init,plan 阶段评估实现成本与 breaking change 风险。
2. **`cc-harness init` 二次执行是否提供 "merge" 选项**(保留旧配置、只补缺失字段)?——影响 init 命令复杂度。
3. **`--resume-id <id>` flag 是否允许非交互模式?** —— 影响 CLI 解析(若允许,需要在 `cc-harness --resume` 后接可选 id)。
4. **todo_update 触发 status=done 时,是否联动 `completion_capture`?** —— 还是只在 `validate()` 时扫?spec 默认"完成即触发 capture"(已勾上)。
5. **并发 session 写入同一 yaml 的 conflict 解决?** —— LWW 在 yaml+md 双写下需具体到哪个文件,要不要 file lock(简单但慢)/ git 冲突(自然但需要 user 介入)?
6. **Live panel 的 stderr/stdout 路由?** —— REPL 主用 stdout tty;4-phase 输出也走 stdout;Live 区域与下方输出怎么严格划分(靠 refresh 区间?靠 Console.file 分流?)?
7. **plan/design mode 下 todo_create 是否允许?** —— spec 默认"工具不注入到 plan/design(物理屏蔽),但 CLI 可手动创建",需 plan 阶段确认无歧义。
8. **session_id 长度限制?** —— 现在用 `repl-{int(time.time())}` (12 字符);要不要加 nanosecond 或 hex 后缀避免同秒重复?