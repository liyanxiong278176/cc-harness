# Sub-E1: Decomposer — design

> **Status**: spec review (待用户审)
> **Date**: 2026-07-23
> **Branch**: `master`(本 spec 不限分支,merge 后归 E1)
> **Author**: brainstorm 7 轮澄清 + 5 段设计

## Goal

把当前 cc-harness 中"**LLM 自主分解 + 派 subagent**"的能力从**隐式可选**(prompts.py:73-79 `todo_block` "可选") + **静态触发**(agent.py:48-64 `SUBAGENT_HINTS_BLOCK` 仅在"最近 4 轮内 todo_create + parent_task 非 None"时注入)升级为**显式契约**(E1 分解契约 section + iter=0 注入 + system hard validation + auto retry + 实时进度 + user 可 reject)。

E1 不重新发明 SubAgentRunner(沿用 D1),不引入新子包(轻量加层),不引入 plan 阶段(Q1 否决)。E1 把 B/C/D1 已有的能力收敛为一个**显式契约**,让 LLM 在第 1 轮 ReAct 就被告知"该任务要分解吗?怎么分解?怎么 fan-out?"。

## 现有代码事实(spec 写入时核实)

- **`prompts.py:_todo_block`** (lines 73-79) 文字块 "📝 TODO:" 在思考后**可选**输出;LLM 不强制输出,无硬规则。
- **`prompts.py:_tool_discipline`** (lines 82-98) 第 3 条 "**工具能力诚实**" 已含子规则:无合适工具就告知用户;E1 不重写此规则(只引用)。
- **`agent.py:SUBAGENT_HINTS_BLOCK`** (lines 48-64) 静态提示块,触发条件 `_has_recent_htn_parent_create(messages, lookback=4)`(agent.py:937-989),**仅当 LLM 已发过 HTN parent `todo_create` 工具调用**才注入。**触发太晚** — LLM 还没意识到要建 HTN 父任务时,hint 不出现。
- **`agent.py:_refresh_system_prompt`** (lines ~870-934) 已有 `extra_ctx` 注入路径(Q3/Q4/E2 已用);E1 沿用此路径加 `e1_decompose_hint` + `iter_count`。
- **`project/tools.py:todo_create_handler`** (lines ~200-400) 创建 todo 任务,接受 `acceptance_criteria: list[str]` 字段;**当前无长度校验**。
- **`project/subagent.py:SubAgentRunner.run()`** (lines 321-528) 单 subagent 跑完返 `SubAgentResult`,8 种 status 值;**当前无 auto retry**;`retried` 字段不存在。
- **`project/tools.py:dispatch_subagent_handler`** (lines ~1100-1311) 并行派 N 个 subagent,沿用 `_render_subagent_summary` 聚合;**当前无实时进度 callback**,无失败 pause 决策。
- **`cc_harness/policy.py`** `PolicyConfig` dataclass 当前无 `e1_decompose_enabled` 字段;`policy.yaml` 配置文件已存在。
- **`cc_harness/repl.py:_handle_slash`** 已有 `/plan /design /coding /mode /help /clear`;**无 `/reject`** slash command。
- **`E2 ReflectionEngine.emit`** 已支持任意 `ReflectionEvent` 子类;`subagent_failed` 事件已触发;E1 不需改 reflection。
- **`agent.py:run_turn`** `iter_count: int = 0` 变量(lines 185)逐轮 +1;Q2 已知这是天然 trigger 点。

## 关键决策(brainstorm 7 轮)

### D1:作用面

**Q1=D — 自动分解 + smart fan-out,不引入 plan 阶段**。LLM 自主评估"是否需要分解 + 是否 fan-out",E1 不强加"必须分解"。plan 阶段(Q1-B)被否决,理由:(1) `mode == "plan"` 已存在,user 可 `/plan` 手动进入;(2) ReAct 哲学破坏(user 强制 round-trip);(3) `todo_create` 已是结构化 planning + C 完成门替 plan 阶段守 acceptance,无需 plan 阶段冗余抽象。

### D2:user-confirm 边界

**Q2=B — 软提示 + user 可 /reject**。LLM 自动建 todo,但第 1 轮 ReAct 后给 user 2-3 行 "📋 计划" 摘要;user 可 `/reject` 中断(本轮 todo 标 cancelled,LLM 继续走直接做路径)。**不是** plan 阶段那种"LLM 出方案 → user yes → 执行"的 round-trip。

