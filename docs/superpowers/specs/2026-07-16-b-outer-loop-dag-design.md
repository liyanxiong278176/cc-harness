# 其一·长程任务 — Sub-project B:外层 loop + DAG 底座设计

> **范围**:cc-harness AI 工程目标"其一·长程任务" 5 红 + 3 黄中的 **B 子集**——DAG 数据完整 + 状态/启发式 verify hook + 主 agent 用的拓扑工具。
>
> **不**实现"外层 plan-execute-verify-replan loop"作为一个独立运行实体——本 spec 是**最小底座**,完整编排交给主 agent 自身(短期)和将来的 subagent spec(远期)。
>
> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。

## Goal

承接 Sub-project A 已经落地的 DAG 字段(`depends_on`)+ 状态机(`status_guard`)+ 依赖校验(`check_no_cycle` / `dep_check`),B 阶段补齐三件最小底座:

1. **`topo_sort` + `get_ready_tasks`** —— 把 A 阶段的 Kahn 占位填上,提供"DAG 拓扑视图"给主 agent 决策
2. **Verify hook(`_after_turn_todo` 实现)** —— 每 turn 扫 in_progress task,启发式 + 状态机双轨 verify,结果以 hints 形式注入下一轮 prompt。**不自动改 status**
3. **`todo_toposort` agent tool** —— 让主 agent 在脑子里做编排决策时能查 DAG 拓扑

**B 阶段不做**:外层 loop、CLI 批跑命令(`cc-harness run --until`)、自动续干/自动 replan、自动 `status=blocked` 触发。这些交给将来的 subagent spec。

## 设计前提(重要)

subagent(引擎其二)是 **远期目标**,不是 B 阶段的依赖:

- B 阶段不假设 subagent 已落地
- B 阶段产出的 DAG 数据 / verify hook / topo 工具在主 agent 单 turn 模型下也能用(hints 注入 prompt、tool 让 LLM 自己查拓扑)
- 如果 subagent 短期落地,B 阶段的产物**直接作为其前置底座**(`get_ready_tasks` 给 subagent fan-out 用,verify hook 给 subagent result 验收入口用)
- 如果 subagent 永远不来,B 阶段的产物也**不退化**——主 agent 用 hints + tool 自己做编排

**承认 A spec line 787 的 "plan-execute-verify-replan loop" 表述是 subagent 立项前的过渡方案**。B 不实现完整 loop,只实现"loop 的数据底座 + verify hook"。

## 现有代码事实(spec 写入时核实)

| 文件 | 现状 | B 处置 |
|---|---|---|
| `cc_harness/project/dependency.py` | A 已落 `check_references` / `check_no_cycle` / `dep_check` + `DependencyCycleError`;`topo_sort` 是 `# TODO: B 阶段实现 Kahn` 占位 | **填占位**:`topo_sort(tasks: dict) -> list[id]` Kahn 算法 + `get_ready_tasks(tasks: dict) -> list[TodoTask]` |
| `cc_harness/project/service.py` | A 已落 7 ops + `subscribe` / `unsubscribe` + `_emit` 事件总线 | **不直接改**,verify hook 通过 `service.list()` 读 |
| `cc_harness/project/tools.py` | A 已落 7 个 tool handler(`todo_list` / `todo_get` / `todo_create` / `todo_update` / `todo_delete` / `todo_resolve` / `todo_validate`) | **新增第 8 个**:`todo_toposort` |
| `cc_harness/repl.py:465` | A 阶段 `_after_turn_todo(state, todo_service)` 是 `pass` 占位 | **填实现**:每 turn 跑 verify 写 `state.todo_hints` |
| `cc_harness/repl.py:53` | `ReplState` 含 todo_service / live_panel / todo_extras / resume_task | **新增字段**:`todo_hints: list[str]` + `last_turn_text: str` |
| `cc_harness/repl.py:399` | `_after_turn_todo` 调用点在 `_after_turn_memory` 之后 | **不动调用点**,只改 `_after_turn_todo` 内部 |
| `cc_harness/agent.py:_refresh_system_prompt` | A 阶段已加 resume_task append(`messages[0]["content"] += "..."`) | **追加 todo_hints append**,与 resume 并列 |
| `docs/superpowers/specs/2026-07-14-long-horizon-task-tracking-design.md` | A spec 注释:`B 阶段加 DAG 拓扑约束`(line 807)、`_after_turn_todo` 占位由 B 填(line 1096)、`topo_sort` 在 `dependency.py` 补实现(line 1329) | 本 spec 是这三处的兑现 |

