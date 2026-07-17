# 其一·长程任务 — Sub-project C:HTN 树(聚合语义)+ Checkpoint 软完成门设计

> **范围**:cc-harness AI 工程目标"其一·长程任务" 5 红 + 3 黄中的 **C 子集**——HTN 嵌套数据层(parent/child 树 + 聚合完成语义)+ Checkpoint 软完成门(resolve 前 acceptance + 聚合校验,tool 层软拦 + force 绕过)。
>
> **不**实现"自动 HTN 规划器"(给大目标自动递归拆 task 树)——那是"外层 plan-execute loop"的一部分,Sub-project B 已明确不做,C 也不做。C 只做**手动**拆解的数据层 + 完成门。
>
> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。

## Goal

承接 Sub-project A 已经落地的 `parent_task` 字段(只挂字段 + 引用校验,无完成语义)和 `acceptance_criteria` 字段(A 挂字段,B 的 `_after_turn_todo` 已跑 `run_verify` 写软 hints),C 阶段补齐两件最小底座:

1. **HTN 树聚合语义** —— `parent_task` 不再只是分组标签:parent 标 done 必须所有 children 已 done(聚合)。配套 `children_all_done` 纯函数 + `todo_toposort` 的 tree 视图。
2. **Checkpoint 软完成门** —— `todo_resolve` tool 层软拦:resolve 前跑两道校验(① children 聚合 ② acceptance verify),任一不过返回 `is_error` 不执行;`force=True` 可绕过 acceptance(启发式可误判),**但绕不过聚合**(数据一致性)。

**C 阶段不做**:自动 HTN 规划器(LLM 自动递归拆 task 树)、parent 环前置检测(只渲染兜底)、完成自动级联(children 全 done 自动标 parent)、跨轮文本聚合、`todo_decompose` 新 tool(复用 `todo_create` 挂 parent)。

## 设计前提(重要)

跟 B 一脉相承的最小底座哲学:

- C 不假设 subagent(引擎其二)已落地。HTN 树 + 完成门在主 agent 单 turn 模型下也独立可用。
- 主 agent 自己决定拆不拆任务(手动调 `todo_create` 挂 `parent_task`)、自己决定何时 resolve,C 只在 resolve 这个**动作点**加校验门。
- 完成门是**软**拦:不通过返回 error + 原因,agent 能理解后补齐重试,或 `force=True` 明确承认风险绕过(仅 acceptance,不绕聚合)。**绝不**做成 service 层 status_guard 抛异常的硬拦(会把 agent 弄哑:acceptance 启发式误判时 agent 推不动)。
- B 的 `_after_turn_todo` 软提示 hook **不动**,与 C 的 tool 软拦互补:hook 在每 turn 后提醒"你 criterion 没体现",tool 在 resolve 动作点兜底"没过我拦住"。两层防御,职责不重叠。

## 现有代码事实(spec 写入时核实)