### D3:fan-out 启发式

**Q3=B — 宽松启发式,信任 LLM**。`dispatch_subagent_handler` 不做并行可行性硬校验(图分析);LLM 自评 + `len(sub_specs) ≥ 2 且 N ≤ 5` 软门槛 + MaxDepth=2 硬限(已 D1 落地)。假并行风险由 user 摘要 + 完成门兜底。

### D4:Sub-task 粒度

**Q4=B — 系统硬校验 acceptance_criteria 1-5 条**。`todo_create_handler` 校验 `acceptance_criteria` 非空且 ≤ 5 条,不通过返 `is_error=True`,LLM 重写。**硬规则**而非启发式提示;理由:C 完成门有真东西可校验。

### D5:失败处理

**Q5=B + C 轻量**。E1 只做 B(auto retry 1 次);C(failure 回灌主 agent)留 post-merge。具体:`SubAgentRunner.run()` 加 `retried: bool = False` 内部状态;`failed / timeout / incomplete` 触发时若 `!retried` → `retried=True` + clean messages 重派 1 次;仍失败 → 走 A 聚合(已有 `_render_subagent_summary`)+ user 决策。

### D6:进度可见性

**Q6=C — 实时进度 + 异常即时打断**。`dispatch_subagent_handler` 内部 per-subagent progress callback 调 `print_info` 渲染(queued / running / done / failed);失败后 pause + user 决策(continue / retry / abort)。失败兜底用户决策在 REPL 模式用 `input()`;CLI / 测试模式注入 mock callback。

### D7:触发边界与护栏

**Q7=A — 无 over-decomposition 硬护栏**。E1 触发条件:(1) `mode == "coding"`;(2) `iter_count == 0`(每 user turn 第 1 轮);(3) `e1_decompose_hint` extra_ctx flag True;(4) 不在 `/reject` reject 状态。**无** `len(sub_specs) ≤ 10` / token budget 等硬护栏(由 user 摘要 + 完成门兜底)。

## 组件设计

### 改动点(全部增量,不改 D1 SubAgentRunner 主体)

```
cc_harness/
├── prompts.py              [MODIFY]  +_decomposition_hint section
├── agent.py                [MODIFY]  _refresh_system_prompt 注入 e1_decompose_hint (iter 0 only)
├── project/tools.py        [MODIFY]  todo_create_handler 加 acceptance_criteria 校验
├── project/subagent.py     [MODIFY]  SubAgentRunner.run() 加 auto-retry-once
├── project/tools.py        [MODIFY]  dispatch_subagent_handler 加实时进度 + 失败 pause
├── repl.py                 [MODIFY]  +/reject slash command + ReplState 字段
└── policy.py               [MODIFY]  +e1_decompose_enabled PolicyConfig 字段
```

### 组件 1:`_decomposition_hint` section(prompts.py)

```python
# cc_harness/prompts.py(新增 section,位置:在 todo_block 之后、tool_discipline 之前)

def _decomposition_hint(ctx: dict) -> str | None:
    """E1 D1/D2/D3/D7:分解契约 — 提示 LLM 在 iter 0 自主评估是否需要分解。"""
    if not ctx.get("e1_decompose_hint"):
        return None
    if ctx.get("mode") != "coding":
        return None
    if ctx.get("iter_count", 1) != 0:
        return None
    return (
        "## 分解契约\n"
        "复杂任务先想清楚:能不能拆成 ≥2 个**独立** sub-task?拆得了 → "
        "用 `todo_create` 建任务(每个 sub-task 必须有 1-5 条 acceptance_criteria),\n"
        "再用 `dispatch_subagent` 派 subagent 并行跑(限制 N≤5,MaxDepth=2 硬拒)。\n"
        "拆不了 / 单任务 → 直接做,不建 todo。\n"
        "\n"
        "判定标准:\n"
        "- 任务描述含 ≥2 个动词 / 含'并且/以及/先 X 再 Y' / 含'并行/拆成/分步' → 倾向分解\n"
        "- 单步修小 bug / 单行 fix → 直接做\n"
        "- 粒度提示:每个 sub-task 应可在 ≤10 轮工具调用内完成\n"
        "\n"
        "失败兜底:任何 sub-agent failed/timeout → 系统自动 retry 1 次,"
        "仍失败则聚合回主 agent 由你决策。"
    )
```