## 关键决策(brainstorm 5 段确认)

### 关于外层 loop 的形态(decision #1)

**B 不实现外层 loop**。理由:

- 完整 loop 把决策从 LLM 手里夺走(verify 失败谁来定 replan?loop 还是 LLM?)
- 与 Claude Code 的"主 agent 自己管 Todo + subagent 派活"模型冲突
- REPL 单 turn 心智不破(B 不改 `run_turn` 签名)
- A 阶段的 `resume_task` 已经覆盖"跨 session 续干"需求

### 关于 verify hook 的策略(decision #2)

**α(启发式) + γ(状态机) 组合,不引入 LLM judge**。理由:

- C' 最小底座,LLM judge 翻倍复杂度,YAGNI
- 启发式 + 状态机**目标不是"准",是"有"** —— 给 LLM 一个"嘿你好像没提到这个"的提示
- 启发式匹配失败**不自动改 status** —— C' 承诺"标注 + 提示",不替 LLM 做决定
- 与 L4 权限闸门兼容(verify hook 只读 TodoService 状态,不写)

### 关于 hints 注入策略(decision #3)

**延迟一轮生效 + 每 turn 覆盖 + 两层截断**。理由:

- Turn N 的 verify 反馈 → 注入 Turn N+1 的 system prompt(LLM 已返回,TN 注入没意义)
- 覆盖而非累积 → hints 不会指数膨胀
- 每 task 最多 3 条 + 全局最多 10 条 → prompt 噪声可控

## 组件设计

### 组件 1:`topo_sort` + `get_ready_tasks`

**位置**:`cc_harness/project/dependency.py`(填现有 TODO 占位)

```python
def topo_sort(tasks: dict[str, TodoTask]) -> list[str]:
    """Kahn 算法。返回拓扑序 list[id];失败抛 DependencyCycleError。

    Tiebreaker:字典序(deterministic,LLM 输出可重现)。
    只跟踪存在于字典内的依赖边,缺失依赖由 check_references 报告。
    空字典返回 []。
    """

def get_ready_tasks(tasks: dict[str, TodoTask]) -> list[TodoTask]:
    """返回所有 ready 的 task — 状态是 pending 且 depends_on 全 done。

    'done' 视为已就绪;不存在于字典的依赖 id 视为不阻塞(由 validate 报告)。
    """
```

**TDD 边界 case**(详测试策略段):空 dict、单 task、链、菱形、并行、环、缺失依赖、self-loop、字典序 tiebreaker。

### 组件 2:`verify.py`(新文件)

**位置**:`cc_harness/project/verify.py`

**API**:

```python
@dataclass
class VerifyResult:
    task_id: str
    passed: bool                          # 整体是否通过
    missing_criteria: list[str]           # 启发式未命中的 criterion
    hints: list[str]                      # 给 LLM 的提示文本


def heuristic_check(criteria: list[str], text: str) -> tuple[bool, list[str]]:
    """启发式检查 text 是否覆盖 criteria 每一条。

    规则:criterion 拆词(去 stopword)后至少 1 个关键词出现在 text 拆词集合。
    空 criteria → (True, [])。空 text → (False, criteria)。
    criterion < 3 字符 → 跳过(避免噪声)。
    """

def state_check(task: TodoTask, all_tasks: dict[str, TodoTask]) -> tuple[bool, str | None]:
    """状态机检查 — depends_on 全 done?

    Returns:
        (deps_ready, hint_or_none)
    """


def run_verify(
    task: TodoTask,
    all_tasks: dict[str, TodoTask],
    last_turn_text: str,
) -> VerifyResult:
    """组合 heuristic + state。

    行为:
        - status != in_progress → passed=True, no-op
        - acceptance_criteria 为空 → passed=True
        - last_turn_text 为空 → passed=True, hints 追加"无产出"提示
        - 否则跑 heuristic + state,任一失败 → passed=False
    """
```

**stopword 列表**(hardcode,简洁版):

```python
_STOPWORDS = {"the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "on",
              "的", "了", "和", "是", "在", "有", "我", "你", "他", "她", "它"}
```

中英文各 10 个,YAGNI,需要时扩展。

### 组件 3:`todo_toposort` tool

**位置**:`cc_harness/project/tools.py`(A 阶段 7 tool 后新增第 8 个)

**OpenAI spec**:

```python
{
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
```