| 文件 | 现状 | C 处置 |
|---|---|---|
| `cc_harness/project/models.py` | A 已落 `parent_task: str \| None` + `acceptance_criteria: list[str]` 字段 | **不动** |
| `cc_harness/project/dependency.py` | A 已落 `check_references` / `check_no_cycle`(查 depends_on 环)/ `dep_check`;B 已落 `topo_sort` / `get_ready_tasks` | **新增** `children_all_done(tasks, parent_id) -> tuple[bool, list[str]]` 纯函数(与 `get_ready_tasks` 一族) |
| `cc_harness/project/verify.py` | B 已落 `VerifyResult` / `heuristic_check` / `state_check` / `run_verify` | **不动**(C 的完成门复用 `run_verify`) |
| `cc_harness/project/service.py` | A 已落 `list(parent_task=...)` / `resolve` 等 7 ops | **不动**(C 的完成门校验全在 tool 层,不污染 service 纯状态机) |
| `cc_harness/project/tools.py` | A 落 7 tool;B 落第 8 个 `todo_toposort`(flat 视图 + group 过滤) | **改 2 处**:① `todo_resolve_handler` 加软拦(聚合 + acceptance + force)② `todo_toposort` 加 `view=flat\|tree` 参数 + tree 渲染 |
| `cc_harness/project/extras.py` | B 已落 `inject_todo_tools` 返回 8 entry,deps = `{service, session_id, cwd}` | **改 deps**:加 `last_turn_text` 字段(handler 跑 acceptance verify 要用) |
| `cc_harness/repl.py` | B 已落 `ReplState.last_turn_text` + `_extract_final_text` + run_turn 调用点传 `todo_hints` | **改 1 处**:`inject_todo_tools` 调用点传 `last_turn_text=state.last_turn_text`(或当前轮已抽取文本) |
| `cc_harness/agent.py:_refresh_system_prompt` | B 已落 `<todo_hints>` 段注入(coding mode gated) | **加提示**:coding mode system prompt 末尾追加"标 done 前会校验 acceptance + 子任务聚合;acceptance 可 force=True 绕过,聚合不可绕" |
| `tests/test_*` | A/B 已落 ~89 测试 | **新增** `test_c_integration.py` + `_test_c_e2e.py` + 各组件单元测试 |

## 关键决策(brainstorm 确认)

### decision 1:聚合语义(非纯组织,非自动级联)

`parent_task` 标 done **必须**所有 children 已 done,否则 C 的 `todo_resolve` 软拦挡住。但**不自动级联**:children 全 done 时不会自动把 parent 标 done,agent 必须主动 resolve parent(系统校验聚合)。

理由:
- 纯组织(父子完成独立)→ HTN 是摆设,做没做没区别
- 自动级联 → agent 中途想给 parent 加新子任务会被"已自动 done"打断,且隐藏了 agent 的主动决策
- 聚合 + 手动标 parent → 符合"大任务 = 子任务总和"直觉,且保留 agent 主动性

**子任务不自动 depend_on parent**(那会死锁:父要子全 done 才 done,子又要父 done 才能开始)。子之间串行靠现有的 `depends_on`。parent_task 表达"组成关系",depends_on 表达"先后关系",两者正交。

### decision 2:完成门 = tool 层软拦 + force 绕过(非硬拦)

`todo_resolve` handler 在 resolve 前跑两道校验,任一不过返回 `is_error=True` 不执行:
1. **聚合**:children 未全 done → error(列 pending children)
2. **acceptance**:criteria 非空且 `run_verify` 不过 → error(列 missing_criteria)

`force=True` 跳过 acceptance(启发式可误判,该让 agent 绕),**但跳不过聚合**(数据一致性,非启发式,不该绕)。

理由:
- 软提示升级(B 现状)→ 太软,agent 经常无视直接 resolve
- service 硬拦(status_guard 抛异常)→ 太死,acceptance 误判时 agent 无路,且 service 层掺了 verify 启发式
- tool 软拦 → 软硬之间:拦了,但 agent 收到结构化 error 能理解后补齐重试,真要绕过 force=True

### decision 3:不做自动 HTN 规划器