**SECTION_POOL 注册**:

```python
SECTION_POOL = [
    # ... 已有 14 项 ...
    ("decomposition_hint", _decomposition_hint, "e1_decompose_hint"),  # E1 新增
]
```

### 组件 2:`_refresh_system_prompt` 注入 e1 hint(agent.py)

```python
# cc_harness/agent.py:_refresh_system_prompt (改动 extra_ctx 注入)

# 现有路径(memory_layer / qa_context / E2 reflection):
extra_ctx = {
    "e1_decompose_hint": (iter_count == 0),  # E1:仅首轮注入
    "iter_count": iter_count,                # E1:section 自检用
    # ... 既有 qa_category / last_neg_reflection ...
}
_refresh_system_prompt(messages, cwd, mode, extra_ctx=extra_ctx, ...)
```

### 组件 3:`todo_create_handler` 硬校验(project/tools.py)

```python
# cc_harness/project/tools.py:todo_create_handler (新增校验)

async def todo_create_handler(args, *, service, session_id, cwd) -> ToolResult:
    # ... 既有参数解析 ...
    criteria = args.get("acceptance_criteria") or []
    
    # E1 D4:硬校验 1-5 条
    if not isinstance(criteria, list) or len(criteria) < 1:
        return _err("todo_create", TodoError(
            "acceptance_criteria 必须 1-5 条(sub-task 必须可验收)"
        ))
    if len(criteria) > 5:
        return _err("todo_create", TodoError(
            f"acceptance_criteria {len(criteria)} 条 > 5 上限(粒度太粗,请拆 sub-task)"
        ))
    
    # ... 既有创建逻辑 ...
```

### 组件 4:`SubAgentRunner.run()` auto retry(project/subagent.py)

```python
# cc_harness/project/subagent.py:SubAgentRunner.run() (新增 retried 形参 + auto retry 逻辑)

async def run(
    self, *, task_id, title, description="", criteria=None,
    parent_id="", session_id="s", timeout=240,
    retried: bool = False,  # E1 D5:auto retry 1 次
) -> SubAgentResult:
    # ... 既有实现 ...
    
    # E1 D5:auto retry 1 次 — transient 兜底
    if (result_obj is not None
            and result_obj.status in {"failed", "timeout", "incomplete"}
            and not retried):
        log.warning(
            "subagent %s 失败 (status=%s),auto retry 1 次",
            task_id, result_obj.status,
        )
        return await self.run(
            task_id=task_id, title=title, description=description,
            criteria=criteria, parent_id=parent_id,
            session_id=session_id, timeout=timeout,
            retried=True,  # 标记已 retry
        )
    
    return result_obj
```

**关键**:
- retry 用 **clean messages**(原 messages 丢弃,不污染)
- retry 1 次后无论结果都返回
- E2 reflection 仍触发(每次 run 都过 reflection path)

### 组件 5:`dispatch_subagent_handler` 实时进度(project/tools.py)

```python
# cc_harness/project/tools.py:dispatch_subagent_handler (新增 progress_cb + 失败 pause)

async def dispatch_subagent_handler(
    args, *, service, session_id, cwd,
    dispatch_subagent_runner, last_turn_text,
    progress_cb=None,            # E1 D6:实时进度 callback (默认 None)
    failure_pause_cb=None,       # E1 D6:失败 pause 决策 callback (默认 None)
) -> ToolResult:
    # ... 既有 parent_task 创建 ...
    
    # E1 D6:实时进度 callback 默认实现
    if progress_cb is None:
        from cc_harness.render import print_info
        async def progress_cb(task_id: str, status: str, detail: str = ""):
            icon = {"queued": "○", "running": "⠋", "done": "✓", "failed": "✗"}.get(status, "?")
            print_info(f"  {icon} [{task_id}] {status} {detail}")
    
    # 并行派发 + 收集结果
    tasks = [
        _run_with_progress(
            dispatch_subagent_runner, progress_cb,
            spec, parent_id, session_id,
        )
        for spec in sub_specs
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # E1 D6:失败 pause 决策(若有未 retry 已 fail 的)
    if failure_pause_cb is not None:
        for r in results:
            if isinstance(r, SubAgentResult) and r.status in {"failed", "timeout", "blocked"}:
                decision = await failure_pause_cb(r)
                if decision == "abort":
                    break  # 终止后续聚合
    
    # ... 既有 _render_subagent_summary 调用 ...
```