**handler** 摘要:

```python
async def handle_todo_toposort(args, *, service, session_id):
    tasks_list = await service.list(include_done=True)
    by_id = {t.id: t for t in tasks_list}
    group = args.get("group", "all")
    tasks = _filter_by_group(tasks_list, group)

    try:
        order = topo_sort(by_id)
        topo_error = None
    except DependencyCycleError as e:
        order = None
        topo_error = str(e)

    llm_text = _render_toposort(order, tasks, by_id, topo_error)
    return ToolResult(
        is_error=topo_error is not None,
        display_text=f"topo: {topo_error or f'{len(order)} tasks'}",
        llm_text=llm_text,
    )
```

**`_render_toposort` 截断**:`len(tasks) > 50` 时截断并提示。

**与 `todo_list` 不重叠**:`todo_list` 按 status filter,`todo_toposort` 提供拓扑序(新维度)。

### 组件 4:`_after_turn_todo` 接线

**位置**:`cc_harness/repl.py:465`

**实现**:

```python
MAX_HINTS_PER_TASK = 3
MAX_HINTS_TOTAL = 10


async def _after_turn_todo(state: ReplState, todo_service) -> None:
    """B 阶段 verify hook。每 turn 跑一次,不自动改 status。
    
    扫所有 in_progress task,跑 run_verify,结果写到 state.todo_hints。
    agent._refresh_system_prompt 读 hints 注入到 system prompt 末尾。
    """
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
            continue  # 单 task 失败不影响其他

        if not result.passed:
            per_task = []
            for miss in result.missing_criteria:
                per_task.append(f"task {task.id} criterion 未在最近一轮输出中体现: {miss}")
            per_task.extend(result.hints)
            hints.extend(per_task[:MAX_HINTS_PER_TASK])

    state.todo_hints = hints[:MAX_HINTS_TOTAL]  # 覆盖 + 截断
```

**`ReplState` 新增字段**:

```python
@dataclass
class ReplState:
    # ... 现有字段
    todo_hints: list[str] = field(default_factory=list)
    last_turn_text: str = ""
```

**`agent.py:_refresh_system_prompt` 新增 section**:

```python
# 在 build_system_prompt 调用后, append resume_append 之后
todo_hints = getattr(state, "todo_hints", []) or []
if todo_hints:
    prompt += "\n\n<todo_hints>\n" + "\n".join(todo_hints) + "\n</todo_hints>"
```

注入位置:`messages[0]["content"]` 末尾 → 与 resume append 并列。

**`last_turn_text` 接线**:

```python
# repl.py 主循环 run_turn 调用后, _after_turn_todo 调用前
state.last_turn_text = _extract_final_text(result_messages)
```

`_extract_final_text` 简单函数:从 messages 末尾找 role=assistant 且 content 是非空 str 的那条。

## 数据流(单 turn 内)

```
Turn N 开始
  state.todo_hints: [Turn N-1 verify 留下的]
  state.last_turn_text: [Turn N-1 的输出]
  ↓
run_turn(N)
  _refresh_system_prompt
    → 注入 <todo_hints> 段(基于 state.todo_hints)
  LLM ReAct(可能调 todo_toposort / todo_list / todo_update)
  返回 messages
  ↓
state.last_turn_text = _extract_final_text(messages)  ← Turn N 的输出
  ↓
_after_turn_todo(state, todo_service)
  → 跑 run_verify, 写 state.todo_hints(覆盖)
  ↓
Turn N+1 开始
  state.todo_hints: [Turn N 的 verify 留下的]
  ...
```

**关键**:hints **延迟一轮生效**——Turn N verify 写 hints,Turn N+1 prompt 才注入。

## 错误处理

| 异常 | 处理 |
|---|---|
| `todo_service.list()` 抛 | 静默 return,不清旧 hints,warn log |
| `run_verify()` 单 task 抛 | 跳过该 task,其他继续,warn log |
| 整个 hook 顶层抛 | swallow,warn log(跟 A `_after_turn_memory` 同模式) |
| `DependencyCycleError` 在 `todo_toposort` | 转 `is_error=True` + 报告环路径,不抛 |
| `topo_sort` 输入空 dict | 返回 `[]`,不抛 |
| `topo_sort` 缺失依赖 | 跳过该边,不阻塞拓扑(由 `validate()` 报告) |
| `last_turn_text` 空 | passed=True,hint "无产出"(避免首轮 turn 焦虑) |
| `acceptance_criteria` 空 | passed=True,no hints |
| `state.todo_hints = []` | agent 不注入 `<todo_hints>` 段 |
| 50+ task tool 输出 | 截断 + 提示 |
| session 重启 | hints 清空(ReplState default),跨 session 不注入旧 hints |

