# 其一·长程任务 — Sub-project C:HTN 树(聚合语义)+ Checkpoint 软完成门设计

> **范围**:cc-harness AI 工程目标"其一·长程任务" 5 红 + 3 黄中的 **C 子集**——HTN 嵌套数据层(parent/child 树 + 聚合完成语义)+ Checkpoint 软完成门(`todo_update(status="done")` 前 acceptance + 聚合校验,tool 层软拦 + force 绕过)。
>
> **不**实现"自动 HTN 规划器"(给大目标自动递归拆 task 树)——那是"外层 plan-execute loop"的一部分,Sub-project B 已明确不做,C 也不做。C 只做**手动**拆解的数据层 + 完成门。
>
> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。

## ⚠ 关键事实纠正(spec review round 1 CRITICAL)

**`todo_resolve` 不是"标完成"动作**。它是 A 阶段落的**只读依赖链解析器**(`service.resolve` = BFS 上游传递依赖,返回 `list[TodoTask]`,不 mutate state;见 `service.py:112-155`、A spec line 429)。

**真正的 mark-done 路径**:`todo_update(status="done")` → `todo_update_handler` → `service.update()` → `status_guard()` → 持久化 → `_on_completion` hook(`service.py:281-378`)。

因此 C 的完成门**挂在 `todo_update_handler`**(当 `status` 字段被设为 `"done"` 且 task 当前非 done 时触发),**不挂 `todo_resolve`**。本 spec 早期草稿误挂 resolve,已全文纠正。

## Goal

承接 Sub-project A 已经落地的 `parent_task` 字段(只挂字段 + 引用校验,无完成语义)和 `acceptance_criteria` 字段(A 挂字段,B 的 `_after_turn_todo` 已跑 `run_verify` 写软 hints),C 阶段补齐两件最小底座:

1. **HTN 树聚合语义** —— `parent_task` 不再只是分组标签:parent 标 done 必须所有直接 children 已 done(聚合)。配套 `children_all_done` 纯函数 + `todo_toposort` 的 tree 视图。
2. **Checkpoint 软完成门** —— `todo_update_handler` 层软拦:当 `status→"done"` 转换时跑两道校验(① children 聚合 ② acceptance verify),任一不过返回 `is_error` 不执行 update;`force=True` 可绕过 acceptance(启发式可误判),**但绕不过聚合**(数据一致性)。

**C 阶段不做**:自动 HTN 规划器(LLM 自动递归拆 task 树)、parent 环前置检测(只渲染兜底)、完成自动级联(children 全 done 自动标 parent)、跨轮文本聚合、`todo_complete` 新 tool(完成门挂现有 `todo_update`,不新开 tool)。

## 设计前提(重要)

跟 B 一脉相承的最小底座哲学:

- C 不假设 subagent(引擎其二)已落地。HTN 树 + 完成门在主 agent 单 turn 模型下也独立可用。
- 主 agent 自己决定拆不拆任务(手动调 `todo_create` 挂 `parent_task`)、自己决定何时完成,C 只在 `todo_update(status="done")` 这个**动作点**加校验门。
- 完成门是**软**拦:不通过返回 error + 原因,agent 能理解后补齐重试,或 `force=True` 明确承认风险绕过(仅 acceptance,不绕聚合)。**绝不**做成 service 层 status_guard 抛异常的硬拦(会把 agent 弄哑:acceptance 启发式误判时 agent 推不动)。
- B 的 `_after_turn_todo` 软提示 hook **不动**,与 C 的 tool 软拦互补:hook 在每 turn 后提醒"你 criterion 没体现",tool 在完成动作点兜底"没过我拦住"。两层防御,职责不重叠。

## 现有代码事实(spec 写入时核实)