REPL 层注入 callback:

```python
# cc_harness/repl.py:run_turn 调 dispatch_subagent 时(若有 ReplState)

async def _repl_failure_pause_cb(r: SubAgentResult) -> str:
    """REPL 模式:失败 pause,问 user 决策。"""
    print_warn(f"[{r.task_id}] {r.status}: {r.error[:200]}")
    print_info("continue / retry / abort?")
    ans = input("> ").strip().lower()
    return ans if ans in {"continue", "retry", "abort"} else "continue"
```

### 组件 6:`/reject` slash 命令(repl.py)

```python
# cc_harness/repl.py:ReplState (新增字段)

@dataclass
class ReplState:
    # ... 既有字段 ...
    decomposition_rejected: bool = False      # E1 D2:本轮 reject 标记
    last_decomp_todo_ids: list[str] = field(default_factory=list)  # E1 D2:本轮建的 todo ids
    last_decomp_summary: str | None = None    # E1 D2:本轮 plan 摘要
    todo_service: TodoService | None = None   # E1 D2:reject 时 cancel todo 用
```

```python
# cc_harness/repl.py:_handle_slash (新增 /reject)

elif cmd in ("reject", "r"):
    if not state.last_decomp_summary:
        print_warn("当前没有分解计划可 reject")
        return True
    state.decomposition_rejected = True
    for tid in state.last_decomp_todo_ids:
        try:
            if state.todo_service:
                await state.todo_service.update(tid, status="cancelled")
        except Exception:
            pass
    state.last_decomp_summary = None
    state.last_decomp_todo_ids = []
    print_info("已 reject 当前分解;LLM 继续走直接做路径")
    return True
```

### 组件 7:user 摘要(plan summary,agent.py + repl.py)

```python
# cc_harness/agent.py:run_turn (iter=0 + todo_create 后调一次)

def _print_decomp_summary(new_todos: list[TodoTask]) -> None:
    """E1 D2:user 第 1 轮看到 2-3 行 plan 摘要。"""
    from cc_harness.render import print_info
    lines = [f"📋 计划:分解为 {len(new_todos)} 个 sub-task"]
    for i, t in enumerate(new_todos[:5], 1):
        crit = t.acceptance_criteria[0] if t.acceptance_criteria else "(无)"
        lines.append(f"  [{i}] {t.title} — {crit[:80]}")
    if len(new_todos) > 5:
        lines.append(f"  ... +{len(new_todos) - 5} more")
    lines.append("  (/reject 中断)")
    print_info("\n".join(lines))
```

调用点:`run_turn` 在 `todo_create_handler` 返 `is_error=False` 后 + `mode == "coding"` 时调。

### 组件 8:policy.yaml kill-switch(policy.py)

```python
# cc_harness/policy.py:PolicyConfig (新增字段)

@dataclass
class PolicyConfig:
    # ... 既有字段 ...
    e1_decompose_enabled: bool = True  # E1 D7:kill-switch
```

`main.py:boot()` 读 policy.yaml 时透传;agent.run_turn 收到 `e1_decompose_enabled=False` 时不注入 `_decomposition_hint` section。

## 接口规格

### `e1_decompose_hint` extra_ctx(agent → prompts)

| 输入 | 类型 | 说明 |
|---|---|---|
| `e1_decompose_hint` | `bool` | iter==0 时 True,其余 False;kill-switch off 时 False |
| `iter_count` | `int` | 当前 turn 序号(用于 _decomposition_hint 自检) |

### `todo_create_handler` 校验

| 输入 | 规则 | 失败响应 |
|---|---|---|
| `acceptance_criteria` | `list[str]`, 长度 ∈ [1, 5] | `is_error=True` + TodoError 提示重写 |

### `SubAgentRunner.run()` retried 形参

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `retried` | `bool` | `False` | True 时不再 retry |

返回值不变(`SubAgentResult`),status 字段反映最终状态。