## 测试策略

### 单元测试(目标 100% line + branch)

**`dependency.py`**(A 已 100%,B 新增函数保持):
- `topo_sort`:空 / 单 / 链 / 菱形 / 并行 / 环 / 缺失依赖 / self-loop / 字典序 tiebreaker / done task 包含
- `get_ready_tasks`:空 / pending 无依赖 / pending 依赖全 done / pending 依赖部分 done / in_progress 排除 / 缺失依赖不阻塞

**`verify.py`**(新文件,目标 100%):
- `heuristic_check`:空 criteria / 空 text / 子串 / 关键词 / miss / 大小写 / 短 criterion 跳过 / stopword / 中英混合
- `state_check`:无依赖 / 全 done / 部分 done / 含 in_progress / 缺失
- `run_verify`:非 in_progress / 空 criteria / 全 pass / heuristic fail / state fail / 空 text

**`tools.py` 新 handler**(目标 ≥85%):
- `todo_toposort` × 7 case:default group / ready / in_progress / blocked / cycle / 空 manifest / 截断

### 集成测试(`tests/test_repl_b_hook.py` 新文件)

- `_after_turn_todo` 每 turn 触发 / 写 hints / 覆盖 hints
- `_after_turn_todo` service error swallow / 单 task 失败跳过 / no service no-op
- `agent._refresh_system_prompt` 注入 `<todo_hints>` 段 / 空 hints 不注入
- hints 截断 3/10 边界

### E2E 测试(`tests/_test_b_e2e.py`,`_` 前缀 gated)

- `test_e2e_llm_uses_topo_sort`(FakeLLM 预设响应,无需真 LLM)
- `test_e2e_verify_hints_influence_next_turn`(FakeLLM 预设)
- 1 个 `@pytest.mark.requires_llm` 真 LLM 测试

### 覆盖目标

| 模块 | 目标 |
|---|---|
| `cc_harness/project/dependency.py` | 100%(A baseline + B 新增保持) |
| `cc_harness/project/verify.py` | 100%(新文件) |
| `cc_harness/project/tools.py` 新 handler | ≥85% |
| `cc_harness/repl.py` 接线点 | 集成测试覆盖 |
| `cc_harness/agent.py` 新增 append | 单元测试覆盖关键路径 |

### 回归保护

- A 阶段 1016 测试 baseline 必须保住
- A 阶段 6 E2E(`_test_project_e2e.py`)仍过
- A 阶段 `test_repl_resume.py` 等 5 个 agent 测试仍过
- B 阶段预期新增 ~30 测试(20 单元 + 10 集成)

## 实施优先级(供 writing-plans 阶段参考)

按依赖链拆解:

1. **`topo_sort` + `get_ready_tasks`**(dependency.py)— 无依赖,先写测试再填实现
2. **`verify.py` 新文件** + `heuristic_check` / `state_check` / `run_verify` — 无依赖,可与 1 并行
3. **`todo_toposort` tool handler**(tools.py)— 依赖 1 + 2
4. **`_after_turn_todo` 实现**(repl.py)— 依赖 2
5. **`agent.py` `<todo_hints>` append**(agent.py)— 依赖 4
6. **`ReplState` 字段 + `last_turn_text` 接线**(repl.py)— 与 4 同文件,合并提交
7. **集成测试 + E2E** — 全部就绪后跑

依赖链:1, 2 → 3;1, 2 → 4 → 5;1, 2 → 6;全部 → 7

## 开放问题(写作 plan 时必须答)

1. **`topo_sort` 返回 list[id] 后,tool handler 是否要在 llm_text 里同时输出 task title?**(当前设计:只渲染 ready/in_progress/blocked 分组的 task title + id;拓扑序只输出 id 列表。是否够清晰?)
2. **`heuristic_check` 的 stopword 列表是否要中英文各扩到 30+?**(当前 10+10,YAGNI 起步;真误判多再扩)
3. **`MAX_RENDER_TASKS = 50` 是否合理?**(基于"50 task 内 LLM 一次性消化";真有大项目 100+ task → 改参数或拆项目)
4. **`<todo_hints>` 注入位置在 resume 段之后还是之前?**(当前设计:resume 之后。LLM 注意力偏末,但 resume 是身份信息,hints 是行动指引,放后面让 LLM "先记住我是谁,再看行动指引"——可能反了,需 plan 阶段验证)
5. **`last_turn_text` 是否要在 messages 末尾的 assistant message 含 tool_calls 时 fallback 找上一条文本?**(当前设计:简单 fallback,但更"对"的实现是合并"工具结果段"作为 verify 目标——但这跟 L5 DLP "工具观察段不扫" 冲突,YAGNI)