agent 收大目标**自动**递归拆成完整 task 树(类似 Claude Code TodoWrite 自动拆)——**不做**。这本质是"外层 plan-execute loop"的一部分,B 已明确不做(决策 #1 of B)。C 延续 YAGNI:agent 自己手动调 `todo_create` 挂 parent 拆任务。

### decision 4:verify 继承 B 单轮 last_turn_text,deps 注入

完成门的 acceptance 校验复用 B 的 `run_verify`,文本源用 `state.last_turn_text`(单轮,继承 B)。`todo_resolve` handler 现签名拿不到 state,**通过 `inject_todo_tools` 的 deps 注入** `last_turn_text`。

**已知限制**(YAGNI 接受):agent 在 turn N 做完事 + 同一轮调 resolve 时,`last_turn_text` 还是 turn N-1 的(滞后一轮)。典型模式(做事 → 下轮 resolve)下是对的;同轮 resolve 的滞后靠 `force=True` 兜底。不做跨轮文本聚合(复杂,且软拦可绕)。

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

纯函数,传 tasks dict(与 `get_ready_tasks` 一致签名),不耦合 service。只看**直接** children(一层),不递归孙(聚合语义只要求直接子 done;孙的聚合由孙自己的 resolve 把关)。

### 组件 2:`todo_resolve` tool 软拦(tools.py 改 handler)

```python
async def todo_resolve_handler(args, *, service, session_id, cwd, last_turn_text=""):
    task_id = args["task_id"]
    force = args.get("force", False)
    # ... 取 task(A 现状逻辑)...

    errors: list[str] = []

    # 1. 聚合校验(force 也不跳)
    all_tasks = await service.list(include_done=True)
    by_id = {t.id: t for t in all_tasks}
    children_done, pending = children_all_done(by_id, task_id)
    if not children_done:
        errors.append(f"task {task_id} 有未完成子任务: {', '.join(pending)}")

    # 2. acceptance 校验(仅 criteria 非空 且 force=False)
    if task.acceptance_criteria and not force:
        result = run_verify(task, by_id, last_turn_text)
        if not result.passed:
            miss = "; ".join(result.missing_criteria)
            errors.append(f"task {task_id} acceptance 未满足: {miss}")

    if errors:
        return ToolResult(
            is_error=True,
            display_text=f"resolve blocked: {len(errors)} check(s) failed",
            llm_text="⚠ task 无法标完成:\n  - " + "\n  - ".join(errors)
                + ("\n(可用 force=true 绕过 acceptance 校验;子任务聚合不可绕)" if any("acceptance" in e for e in errors and "子任务" not in e) else ""),
        )

    # 3. 两道都过(或 acceptance 被 force 跳)→ resolve
    if force and task.acceptance_criteria:
        log.warning("todo_resolve: force=True bypassed acceptance for %s", task_id)
    return await _orig_resolve(service, session_id, task_id)  # A 现状逻辑
```

**spec 变更**:`TODO_RESOLVE_SPEC` 的 parameters 加 `force: {"type": "boolean", "default": false}`,description 说明软拦语义。

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

### 组件 4:deps 注入 last_turn_text(extras.py + repl.py)

`inject_todo_tools` 的 deps:
```python
deps = {"service": service, "session_id": session_id, "cwd": cwd,
        "last_turn_text": last_turn_text}
```
新增 `last_turn_text: str = ""` 形参。`repl.py` 调用点传 `state.last_turn_text`。

**注意**:其他 7 个 tool handler 不用 `last_turn_text`,新增 kwarg 默认 `""` 不破现有签名(它们 `**kwargs` 或显式忽略)。需核实现有 handler 签名是否硬编码 kwargs —— 若硬编码 `(args, *, service, session_id, cwd)` 则新增 kwarg 会 break,需改成 `**kwargs` 兜底或同步加形参。**plan 阶段核实并锁死接线方式**。

### 组件 5:system prompt 提示(agent.py)

`_refresh_system_prompt` 的 coding mode 段(与 `<todo_hints>` 并列)追加静态提示:
```
<todo_resolve_gate>
标 task 为 done 前,系统会校验:① 所有子任务(parent_task)已 done;② acceptance_criteria 在最近输出中体现。
- 子任务聚合校验不可绕过(数据一致性)。
- acceptance 校验可用 todo_resolve(force=true) 绕过(仅在确认启发式误判时)。
</todo_resolve_gate>
```

## 数据流

### resolve(标完成)路径

```
agent 调 todo_resolve(task_id, force=false)
  ↓ handler:
  1. 取 task(A 现状:存在 + in_progress)
  2. 聚合:children_all_done(by_id, task_id)
     ├ pending → 收 error "未完成子任务: ..."
  3. acceptance(criteria 非空 且 not force):
     run_verify(task, by_id, last_turn_text)
     ├ not passed → 收 error "acceptance 未满足: ..."
  4. errors 非空 → is_error=True return(不 resolve)
  5. 全过 → service.resolve(task_id)
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
  agent 调 todo_resolve → C 软拦(聚合 + acceptance + force)
```

B 软提示 + C 软拦,两层互补。

## 错误处理

| 情况 | 处理 |
|---|---|
| resolve:children 未全 done | is_error,列 pending children,不 resolve(**force 也不跳**) |
| resolve:acceptance 不过 + force=false | is_error,列 missing_criteria,不 resolve |
| resolve:acceptance 不过 + force=true | 跳 acceptance,仍查聚合;聚合过则 resolve,llm_text 回显"已绕过 acceptance 校验" + warn log |
| resolve:两道都不过 | **两个都报**(一次性给全 error 列表,省 agent 来回) |
| resolve:空 acceptance | 跳 acceptance 校验,只查聚合 |
| resolve:无 children 的 task | 聚合 (True, []),直接放行到 acceptance 校验 |
| tree 视图遇 parent 环 | visited set 截断 + 标 ⚠ cycle,不崩 |
| tree 视图孤儿(parent 已删) | A 阶段 force-delete 留 `dangling_parent`,tree 渲染时 parent 找不到 → 当顶层处理 + 标注 |
| last_turn_text 为空(首轮) | run_verify 返回 passed=true + "无产出" hint(B 已定义),acceptance 校验放行(不焦虑首轮) |
| deps 缺 last_turn_text(兼容旧调用) | handler 默认 `last_turn_text=""`,走 run_verify 空文本路径(放行 + hint) |
| service.list 抛 | handler 现有 `_err` 兜底(A 已有) |
| run_verify 单 task 抛 | acceptance 校验 fail-soft:跳过该检查(warn log),不阻断聚合校验 + resolve |

## 测试策略

### 单元测试(目标 100% line + branch)

**`dependency.py:children_all_done`**(新增,~5):
- 无 children → (True, [])
- children 全 done → (True, [])
- 部分 done → (False, [pending ids 字典序])
- children 引用缺失(容错)→ 跳过不阻塞
- 字典序确定性

**`tools.py:todo_resolve` 软拦**(改,~7):
- 聚合不过 → is_error 列 children
- acceptance 不过 + force=false → is_error 列 missing
- 两道都不过 → 两个 error 都报
- force=true 跳 acceptance → resolve 成功 + warn
- force=true 不跳聚合(仍有 pending children)→ is_error
- 空 acceptance → 跳 acceptance,聚合过则 resolve
- 正常两道都过 → resolve 成功

**`tools.py:_render_toposort` tree 视图**(改,~6):
- 单层 children 缩进
- 多层嵌套(孙)
- 混合(有 parent 的 + 无 parent 的顶层)
- parent 环 visited 兜底不崩
- 截断 50
- `view=flat` 默认不破现状(回归)

### 集成测试(`tests/test_c_integration.py`,FakeLLM,~5)

- agent resolve acceptance 未满足 task → 收 error → 下轮补齐(criteria 命中)→ 再 resolve 成功
- agent resolve parent(children 没 done)→ 收 error 列 children → 完成 children → resolve parent 成功
- agent force=true 绕过 acceptance resolve 成功
- deps 注入 last_turn_text 接线(handler 能读到上轮文本)
- agent 拆任务(连续 todo_create 挂 parent)+ todo_toposort view=tree 看到树

### E2E(`tests/_test_c_e2e.py`,gated,1)

- 真 LLM:创建 parent + children,完成 children,resolve parent,assert 聚合校验生效;或 acceptance 未满足时被拦

### 覆盖目标

| 模块 | 目标 |
|---|---|
| `cc_harness/project/dependency.py` 新增 `children_all_done` | 100% |
| `cc_harness/project/tools.py` `todo_resolve` 软拦 + tree 视图 | ≥85% |
| `cc_harness/project/extras.py` deps 接线 | 集成测试覆盖 |
| `cc_harness/agent.py` prompt 提示 | 单元覆盖关键路径 |
| `cc_harness/repl.py` deps 传参 | 集成测试覆盖 |

### 回归保护

- B 阶段 baseline **1105** 测试必须保住
- B 阶段 6 E2E + `test_b_integration.py` 仍过
- A 阶段 `test_project_*.py` / `test_repl_b_hook.py` 等仍过
- C 阶段预期新增 ~24 测试(18 单元 + 5 集成 + 1 E2E gated)

## 实施优先级(供 writing-plans 阶段参考)

按依赖链拆解:

1. **`children_all_done`**(dependency.py)— 无依赖,先写测试再填实现
2. **`todo_resolve` 软拦 + force**(tools.py)— 依赖 1 + B 的 run_verify + deps 注入(组件 4)
3. **deps 注入 last_turn_text**(extras.py + repl.py)— 依赖 2(handler 要用),可与 2 合并提交
4. **`todo_toposort` view=tree**(tools.py)— 依赖 1(无需),可与 2 并行
5. **system prompt 提示**(agent.py)— 依赖 2,最后接
6. **集成测试 + E2E** — 全部就绪后跑

依赖链:1 → 2 + 4(并行);2 ↔ 3(合并);2 → 5;全部 → 6

## 开放问题(plan 阶段核实)

1. **现有 7 个 tool handler 签名**是否硬编码 `(args, *, service, session_id, cwd)`?若硬编码,deps 加 `last_turn_text` 后,它们收不到会 TypeError。**plan 阶段必须核实**:要么全改 `**kwargs`,要么只给 `todo_resolve_handler` 加形参 + 其他不动(但 deps 是 dict 合并,多一个 key 不影响 `**kwargs` 的 handler)。倾向后者(只 resolve 用,其他忽略多余 key)。
2. **聚合校验是否要递归孙**?当前设计只看直接 children(一层)。深层 HTN(parent → child → grandchild)时,grandchild 没 done 但 child 已 done(矛盾,不该发生)。若担心,可加递归,但 YAGNI 起步只看一层。**plan 阶段锁死**。
3. **`force=true` 时 llm_text 的提示文案**:是否要更强提示"你正在绕过完成校验,确认 task 真的完成了吗"?当前设计回显"已绕过 acceptance 校验",plan 阶段可加强化。
4. **tree 视图与 flat 视图的 group 过滤组合**:`group=ready + view=tree` 怎么处理?当前设计 group 过滤 task 集合,view 决定渲染;两者正交。plan 阶段验证边界。

## Out of scope(明确不做)

- ❌ 自动 HTN 规划器(给大目标自动递归拆 task 树,= 外层 loop 的一部分)
- ❌ 完成自动级联(children 全 done 自动标 parent)
- ❌ service 层硬拦(status_guard 抛异常)
- ❌ parent 环前置检测(create/update 时拦,改 A baseline)
- ❌ 跨轮文本聚合(resolve 时看整个 in_progress 期间所有产出)
- ❌ `todo_decompose` 新 tool(复用 `todo_create` 挂 parent)
- ❌ HTN 可视化 UI(Live panel / tree 视图 tool 已够)
- ❌ SubAgent / Agent Team(引擎其二,远期)
- ❌ LLM judge 替代 acceptance 启发式(复杂度翻倍,B 已砍)
- ❌ acceptance_criteria 自动抽取(A 已砍)
- ❌ parent_task 自动 depend_on 注入(会死锁,见 decision 1)

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| acceptance 启发式误判率高 → 软拦太烦 | force=true 绕过;真误判多 → 扩 stopword 或调 heuristic(B 阶段已留扩展点) |
| last_turn_text 滞后一轮(同轮 resolve) | 典型模式(做事→下轮 resolve)不受影响;同轮靠 force 兜底;不做跨轮聚合(YAGNI) |
| deps 加 last_turn_text 破其他 7 个 handler 签名 | plan 阶段核实签名(开放问题 #1);倾向 `**kwargs` 兼容 |
| parent 环导致 tree 渲染无限递归 | visited set 兜底(零 baseline 风险) |
| 聚合校验只看一层,深层 HTN 漏孙 | YAGNI 起步一层;孙的聚合由孙自己 resolve 把关 |
| force 被滥用(agent 养成无脑 force 习惯) | prompt 提示"仅在确认启发式误判时";warn log 审计 |
| C 的软拦与 B 的软提示职责重叠混淆 | B = 每 turn 后提醒(预防),C = resolve 动作点兜底(强制);prompt 分别说明 |
| C 阶段 commit 破坏 B 阶段 1105 baseline | 强约束:每个 task 跑 pytest,B baseline 必须保住 |
| run_verify 单 task 抛 → resolve 卡死 | fail-soft:跳过该检查,不阻断 resolve(宁可放过不卡死) |

## 迁移计划

**影响面**:
- `cc_harness/project/dependency.py`(加 `children_all_done`)
- `cc_harness/project/tools.py`(改 `todo_resolve_handler` + `_render_toposort` + `TODO_RESOLVE_SPEC` + `TODO_TOPOSORT_SPEC`)
- `cc_harness/project/extras.py`(deps 加 `last_turn_text`)
- `cc_harness/repl.py`(调用点传 `last_turn_text`)
- `cc_harness/agent.py`(`_refresh_system_prompt` 加 `<todo_resolve_gate>` 静态段)

**迁移方案**:
1. **依赖顺序**:1(children_all_done)→ 2+3(resolve 软拦 + deps)→ 4(tree 视图)→ 5(prompt)→ 6(集成)
2. **每步独立 commit**:`feat(dependency): children_all_done`、`feat(tools): todo_resolve 软拦 + force`、`feat(tools): todo_toposort view=tree`、`feat(repl/agent): last_turn_text deps + resolve_gate prompt`
3. **回退友好**:每个 commit 可单独 revert;C 功能可关闭(不调 force / view=flat / prompt 段空)
4. **B baseline 保护**:每个 commit 跑 `pytest tests/`,1105 baseline 必须保住

**backout**:
- `children_all_done` 删除 → todo_resolve 软拦退化为只查 acceptance(或全跳)
- `view=tree` 删除 → toposort 回 flat(默认值,零影响)
- `<todo_resolve_gate>` prompt 段删除 → agent 不知道有门,但门仍在(门是 tool 层的,prompt 只是告知)
- deps 不传 last_turn_text → handler 默认空字符串,acceptance 校验走空文本路径(放行)

**commit message 规范**:每个 C 阶段 commit 末尾必须显式报告:`baseline: 1105 passed → now: X passed (delta +N)`,确保 B 阶段 1105 是否保住在 commit message 一眼可见。

## 后续 plan(本 C 完成后另立)

- **Sub-project D(推测)**:SubAgent / Agent Team(引擎其二)—— 用 B 的 `get_ready_tasks` 做 fan-out,用 C 的聚合校验做 subagent result 验收入口,用 B 的 verify hook 做 subagent 产出 check。C 的 `parent_task` 树天然映射 subagent 任务分解。
- **跨轮文本聚合**:若 C 的单轮 last_turn_text 在实际使用中误拦率高,立小 spec 做"resolve 时聚合 in_progress 期间所有 turn 文本"。
- **parent 环前置检测**:若渲染兜底的 ⚠ cycle 频繁出现,立小 spec 在 create/update 加 parent 环检测。