### `dispatch_subagent_handler` callback 形参

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `progress_cb` | `async (task_id, status, detail) -> None` | `None`(走默认 `print_info`) | per-subagent 进度 callback |
| `failure_pause_cb` | `async (SubAgentResult) -> "continue"\|"retry"\|"abort"` | `None`(不 pause) | 失败 pause 决策 callback |

## 测试策略

### Unit tests

| 测试目标 | 文件 | 关键 case |
|---|---|---|
| `_decomposition_hint` 渲染 | `tests/test_prompts.py` | iter=0 注入 / iter≥1 不注入 / mode≠coding 不注入 / kill-switch off |
| `_refresh_system_prompt` e1 ctx 注入 | `tests/test_agent.py` | iter=0 e1_decompose_hint=True / iter=1 False |
| `todo_create_handler` 校验 | `tests/test_project_tools.py` | 0 criteria → 拒 / 6 criteria → 拒 / 1-5 → pass |
| `SubAgentRunner.run` auto retry | `tests/test_d1_subagent.py` | failed → retry 1 次 / retry 后仍 failed → 不再 retry / done 不 retry / retried=True 直接走完 |
| `dispatch_subagent_handler` 实时进度 | `tests/test_d1_subagent.py` | progress_cb 触发顺序 / 失败 pause_cb 调起 |
| `/reject` slash 命令 | `tests/test_repl.py` | reject 成功 → flag set + todo cancelled / 无 decomp 时 warn |

### Integration tests

| 测试 | 文件 |
|---|---|
| decomp hint 注入 → LLM 决策 todo_create + dispatch_subagent 全链路 | `tests/test_e1_integration.py` |
| auto retry 走完 → summary 聚合 → 主 agent 下一轮决策 | `tests/test_e1_integration.py` |
| user /reject 中断 → LLM 走直接做路径 | `tests/test_e1_integration.py` |

### E2E(真 LLM,gated)

| 测试 | 文件 |
|---|---|
| 写真 LLM 跑 "实现 X + Y + Z" → 自动分解 + fan-out + 完成 | `tests/_test_e1_e2e.py`(双 `OPENAI_API_KEY` + `EMBEDDING_API_KEY` env 守卫) |

## 风险

- **M2 turn_idx 同款问题**:第 1 轮触发后,后续轮若 user 续问新任务,LLM 应重新评估(每个 user turn iter=0)。`iter_count` 在 run_turn 内重置?需验证(预计:每个 user turn 入口 `iter_count = 0`,不是跨 turn 累加)。
- **dispatch_subagent 实时进度 callback**:callback 在 REPL 模式下用 `print_info` 渲染,可能与主 agent 的 `print_thought` / `print_action` 输出交错;需节流(≤1/s)。
- **`/reject` 时机**:user 看到 plan 摘要到打 `/reject` 之间,主 agent 可能已经在跑(并发起 subagent);reject 只能 cancel todo,不能中断已在跑的 subagent。**subagent 跑完才 cancel**(异步)。
- **auto retry 的 clean messages**:重派时 `messages` 是新的,subagent 没有上次失败上下文。transient 失败可解,但 persistent bug 二次失败同样的事。E2 reflection 仍触发(失败状态记录)。
- **`todo_create_handler` 校验改回归**:当前 E2/E4 测试可能用 0 criteria 的 todo(占位 / 测试 fixture);需 inspect test_d1_*.py / test_b_*.py / test_c_*.py / test_d_*.py。

## 不做(YAGNI)

- ❌ 不引入 `DecomposerService` 子包(方案 β 否决,Q5/Q7 决策下没必要)
- ❌ 不引入 `DecompositionPolicy` protocol(方案 γ 否决,Q7 选 A 不留 override)
- ❌ 不改 SubAgentRunner 核心(只加 retry 1 次)
- ❌ 不接 L5 / L2 / reflection 任何新逻辑(沿用已有路径)
- ❌ 不引入 plan 阶段(Q1 否决)
- ❌ 不加 over-decomposition 硬护栏(Q7 否决)
- ❌ 不做 failure 回灌主 agent(C 留 post-merge)
- ❌ 不改 `_render_subagent_summary`(Q5-B 仍走 A 聚合)
- ❌ 不改 `e2 reflection` 任何代码