| 文件 | 现状 | C 处置 |
|---|---|---|
| `cc_harness/project/models.py` | A 已落 `parent_task: str \| None` + `acceptance_criteria: list[str]` 字段 | **不动** |
| `cc_harness/project/dependency.py` | A 已落 `check_references` / `check_no_cycle`(查 depends_on 环)/ `dep_check`;B 已落 `topo_sort` / `get_ready_tasks` | **新增** `children_all_done(tasks, parent_id) -> tuple[bool, list[str]]` 纯函数(与 `get_ready_tasks` 一族) |
| `cc_harness/project/verify.py` | B 已落 `VerifyResult` / `heuristic_check` / `state_check` / `run_verify` | **不动**(C 的完成门复用 `run_verify`) |
| `cc_harness/project/service.py` | A 已落 `list(parent_task=...)` / `update`(mark-done 路径,含 status_guard + `_on_completion`)/ `resolve`(**只读**依赖链查看器,非 mark-done) | **不动**(C 的完成门校验全在 tool 层,不污染 service 纯状态机) |
| `cc_harness/project/tools.py` | A 落 7 tool(含 `todo_update_handler:497` / `todo_resolve_handler:563`);B 落第 8 个 `todo_toposort`(flat 视图 + group 过滤) | **改 2 处**:① `todo_update_handler` 加完成门(仅 status→done 转换触发)+ force 参数 ② `todo_toposort` 加 `view=flat\|tree` 参数 + tree 渲染。**`todo_resolve` 不动**(它是只读查看器,与完成门无关) |
| `cc_harness/project/extras.py` | B 已落 `inject_todo_tools` 返回 8 entry,deps = `{service, session_id, cwd}` | **改 deps**:加 `last_turn_text` 字段(handler 跑 acceptance verify 要用) |
| `cc_harness/agent.py` | dispatch:`h_kwargs = {"cwd": ..., **deps}` 统一 splat(`agent.py:247`)→ **deps 加 key,所有 8 个 handler 签名都要兼容**;B 已落 `<todo_hints>` 段注入 | 完成 handler 签名兼容(见开放问题 #1)+ `_refresh_system_prompt` 加 `<todo_resolve_gate>` 静态提示 |
| `cc_harness/repl.py` | B 已落 `ReplState.last_turn_text` + `_extract_final_text` + run_turn 调用点传 `todo_hints` | **改 1 处**:`inject_todo_tools` 调用点传 `last_turn_text=state.last_turn_text` |
| `tests/test_*` | A/B 已落 ~100 测试 | **新增** `test_c_integration.py` + `_test_c_e2e.py` + 各组件单元测试 |

**baseline 核实**:当前 `full-eval` HEAD `pytest --collect-only` = **1116 tests**(spec review round 1 实测)。C 的 commit message baseline 锚定 **1116**,不是 B final reviewer 报的 1105(那是我之前 narrow-scope 跑漏了)。

## 关键决策(brainstorm 确认)

### decision 1:聚合语义(非纯组织,非自动级联)

`parent_task` 标 done **必须**所有直接 children 已 done,否则 C 的 `todo_update(status="done")` 软拦挡住。但**不自动级联**:children 全 done 时不会自动把 parent 标 done,agent 必须主动 update parent status=done(系统校验聚合)。

理由:
- 纯组织(父子完成独立)→ HTN 是摆设,做没做没区别
- 自动级联 → agent 中途想给 parent 加新子任务会被"已自动 done"打断,且隐藏了 agent 的主动决策
- 聚合 + 手动标 parent → 符合"大任务 = 子任务总和"直觉,且保留 agent 主动性

**聚合只看直接 children(一层),不递归孙**:孙的聚合由孙自己的 `todo_update(status="done")` 把关。深层 HTN(parent→child→grandchild)正确闭合靠每层各自把关。

**子任务不自动 depend_on parent**(那会死锁:父要子全 done 才 done,子又要父 done 才能开始)。子之间串行靠现有的 `depends_on`。`parent_task` 表达"组成关系",`depends_on` 表达"先后关系",两者正交。

### decision 2:完成门 = `todo_update_handler` tool 层软拦 + force 绕过(非硬拦)

**完成动作 = `todo_update(status="done")`**(不是 `todo_resolve`)。`todo_update_handler` 在 `service.update` 调用**之前**,若 `fields["status"]=="done"` 且 task 当前非 done,跑两道校验,任一不过返回 `is_error=True` 不执行 update:
1. **聚合**:`children_all_done(by_id, task_id)` → 有 pending children → error(列 pending children)
2. **acceptance**:criteria 非空且 `run_verify` 不过 → error(列 missing_criteria)

`force=True`(todo_update 新增参数,仅 status=done 时有意义)跳过 acceptance(启发式可误判,该让 agent 绕),**但跳不过聚合**(数据一致性,非启发式,不该绕)。

**为什么不挂 service.update / 不新开 todo_complete**:
- service 层硬拦(status_guard 抛异常)→ 太死,acceptance 误判时 agent 无路,且 service 层掺了 verify 启发式,污染纯状态机
- 新开 `todo_complete` tool → 语义干净但**可被 `todo_update(status="done")` 绕过**(agent 不调 complete 直接 update 就逃 gate),gate 形同虚设
- 挂 `todo_update_handler` → 堵住 agent 唯一的 mark-done 路径,最 robust;程序化批量导入走 `service.update`(绕过 tool handler)不受 gate 影响(合理:导入不是 agent"完成"动作)

**批量/纠正场景**:agent 用 `todo_update(status="done")` 重同步/纠正也过 gate(一致性 —— 既然有 gate 就该一致)。真要绕过传 `force=True`。程序化批量导入直接调 `service.update`,绕过 handler,不过 agent gate。

### decision 3:不做自动 HTN 规划器

agent 收大目标**自动**递归拆成完整 task 树(类似 Claude Code TodoWrite 自动拆)——**不做**。这本质是"外层 plan-execute loop"的一部分,B 已明确不做。C 延续 YAGNI:agent 自己手动调 `todo_create` 挂 parent 拆任务。

### decision 4:verify 继承 B 单轮 last_turn_text,deps 注入

完成门的 acceptance 校验复用 B 的 `run_verify`,文本源用 `state.last_turn_text`(单轮,继承 B)。`todo_update_handler` 现签名 `(args, *, service, session_id, cwd)` 拿不到 state,**通过 `inject_todo_tools` 的 deps 注入** `last_turn_text`。

**已知限制**(YAGNI 接受,见开放问题 #1 的升级路径):agent 在 turn N 做完事 + 同一轮调 `todo_update(status="done")` 时,`last_turn_text` 还是 turn N-1 的(滞后一轮,因 dispatch 时 turn N 未结束、`state.last_turn_text` 尚未更新)。典型模式(做事 → 下轮 update done)下是对的;同轮完成的滞后靠 `force=True` 兜底。不做跨轮文本聚合(复杂,且软拦可绕)。

### decision 5:parent 环只渲染兜底,不前置检测

A 阶段 `check_no_cycle` 只查 `depends_on` 环,没查 `parent_task` 环(A→B→A parent 环理论上能跨两次 update 构造)。C **不做前置检测**(create/update 时拦会改 A baseline 流程,风险高),只做**渲染兜底**:`_render_toposort` 的 tree 视图用 visited set 防无限递归,遇已访问 node 截断 + 标 ⚠ 环。零 baseline 风险。

### decision 6:tree 作为 `todo_toposort` 的 view 参数(不新开 tool)

`todo_toposort` 加 `view` 参数:`flat`(现状)/ `tree`(HTN 缩进树)。toposort 本就是"看全局结构",tree 是它的自然扩展。不新开 `todo_tree` tool(YAGNI,tool 数已够多)。

### decision 7:`children_all_done` 放 `dependency.py`(不放 verify.py)

`children_all_done` 是纯状态查询(查 children 的 status),不是启发式判断。跟 `get_ready_tasks` / `topo_sort` 一族,放 `dependency.py`。`verify.py` 留给启发式(heuristic_check)。

## 组件设计

### 组件 1:`children_all_done`(dependency.py 纯函数)

```python
def children_all_done(
    tasks: dict[str, TodoTask], parent_id: str
) -> tuple[bool, list[str]]:
    """parent 的所有直接 children 是否全 done。

    Returns:
        (all_done, pending_child_ids)
        - 无 children → (True, [])
        - children 引用不存在(缺失,理论上 create/update 已防)→ 容错跳过,不阻塞
        - 返回的 pending_child_ids 按 task.id 字典序(确定性)
    """
```

纯函数,传 tasks dict(与 `get_ready_tasks` 一致签名),不耦合 service。只看**直接** children(一层)。

### 组件 2:`todo_update_handler` 完成门(tools.py 改 handler)

`todo_update_handler`(`tools.py:497`)在 `service.update` 调用前插入完成门:

```python
async def todo_update_handler(
    args: dict, *, service, session_id: str, cwd: str,
    last_turn_text: str = "",          # C 阶段:deps 注入,完成门 acceptance 用
) -> ToolResult:
    """todo_update:任意 T11 字段 → Service.update。session_id 显式传。
    C 阶段:status→done 转换时跑完成门(聚合 + acceptance),force 可绕 acceptance。"""
    del cwd
    task_id = args.get("task_id")
    if not task_id:
        return ToolResult.error(display="task_id is required", llm="...")

    fields = _extract_update_fields(args)   # 现有字段提取逻辑不变
    force = bool(args.get("force", False))  # C 新增:仅 status=done 时有意义

    # --- C 阶段完成门:仅当本次要把 status 设为 done ---
    if fields.get("status") == "done":
        gate = await _completion_gate(
            service, task_id, force, last_turn_text
        )
        if gate is not None:                # not None = 被拦,返回 error
            return gate

    try:
        updated = await service.update(task_id, session_id=session_id, **fields)
    except TodoError as e:
        return _err("todo_update", e)
    # ... 现有成功反馈 ...
```

**`_completion_gate` 辅助函数**(tools.py 模块级):

```python
async def _completion_gate(
    service, task_id: str, force: bool, last_turn_text: str
) -> ToolResult | None:
    """完成门:返回 None=放行,ToolResult(is_error=True)= 拦截。

    两道校验,任一不过收集到 errors:
      1. 聚合:children_all_done(force 也不跳)
      2. acceptance:criteria 非空 且 not force → run_verify
    service.list / run_verify 内部异常 → fail-soft(放行,不阻断 update),warn log。
    """
    try:
        all_tasks = await service.list(include_done=True)
    except Exception as e:
        log.warning("completion_gate: service.list failed: %s — fail-soft 放行", e)
        return None
    by_id = {t.id: t for t in all_tasks}
    if task_id not in by_id:                # 交给 service.update 报 TaskNotFound
        return None

    task = by_id[task_id]
    if task.status == "done":               # 已 done,重复设 done 不触发 gate
        return None

    errors: list[str] = []

    # 1. 聚合(force 也不跳 — 数据一致性)
    children_done, pending = children_all_done(by_id, task_id)
    if not children_done:
        errors.append(f"task {task_id} 有未完成子任务: {', '.join(pending)}")

    # 2. acceptance(criteria 非空 且 not force)
    if task.acceptance_criteria and not force:
        try:
            result = run_verify(task, by_id, last_turn_text)
        except Exception as e:
            log.warning("completion_gate: run_verify failed: %s — fail-soft 跳过 acceptance", e)
            result = None
        if result is not None and not result.passed:
            miss = "; ".join(result.missing_criteria)
            errors.append(f"task {task_id} acceptance 未满足: {miss}")

    if not errors:
        return None

    has_acceptance_err = any("acceptance" in e for e in errors)
    has_children_err = any("子任务" in e for e in errors)
    hint = ""
    if has_acceptance_err and not has_children_err:
        hint = "\n(可用 force=true 绕过 acceptance 校验;子任务聚合不可绕)"
    elif has_acceptance_err and has_children_err:
        hint = "\n(子任务聚合不可绕;补齐子任务后,acceptance 可用 force=true 绕过)"
    return ToolResult(
        is_error=True,
        display_text=f"todo_update blocked: {len(errors)} check(s) failed",
        llm_text="⚠ task 无法标完成:\n  - " + "\n  - ".join(errors) + hint,
    )
```

**spec 变更**:`TODO_UPDATE_SPEC` 的 parameters 加 `force: {"type": "boolean", "default": false}`,description 补"status=done 时触发完成门(聚合 + acceptance 校验),force=true 绕过 acceptance"。

### 组件 3:`todo_toposort` view=tree(tools.py 改)

`TODO_TOPOSORT_SPEC` parameters 加:
```python
"view": {
    "type": "string",
    "enum": ["flat", "tree"],
    "default": "flat",
    "description": "flat=现状拓扑+分组;tree=HTN 缩进树(parent/child 嵌套)",
},
```

`_render_toposort` 加 tree 分支:
```
HTN 树视图 (12 tasks):
  T1 [in_progress] "加 HTN feature"
    ├ T2 [done] "写 spec"
    ├ T3 [in_progress] "改代码"
    │   ├ T3a [done] "改 models"
    │   └ T3b [pending] "改 tools"
    └ T4 [pending] "写测试"
  T5 [pending] "另一顶层任务" (no parent)
```

- 顶层(parent=None)在最左,children 缩进 +2/层
- 同层 children 按 topo order 排
- **visited set 防环兜底**:遇已访问 node 截断 + `⚠ cycle: T_x`
- 截断 `MAX_RENDER_TASKS=50` 不变

### 组件 4:deps 注入 last_turn_text(extras.py + repl.py + handler 签名)

`inject_todo_tools` 的 deps:
```python
deps = {"service": service, "session_id": session_id, "cwd": cwd,
        "last_turn_text": last_turn_text}
```
新增 `last_turn_text: str = ""` 形参。`repl.py` 调用点传 `state.last_turn_text`。

**⚠ 接线约束(dispatch 统一 splat)**:`agent.py:247` 是 `h_kwargs = {"cwd": ..., **deps}`,**所有 8 个 handler 共享同一份 deps**。deps 加 `last_turn_text` 后,dispatch 会把它 splat 给**每一个** handler。现有 7 个 handler 签名是 `(args, *, service, session_id, cwd)` 硬编码 → 收到 `last_turn_text` kwarg 会 **TypeError**。

**plan 阶段必须锁死接线方式**(开放问题 #1),候选:
- (a) 所有 8 个 handler 加 `last_turn_text: str = ""` 形参(显式,推荐)
- (b) 所有 8 个 handler 加 `**kwargs` 兜底(隐式,但 ruff 可能报未用)
- (c) 只给 `todo_update_handler` 单独的 deps(需要改 dispatch 支持 per-entry deps merge,改动大,不推荐)

倾向 (a):8 个 handler 统一加 `last_turn_text: str = ""`,显式 > 隐式,与现有 `del cwd` 风格一致(未用的形参 `del` 掉)。

### 组件 5:system prompt 提示(agent.py)

`_refresh_system_prompt` 的 coding mode 段(与 `<todo_hints>` 并列)追加静态提示:
```
<todo_resolve_gate>
标 task 为 done(todo_update status=done)前,系统会校验:① 所有直接子任务(parent_task)已 done;② acceptance_criteria 在最近输出中体现。
- 子任务聚合校验不可绕过(数据一致性)。
- acceptance 校验可用 todo_update(status=done, force=true) 绕过(仅在确认启发式误判时)。
</todo_resolve_gate>
```

## 数据流

### 完成门(`todo_update status=done`)路径

```
agent 调 todo_update(task_id, status="done", force=false)
  ↓ handler:
  1. 提取 fields(现状)+ force
  2. fields["status"]=="done" → _completion_gate(service, task_id, force, last_turn_text):
     a. service.list → by_id;task 已 done → 放行(重复设 done 不触发)
     b. 聚合:children_all_done → pending → 收 error "未完成子任务: ..."
     c. acceptance(criteria 非空 且 not force):run_verify → not passed → 收 error "acceptance 未满足: ..."
     d. service.list / run_verify 异常 → fail-soft 放行(warn log)
     e. errors 非空 → is_error=True return(不 update)
  3. gate 放行(None)→ service.update(task_id, status="done", ...)→ status_guard + _on_completion(现状)
```

### tree 视图路径

```
agent 调 todo_toposort(group=all, view=tree)
  ↓ service.list(include_done=True) → 按 parent_task 构森林
  ↓ DFS 缩进渲染(visited 防环)→ 截断 50
```

### 与 B 的 `_after_turn_todo` 关系(不动)

```
turn N 结束:
  _after_turn_todo → run_verify(in_progress tasks) → 写 state.todo_hints(软提示)
turn N+1 开始:
  _refresh_system_prompt → 注入 <todo_hints>(B)+ <todo_resolve_gate>(C 静态提示)
turn N+1 中:
  agent 调 todo_update(status=done) → C 完成门(聚合 + acceptance + force)
```

B 软提示 + C 软拦,两层互补。

## 错误处理

| 情况 | 处理 |
|---|---|
| update status=done:children 未全 done | is_error,列 pending children,不 update(**force 也不跳**) |
| update status=done:acceptance 不过 + force=false | is_error,列 missing_criteria,不 update |
| update status=done:acceptance 不过 + force=true | 跳 acceptance,仍查聚合;聚合过则 update 成功,llm_text 回显"已绕过 acceptance 校验" + warn log |
| update status=done:两道都不过 | **两个都报**(一次性给全 error 列表,省 agent 来回) |
| update status=done:空 acceptance | 跳 acceptance 校验,只查聚合 |
| update status=done:无 children 的 task | 聚合 (True, []),直接放行到 acceptance 校验 |
| update status=done:task 已是 done(重复设) | 放行,不触发 gate(幂等) |
| update 其他字段(非 status=done) | **完全不触发 gate**(现状行为,force 参数被忽略) |
| gate 内 `service.list` 抛 | fail-soft 放行(warn log)—— 与 run_verify 异常一致,绝不因内部错误卡死 update |
| gate 内 `run_verify` 抛 | fail-soft 跳过 acceptance 校验(warn log),聚合校验仍跑 |
| tree 视图遇 parent 环 | visited set 截断 + 标 ⚠ cycle,不崩 |
| tree 视图孤儿(parent 已删,`dangling_parent`) | parent 找不到 → 当顶层处理 + 标注 |
| last_turn_text 为空(首轮 / deps 未注入) | handler 默认 `last_turn_text=""`,run_verify 空文本路径(passed=true + "无产出" hint,B 已定义),acceptance 校验放行 |

**fail-soft 一致性原则**:gate 内部任何异常(service.list / run_verify / children_all_done)→ 放行,不阻断 update。理由:宁可放过(软拦本就允许 force 绕过),不可把 agent 弄哑。与 B 的 `_after_turn_todo` swallow-and-warn 模式一致。

## 测试策略

### 单元测试(目标 100% line + branch)

**`dependency.py:children_all_done`**(新增,~5):
- 无 children → (True, [])
- children 全 done → (True, [])
- 部分 done → (False, [pending ids 字典序])
- children 引用缺失(容错)→ 跳过不阻塞
- 字典序确定性

**`tools.py:todo_update` 完成门**(改,~8):
- status=done + 聚合不过 → is_error 列 children
- status=done + acceptance 不过 + force=false → is_error 列 missing
- status=done + 两道都不过 → 两个 error 都报
- status=done + force=true 跳 acceptance(聚合过)→ update 成功 + warn
- status=done + force=true 不跳聚合(仍有 pending children)→ is_error
- status=done + 空 acceptance → 跳 acceptance,聚合过则 update
- status=done + 无 children + 空 acceptance → 直接 update
- **status 非 done 的 update(改 title 等)→ 完全不触发 gate**(回归保护)
- status=done 但 task 已 done(重复设)→ 放行不触发
- gate 内 service.list 抛 → fail-soft 放行
- gate 内 run_verify 抛 → fail-soft 跳 acceptance,聚合仍跑

**`tools.py:_render_toposort` tree 视图**(改,~6):
- 单层 children 缩进
- 多层嵌套(孙)
- 混合(有 parent 的 + 无 parent 的顶层)
- parent 环 visited 兜底不崩
- 截断 50
- `view=flat` 默认不破现状(回归)

### 集成测试(`tests/test_c_integration.py`,FakeLLM,~5)

- agent update status=done acceptance 未满足 → 收 error → 下轮补齐(criteria 命中)→ 再 update done 成功
- agent update parent status=done(children 没 done)→ 收 error 列 children → 完成 children → update parent done 成功
- agent force=true 绕过 acceptance update done 成功
- deps 注入 last_turn_text 接线(handler 能读到上轮文本)
- agent 拆任务(连续 todo_create 挂 parent)+ todo_toposort view=tree 看到树

### E2E(`tests/_test_c_e2e.py`,gated,1)

- 真 LLM:创建 parent + children,完成 children,update parent done,assert 聚合校验生效;或 acceptance 未满足时被拦

### 覆盖目标

| 模块 | 目标 |
|---|---|
| `cc_harness/project/dependency.py` 新增 `children_all_done` | 100% |
| `cc_harness/project/tools.py` `todo_update` 完成门 + tree 视图 | ≥85% |
| `cc_harness/project/extras.py` deps 接线 | 集成测试覆盖 |
| `cc_harness/agent.py` prompt 提示 + handler 签名兼容 | 单元覆盖关键路径 |
| `cc_harness/repl.py` deps 传参 | 集成测试覆盖 |

### 回归保护

- B 阶段 baseline **1116** 测试必须保住(collect-only 实测)
- B 阶段 `test_b_integration.py` + `_test_b_e2e.py` 仍过
- A 阶段 `test_project_*.py` / `test_repl_b_hook.py` 等仍过
- C 阶段预期新增 ~25 测试(19 单元 + 5 集成 + 1 E2E gated)

## 实施优先级(供 writing-plans 阶段参考)

按依赖链拆解:

1. **`children_all_done`**(dependency.py)— 无依赖,先写测试再填实现
2. **handler 签名兼容**(tools.py 全 8 个)— 为组件 4 deps 注入铺路,可与 1 并行
3. **`todo_update` 完成门 + force**(tools.py)— 依赖 1 + B 的 run_verify + 组件 4(deps 注入 last_turn_text)
4. **deps 注入 last_turn_text**(extras.py + repl.py)— 依赖 2(handler 签名) + 3(handler 用),与 3 合并提交
5. **`todo_toposort` view=tree**(tools.py)— 依赖 1(无需),可与 3 并行
6. **system prompt 提示**(agent.py)— 依赖 3,最后接
7. **集成测试 + E2E** — 全部就绪后跑

依赖链:1, 2(并行)→ 3 + 4(合并)→ 5(并行);3 → 6;全部 → 7

## 开放问题(plan 阶段核实)

1. **handler 签名兼容方式**(组件 4):dispatch 统一 splat deps,加 `last_turn_text` 后所有 8 handler 都要兼容。倾向 (a) 全加 `last_turn_text: str = ""` 形参。plan 阶段锁死,并验证 dispatch 对 `todo_update_handler` 之外的 handler 传多余 kwarg 不崩。
2. **last_turn_text 滞后一轮的升级路径**:当前用 `state.last_turn_text`(上一轮)。handler 跑在 turn 内,理论上可调研从当前 `messages` 提取已产出文本(`_extract_final_text(messages)`)注入 deps,消除滞后。但 messages 是 agent 内部状态,泄露给 tool handler 跨层。plan 阶段评估:若简单就做,若复杂留 follow-up(决策 4 的 YAGNI 默认)。
3. **`force=true` 时 llm_text 提示文案**:是否要更强提示"你正在绕过完成校验,确认 task 真的完成了吗"?当前设计回显"已绕过 acceptance 校验",plan 阶段可加强化措辞 + warn log 落审计。
4. **tree 视图与 flat 视图的 group 过滤组合**:`group=ready + view=tree` 怎么处理?当前设计 group 过滤 task 集合,view 决定渲染;两者正交。plan 阶段验证边界(group 过滤掉 parent 但留 child 时 tree 怎么显示)。

## Out of scope(明确不做)

- ❌ 自动 HTN 规划器(给大目标自动递归拆 task 树,= 外层 loop 的一部分)
- ❌ 完成自动级联(children 全 done 自动标 parent)
- ❌ service 层硬拦(status_guard 抛异常)
- ❌ `todo_complete` 新 tool(完成门挂现有 `todo_update`,堵 mark-done 唯一路径)
- ❌ parent 环前置检测(create/update 时拦,改 A baseline)
- ❌ 跨轮文本聚合(update done 时看整个 in_progress 期间所有产出)
- ❌ `todo_decompose` 新 tool(复用 `todo_create` 挂 parent)
- ❌ HTN 可视化 UI(Live panel / tree 视图 tool 已够)
- ❌ SubAgent / Agent Team(引擎其二,远期)
- ❌ LLM judge 替代 acceptance 启发式(复杂度翻倍,B 已砍)
- ❌ acceptance_criteria 自动抽取(A 已砍)
- ❌ parent_task 自动 depend_on 注入(会死锁,见 decision 1)
- ❌ 改 `todo_resolve` 语义(它是只读依赖链查看器,与完成门无关)

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| acceptance 启发式误判率高 → 软拦太烦 | force=true 绕过;真误判多 → 扩 stopword 或调 heuristic(B 阶段已留扩展点) |
| last_turn_text 滞后一轮(同轮 update done) | 典型模式(做事→下轮 update done)不受影响;同轮靠 force 兜底;不做跨轮聚合(YAGNI,见开放问题 #2) |
| deps 加 last_turn_text 破其他 7 个 handler 签名 | dispatch 统一 splat,**8 个 handler 全部加形参兼容**(组件 4 + 开放问题 #1),plan 验证 |
| 完成门误伤"非完成"的 update(改 title 等) | gate **仅** `fields["status"]=="done"` 时触发,其他 update 完全不过 gate(测试覆盖) |
| parent 环导致 tree 渲染无限递归 | visited set 兜底(零 baseline 风险) |
| 聚合校验只看一层,深层 HTN 漏孙 | YAGNI 起步一层;孙的聚合由孙自己 update done 把关 |
| force 被滥用(agent 养成无脑 force 习惯) | prompt 提示"仅在确认启发式误判时";warn log 审计 |
| C 的软拦与 B 的软提示职责重叠混淆 | B = 每 turn 后提醒(预防),C = update done 动作点兜底(强制);prompt 分别说明 |
| gate 内部异常卡死 update | fail-soft 原则:service.list / run_verify / children_all_done 任一异常 → 放行(warn log),与 B swallow-and-warn 一致 |
| C 阶段 commit 破坏 B 阶段 1116 baseline | 强约束:每个 task 跑 pytest,B baseline 必须保住 |

## 迁移计划

**影响面**:
- `cc_harness/project/dependency.py`(加 `children_all_done`)
- `cc_harness/project/tools.py`(改 `todo_update_handler` + `_render_toposort` + `TODO_UPDATE_SPEC` + `TODO_TOPOSORT_SPEC` + 全 8 handler 签名兼容)
- `cc_harness/project/extras.py`(deps 加 `last_turn_text`)
- `cc_harness/repl.py`(调用点传 `last_turn_text`)
- `cc_harness/agent.py`(`_refresh_system_prompt` 加 `<todo_resolve_gate>` 静态段)

**迁移方案**:
1. **依赖顺序**:1(children_all_done)+ 2(handler 签名兼容)→ 3+4(update 完成门 + deps)→ 5(tree 视图)→ 6(prompt)→ 7(集成)
2. **每步独立 commit**:`feat(dependency): children_all_done`、`feat(tools): handler 签名兼容 last_turn_text`、`feat(tools): todo_update 完成门 + force`、`feat(tools): todo_toposort view=tree`、`feat(repl/agent): last_turn_text deps + resolve_gate prompt`
3. **回退友好**:每个 commit 可单独 revert;C 功能可关闭(不传 force / view=flat / prompt 段空 / gate 仅 status=done 触发)
4. **B baseline 保护**:每个 commit 跑 `pytest tests/`,1116 baseline 必须保住

**backout**:
- `children_all_done` 删除 → update 完成门退化为只查 acceptance(或全跳)
- `view=tree` 删除 → toposort 回 flat(默认值,零影响)
- `<todo_resolve_gate>` prompt 段删除 → agent 不知道有门,但门仍在(tool 层)
- deps 不传 last_turn_text → handler 默认空字符串,acceptance 校验走空文本路径(放行)
- handler 签名回退(去掉 last_turn_text 形参)→ deps 也要同步去掉,否则 dispatch splat TypeError

**commit message 规范**:每个 C 阶段 commit 末尾必须显式报告:`baseline: 1116 passed → now: X passed (delta +N)`,确保 B 阶段 1116 是否保住在 commit message 一眼可见。

## 后续 plan(本 C 完成后另立)

- **Sub-project D(推测)**:SubAgent / Agent Team(引擎其二)—— 用 B 的 `get_ready_tasks` 做 fan-out,用 C 的聚合校验做 subagent result 验收入口,用 B 的 verify hook 做 subagent 产出 check。C 的 `parent_task` 树天然映射 subagent 任务分解。
- **跨轮文本聚合**:若 C 的单轮 last_turn_text 在实际使用中误拦率高,立小 spec 做"update done 时聚合 in_progress 期间所有 turn 文本"(开放问题 #2 的升级)。
- **parent 环前置检测**:若渲染兜底的 ⚠ cycle 频繁出现,立小 spec 在 create/update 加 parent 环检测。