## Out of scope(明确不做)

- ❌ 外层 plan-execute-verify-replan loop(主 agent 自己管)
- ❌ `cc-harness run --until <condition>` CLI 批跑命令
- ❌ 自动续干 / 自动 replan
- ❌ Verify 失败自动改 `status=blocked`(LLM 自己决定)
- ❌ DAG 可视化 UI(Live panel 已经展示 task 列表)
- ❌ SubAgent / Agent Team(引擎其二,远期目标)
- ❌ Self-Play / 自改代码(引擎其三)
- ❌ 多 session 并发写同一 manifest 的 lock(C' 是单 session,git 自然冲突)
- ❌ Verify 结果写 `logs/b_verify.jsonl` 专属审计(走标准 logger)
- ❌ LLM judge 替代启发式 verify(复杂度翻倍)

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 启发式 verify 误判率高 | hints 不自动改 status,LLM 可忽略;真误判多 → 扩 stopword 或弃用 |
| `topo_sort` 在大项目上慢 | Kahn O(V+E),100 task 仍 < 1ms;YAGNI 不优化 |
| `last_turn_text` 取错段 | 取 assistant 文本 content,跳过 tool_calls;有边界 case 时 fallback |
| hints 注入 prompt 把 LLM 弄糊涂 | 每 task 最多 3 条 + 全局 10 条,LLM 仍可忽略 |
| 50 task tool 输出截断让 LLM 漏信息 | LLM 自己用 `todo_list` filter 补充 |
| `DependencyCycleError` 在 tool 里转 is_error,LLM 困惑 | llm_text 明确给"建议 todo_update 修正 depends_on" |
| 跨 session hints 残留 | ReplState default 清空,新 session turn==0 无 hints |
| A 阶段 resume 协议被 B 的 hints append 破坏 | 两段并列 append,不互斥,测试覆盖 |
| B 阶段 commit 破坏 A 阶段 1016 baseline | 强约束:每个 task 跑 `pytest tests/`,必须 1016 全过 |
| LLM judge 后续被要求加进来 | 留 `run_verify` 扩展点(未来 `run_verify_llm`),B 不实现 |

## 迁移计划

**影响面**:
- `cc_harness/repl.py`(加 2 字段 + 填 _after_turn_todo 实现)
- `cc_harness/agent.py`(_refresh_system_prompt 加 1 段 append)
- `cc_harness/project/dependency.py`(填 topo_sort 占位 + 新增 get_ready_tasks)
- `cc_harness/project/verify.py`(新建)
- `cc_harness/project/tools.py`(加第 8 个 tool)

**迁移方案**:
1. **依赖顺序**:先 1 + 2(纯逻辑),后 3 + 4 + 5 + 6(接线),最后 7(集成测试)
2. **每步独立 commit**:`feat(dependency): topo_sort Kahn`、`feat(project): verify.py heuristic + state`、`feat(project): todo_toposort tool`、`feat(repl): _after_turn_todo verify hook`、`feat(agent): inject <todo_hints>` 段
3. **回退友好**:每个 commit 可单独 revert,B 阶段功能可关闭(state.todo_hints 默认空 → hook 不注入)
4. **A baseline 保护**:每个 commit 跑 `pytest tests/`,1016 baseline 必须保住

**backout**:
- `state.todo_hints` 默认空 → agent 不注入 → verify hook no-op(功能关闭)
- `topo_sort` 占位没填 → `todo_toposort` 不可用,但不破坏其他 tool
- `verify.py` 新文件 → 删除即可

## 后续 plan(本 B 完成后另立)

- **Sub-project C**:手动目标拆解(HTN)+ Checkpoint 自检(`_after_turn_todo` 已落,C 阶段可读 hints 决定是否扩展)
- **Sub-project D(推测)**:SubAgent / Agent Team(引擎其二)—— 用 B 阶段的 `get_ready_tasks` 做 fan-out,用 verify hook 做 result 验收入口