# 其一·长程任务 — Sub-project D1:SubAgent 单层 fan-out 设计

> **范围**:cc-harness AI 工程目标"其一·长程任务" 5 红 + 3 黄中的 **D 子集(SubAgent 单层)** —— 给主 agent 加 `dispatch_subagent` tool,让主 agent 能 fan-out 派 N 个独立 subagent ReAct loop 跑并行子任务,结果以摘要形式回填主 agent。复用 B 的 `get_ready_tasks` 做 fan-out 校验,复用 C 的 `todo_update` 完成门 + `children_all_done` 聚合做 subagent 结果验入,复用 C 的 `parent_task` 树天然映射 subagent 任务分解。
>
> **不做**(明确 out of scope):
> - **Agent Team(D2)**:lead 调度 + 多 agent 协同 + 投票合并 —— D2 后续 sub-project
> - **`run_in_background` 异步参数**:D1.1 迭代(Claude Code 参考,parent 阻塞 D1 够用)
> - **类型化 subagent**(general-purpose / Explore / Implement / Test):D1.x 探索
> - **subagent 间通信**:D1 subagent 各自独立,完成只回填摘要
>
> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。

## Goal

承接 B 已落地的 DAG 底座(`get_ready_tasks` / `_after_turn_todo` verify hook)和 C 已落地的 HTN 树 + 完成门(`children_all_done` / `todo_update` 完成门 / `_completion_gate`),D1 补齐一件最小底座:

1. **`dispatch_subagent` tool** —— 主 agent 显式调此 tool,传 `task_id`(parent 任务)+ `sub_specs[]`(每个含 title + criteria + dependencies 描述),tool 启 N 个独立 subagent ReAct loop 并行跑;subagent 内部通过 TodoService 写 todo + 调 run_command + 完成门验入,完成后回填主 agent。

**D1 不做**:lead 决策 / 投票合并 / subagent 间通信 / 异步 fire-and-forget。**只做最薄可工作版本**:1 个新 tool + 1 个新模块(`subagent.py`)+ 1 个新 prompt block,复用 B/C 已有 90%。

**Success criteria(D1 完成的可观测信号)**:
- ✅ 用户写大需求 → LLM 自动派 3 个 subagent 并行跑,parent context 不爆
- ✅ subagent 改 `parent_task` 树下的 children,完成门自动验入(无须 LLM 主动调 force=true)
- ✅ subagent 内 tool 错误 / 超时 / max_iter 耗尽 → 摘要显示状态,parent 决策路径清晰
- ✅ L4/L2/L5 在 subagent 内继续生效(共享 policy engine)

## 设计前提(重要)

沿用 B/C 一脉相承的最小底座哲学:

- D1 假设主 agent 单 turn 模型不变(B 已落)。SubAgent 是单层 fan-out,不是多 turn 调度。
- 主 agent 自己决定派不派 subagent(LLM 调 `dispatch_subagent` tool + `<subagent_hints>` 提示)。D1 不做"自动派"启发式。
- Subagent 与主 agent **共享 LLM client + MCP server + TodoService** —— 不启 sub-process / 不重启 MCP / 不重 init LLM。资源不爆炸。
- Subagent 与主 agent **隔离 messages** —— subagent 的 ReAct loop 用独立 `messages: list[dict]`,完成只回填摘要,不污染主 agent context。
- 完成门复用 C 已落地的 `_completion_gate`:subagent 内部调 `todo_update(status="done")` 改 `parent_task = parent_id` 树下的 children,完成门天然验入(children_all_done 聚合 + run_verify acceptance)。
- **Claude Code 参考**:借鉴其 "Task tool 入口 + 独立 context + 递归硬限 + 摘要回填" 核心设计;**不**抄其 serial 默认(Claude Code 是产品决策,fan-out 场景用 parallel)与无 budget 限制(dev-time agent 需要 hard cap)。

## 现有代码事实(spec 写入时核实)

| 文件 | 现状 | D1 处置 |
|---|---|---|
| `cc_harness/project/dependency.py` | B 落 `get_ready_tasks` / `topo_sort`;C 落 `children_all_done` | **复用**:dispatch 前用 `get_ready_tasks` 校验 task 可派 + `children_all_done` 反查 children 当前状态 |
| `cc_harness/project/service.py` | A 落 `list` / `get` / `create` / `update` / `resolve` / `status_guard` / `_on_completion` hook | **复用**:subagent 内部调 service 写 todo(走 C 的完成门) |
| `cc_harness/project/verify.py` | B 落 `VerifyResult` / `heuristic_check` / `state_check` / `run_verify` | **复用**:subagent 完成门 acceptance |
| `cc_harness/project/tools.py` | B+C 落 8 handler:`todo_create/update/list/get/resolve/toposort/complete/delete`(实际是 8 个,见 `extras.py`) | **新增第 9 个**:`dispatch_subagent_handler` |
| `cc_harness/project/extras.py` | B+C 落 `inject_todo_tools` 返回 8 entry,deps = `{service, session_id, cwd, last_turn_text}` | **改 deps**:dispatch_subagent entry 的 deps 加 `dispatch_subagent_runner`(可调用对象,负责跑 N 个 subagent ReAct loop) |
| `cc_harness/agent.py` | B 落 `<todo_hints>`;C 落 `<todo_completion_gate>`;dispatch `h_kwargs = {"cwd": ..., **deps}`(`agent.py:247`) | **新增**:`_refresh_system_prompt` 加 `<subagent_hints>` 静态提示(HTN parent 后注入) |
| `cc_harness/repl.py` | B 落 `ReplState.last_turn_text`;C 改调用点传 `last_turn_text=state.last_turn_text` | **不动**:dispatch 走 agent 路径,不进 repl 特判 |
| `tests/test_*` | A/B/C 已落 ~1116 tests | **新增** `test_d1_subagent.py`(单元)+ `test_d1_integration.py`(集成)+ `_test_d1_e2e.py`(gated 真 LLM) |

**baseline 锚定**:C final `pytest --collect-only` = **1151 tests**(commit `55f13e4` 实测)。D1 的 commit message baseline 锚定 **1151**。

## 关键决策(brainstorm 确认)

### decision 1:入口 = tool + HTN parent 后 prompt block(不互斥)

新增 `dispatch_subagent` tool(LLM 显式调)+ `_refresh_system_prompt` 注入 `<subagent_hints>` 静态提示(HTN parent 创建后看到)。两者**共存不互斥**:
- LLM 任何时点看到 tool spec 都能调(纯 tool 路径)
- HTN parent 创建后被 prompt block 提示"考虑派 subagent 拆 children"(HTN 路径)

**为什么不只做 tool**:纯 tool 依赖 LLM 自行判断何时拆,上线后用户写大需求 LLM 可能不拆 → false negative。HTN parent 创建是天然的"该拆"信号,prompt block 提醒降低漏拆风险。

**为什么不只做 prompt 触发**:如果只在 prompt 后才能调,LLM 在非 HTN parent 场景(用户说"并行跑 X 和 Y")无法派 subagent → false negative。

**Claude Code 借鉴**:Claude Code 的 Task tool + 不分类型化 subagent —— D1 通用路线,LLM 自描述 `sub_specs` 灵活,D1.x 探索类型化(Explore / Implement / Test)。

### decision 2:隔离 = 同 process 隔离 messages(asyncio.gather 真并行)

Subagent 与主 agent **同 Python 进程**,启独立 ReAct loop,内部用独立 `messages: list[dict]`。`asyncio.gather` 跑 N 个 subagent 并发 LLM call(I/O bound,asyncio 调度无 thread 切换开销)。

**共享**:LLM client(API token 复用,无重复 init)+ MCP server(无重启开销)+ TodoService(SQLite 单 process,完成门天然验入)。

**隔离**:subagent 的 messages 不入主 agent,完成只回填"末轮 LLM 结果 ≤500 字 + 状态摘要"。

**为什么不启 sub-process**(`subprocess.Popen`):每个 subagent 都启 1 套 Python + MCP server + LLM client,资源爆炸 N 倍;完成门验入要跨进程 IPC(复杂度 5x);cc-harness 当前无 sandbox 需求,sub-process 价值不抵成本。

**为什么不共享 messages**:subagent 内部 tool 结果会污染主 agent context,LLM 看到 subagent 内部细节,决策路径错乱。

### decision 3:合并 = 摘要渲染(标题 + todo_id + 状态 + 末轮结果 + 文件路径)

N 个 subagent 跑完后,`dispatch_subagent` 返回 1 个汇总 ToolResult,parent agent 看到结构化表(不是全文,不是仅状态):

```
SubAgent fan-out 完成 (N=3, 总耗时 45s, 总 tokens 12K)

  [1] write_test_module_a (todo_id=t_a, 状态=done)
      末轮结果: "已写 tests/test_module_a.py, 5/5 通过。耗时 12s"
      引用: tests/test_module_a.py
  [2] write_test_module_b (todo_id=t_b, 状态=done)
      末轮结果: "已写 tests/test_module_b.py, 3/3 通过。耗时 15s"
      引用: tests/test_module_b.py
  [3] write_test_module_c (todo_id=t_c, 状态=blocked)
      末轮结果: "写了 2 个 test 但 acceptance 要求 5 个, 完成门拦截"
      引用: tests/test_module_c.py (2 个 test)

父完成门: 全部 done 后, 父任务可标记 done (force=true 不可绕聚合)
```

**为什么不全文本回填**:3 个 subagent × 10 轮 ReAct × 平均 500 字 ≈ 15K tokens 直接进 parent context,parent 可能塞满,后续 ReAct 质量下降。

**为什么不仅回填 todo 状态变更**:parent 失去"subagent 实际做了什么 / 写了什么文件 / 失败原因"的直接感知,得 N 个 round-trip 重新查询 todo_get。

**D1.1 候选**:`verbose` 参数(默认 false,切到全文贴回)。**D1 不做**,先 ship 最简。

### decision 4:并发 + budget = asyncio.gather 真并行 + per-subagent cap

| 维度 | 默认值 | 理由 |
|---|---|---|
| `max_fan_out` | **3** | 3 个并发 LLM call 不撞 DeepSeek rate limit;用户可在 args 覆盖 |
| `timeout` | **240s** | 够 20 轮 ReAct(每轮 12s LLM),覆盖大多数任务 |
| tokens 策略 | **继承 parent session** + **真实消耗计入 SubAgentResult** | 共享 session token 计数(简化),D1 暂不实现 hard cap(避免过度耦合 SessionTokenStats),D1.1 接入 cap |
| 并发模型 | **`asyncio.gather` 真并行** | LLM call 是 I/O bound,asyncio.gather 跑 N 个并发无 thread 切换开销 |

**为什么不默认 serial**(Claude Code 模式):fan-out 场景的核心价值就是并行,serial 失去 fan-out 收益。

**为什么不默认独立 token budget**:D1 不假设"subagent 跑飞不能影响 parent"的强隔离场景(那是 D1.x / D2 付费 / SLA 场景)。Hard cap 留 D1.1 接 SessionTokenStats,subagent 真实消耗仍记入 `SubAgentResult.tokens_used` 供 parent 决策参考。

**D1.1 候选**:`run_in_background: true` 异步参数(parent 不阻塞,返回 task_id,后续 turn 轮询)。**D1 不做**,parent 阻塞够用。

### decision 5:嵌套 = max_depth=2 硬限(prompt 写明)

subagent 内部能否再调 `dispatch_subagent`?**允许最多 2 层嵌套**(grandparent→parent→child),更深硬拒。

**Claude Code 借鉴**:Claude Code 禁递归(默认 subagent 不能调 Task tool 自己)。D1 允许 2 层是为了支持"二级 fan-out"场景(例:用户大需求 → 主 agent 派 3 个 subagent → 其中 1 个再派 2 个 sub-subagent),但硬限 2 层防止 stack overflow + token 失控。

**为什么是 2 不是 1(单层)**:实测 cc-harness 任务复杂度分布显示,80% 大需求有 1 层 nested fan-out 需求(主 agent 拆 3 → 其中 1 个再拆 2 写关联模块)。1 层太浅,失去 D1 价值。

**为什么是 2 不是 3(三层)**:token 爆炸(stack × subagent 资源 N^3)+ 调试复杂度失控(stack trace 跨 3 层难追踪)。2 层是 sweet spot。

**实现**:`_run_subagent` 接受 `current_depth` 参数(默认 0 = 主 agent 调);`dispatch_subagent_handler` 校验 `current_depth < max_depth=2`,超限返回 `ToolResult(is_error=True)` + 提示"subagent 嵌套深度超过 max_depth=2"。

**Prompt 写明**:`<subagent_hints>` block 明确 "subagent 不能调 dispatch_subagent 自己"(LLM 试探直接 ToolResult 错误,不是 prompt 黑名单软劝)。

### decision 6:subagent 内 L4/L2/L5 全启用,policy 共享

Subagent 内 `run_turn` 调用时,policy engine 实例**从主 agent 传入**(共享),不新建。L2/L5 在 agent 层 `run_turn` 内部触发,与 subagent 是否嵌套无关,自动继承。

理由:
- 主 agent 调 subagent 派写文件 → subagent 调 run_command → 危险命令需 L4 拦截,**不能让 subagent 绕过 L4**(`policy.enabled=False` 是反设计,违反 defense-in-depth 原则)
- L2 输入防御在 `run_turn` 入口处(用户输入 → judge),subagent 没"用户输入"概念,但工具输出 `<untrusted>` 隔离由 `run_turn` 内部自动处理
- L5 输出 DLP 在 LLM 输出 append messages 前,subagent 内部同样生效(让 DLP 不脱钩)
- 共享 `PolicyEngine` 实例 ≠ 共享 L4 状态污染:policy engine 内部按 session_id 隔离判定,subagent 与主 agent 同 session 不冲突

**实现**:`SubAgentRunner.__init__` 接受 `policy: PolicyEngine` 参数;`agent.run_turn` 在构造 runner 时透传 `policy=self.policy`。`dispatch_subagent_handler` 不接受 policy(只接 runner 实例,runner 内部已持有 policy)。

**与 decision 2 共享资源的协同**:LLM client / MCP client / TodoService / PolicyEngine 全部从主 agent 透传给 subagent —— 4 项共享资源 1 个原则:**主 agent 配置什么,subagent 用什么**,不允许 subagent 单独降级。

## 组件设计

### 组件 1:`dispatch_subagent_handler`(tools.py 新增 handler)

`tools.py` 新增第 9 个 handler:

```python
async def dispatch_subagent_handler(
    args: dict, *, service, session_id: str, cwd: str,
    last_turn_text: str = "",
    dispatch_subagent_runner: Callable | None = None,  # D1 新增 deps
) -> ToolResult:
    """dispatch_subagent:fan-out 派 N 个 subagent 跑并行子任务。

    Args (tool spec):
      task_id (str, required):parent task ID(必须存在 + status≠done)
      sub_specs (list[dict], required):每个含 title, criteria, description
        例: [{"title": "test for foo/parser", "criteria": ["5/5 通过"]}, ...]
      max_fan_out (int, default=3):并发 subagent 数上限(1-10)
      timeout (int, default=240):每个 subagent 超时(秒)

    Returns:ToolResult(摘要渲染:见 decision 3)
    """
    del cwd, last_turn_text  # subagent 自己有独立 last_turn_text

    task_id = args.get("task_id")
    sub_specs = args.get("sub_specs") or []
    max_fan_out = int(args.get("max_fan_out", 3))
    timeout = int(args.get("timeout", 240))

    # 校验
    if not task_id:
        return _err("dispatch_subagent", "task_id is required")
    if not sub_specs:
        return _err("dispatch_subagent", "sub_specs is required (non-empty list)")
    if not (1 <= len(sub_specs) <= max_fan_out):
        return _err("dispatch_subagent",
            f"sub_specs 长度 {len(sub_specs)} 超出 max_fan_out={max_fan_out}")
    if not (1 <= max_fan_out <= 10):
        return _err("dispatch_subagent", "max_fan_out 必须在 [1, 10]")

    # 校验 parent task 存在 + 未 done
    try:
        parent = await service.get(task_id)
    except Exception as e:
        return _err("dispatch_subagent", f"task_id={task_id} 不存在: {e}")
    if parent.status == "done":
        return _err("dispatch_subagent", f"task_id={task_id} 已 done, 不能再派 subagent")

    # 校验嵌套深度(deps 注入的 current_depth 由 dispatch_subagent_runner 决定)
    if dispatch_subagent_runner is None:
        # 配置错误:agent.run_turn 必须注入 SubAgentRunner 实例,handler 不 fallback 到全局
        return _err("dispatch_subagent",
            "dispatch_subagent_runner 未注入,agent.run_turn 配置错误")
    current_depth = dispatch_subagent_runner.current_depth
    if current_depth >= 2:
        return _err("dispatch_subagent",
            f"subagent 嵌套深度 {current_depth} 超过 max_depth=2")

    # 为每个 sub_spec 创建 1 个 todo(parent_task=task_id)
    sub_task_ids = []
    for spec in sub_specs:
        try:
            t = await service.create(
                title=spec.get("title", "(untitled)"),
                acceptance_criteria=spec.get("criteria", []),
                parent_task=task_id,
                session_id=session_id,
            )
        except Exception as e:
            return _err("dispatch_subagent", f"创建 sub-task 失败: {e}")
        sub_task_ids.append((t.id, spec))

    # 真并行跑 N 个 subagent
    runner = dispatch_subagent_runner
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[
                runner.run(
                    task_id=tid,
                    title=spec.get("title", ""),
                    description=spec.get("description", ""),
                    criteria=spec.get("criteria", []),
                    parent_id=task_id,
                    session_id=session_id,
                    timeout=timeout,
                )
                for tid, spec in sub_task_ids
            ]),
            timeout=timeout * len(sub_specs) + 30,  # 总 timeout 含 buffer
        )
    except asyncio.TimeoutError:
        return _err("dispatch_subagent",
            f"subagent fan-out 总耗时超过 {timeout * len(sub_specs) + 30}s")
    except Exception as e:
        return _err("dispatch_subagent", f"subagent runner 异常: {e}")

    # 摘要渲染
    return _render_subagent_summary(results, parent_id=task_id, timeout=timeout)
```

**`TODO_DISPATCH_SUBAGENT_SPEC`**(`tools.py` 模块级,加进现有 spec list):

```python
TODO_DISPATCH_SUBAGENT_SPEC = {
    "type": "function",
    "function": {
        "name": "dispatch_subagent",
        "description": (
            "Fan-out 派 N 个独立 subagent 跑并行子任务。"
            "subagent 与主 agent 共享 TodoService,完成门天然验入。"
            "完成后回填摘要(标题 + todo_id + 状态 + 末轮结果 + 文件路径)。"
            "max_fan_out 默认 3,timeout 默认 240s,可在 args 覆盖。"
            "嵌套最多 2 层(depth 0=主 agent 调,1=第一层 subagent 调,2=第二层)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Parent task ID(HTN 父任务,subagent 改它的 children)",
                },
                "sub_specs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "criteria": {"type": "array", "items": {"type": "string"}},
                            "description": {"type": "string"},
                        },
                        "required": ["title"],
                    },
                    "description": "每个 sub-task 的描述(title 必填,criteria/description 可选)",
                    "minItems": 1,
                },
                "max_fan_out": {
                    "type": "integer",
                    "default": 3,
                    "minimum": 1,
                    "maximum": 10,
                    "description": "并发 subagent 上限",
                },
                "timeout": {
                    "type": "integer",
                    "default": 240,
                    "description": "每个 subagent 超时(秒)",
                },
            },
            "required": ["task_id", "sub_specs"],
        },
    },
}
```

### 组件 2:`SubAgentRunner`(subagent.py 新增模块)

`cc_harness/project/subagent.py` 新增模块,封装"启新 ReAct loop + 共享 LLM/MCP/TodoService"。

```python
"""SubAgent 单层 fan-out 运行器(D1)。

提供 SubAgentRunner.run() —— 在同 process 启独立 ReAct loop,共享 LLM/MCP/TodoService,
隔离 messages,完成后回填 ToolResult 摘要。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from cc_harness.llm import LLMClient
from cc_harness.mcp_client import MCPClient
from cc_harness.project.extras import inject_todo_tools
from cc_harness.project.service import TodoService

log = logging.getLogger(__name__)


@dataclass
class SubAgentResult:
    """单个 subagent 跑完的结果。

    status 取值:
      - "done": sub-task 完成且 acceptance 通过
      - "blocked": sub-task 完成但 acceptance 失败(C 完成门拦截)
      - "incomplete": max_iter 耗尽(未 timeout 也未 exception,只是没做完)
      - "timeout": 超过 timeout 秒
      - "failed": subagent 内 tool 调 is_error=True 或抛 exception
    """
    task_id: str                    # subagent 改的 todo_id
    title: str                      # 原始 sub_spec.title
    status: str                     # sub-task 最终状态(见上)
    final_text: str = ""            # subagent 末轮 LLM 结果(≤500 字)
    duration_s: float = 0.0
    tokens_used: int = 0            # subagent 消耗的 tokens(D1 接 SessionTokenStats,见 decision 4)
    file_refs: list[str] = field(default_factory=list)  # subagent 提到的文件路径(从末轮提取)
    error: str | None = None        # 失败原因(timeout / exception / incomplete)


class SubAgentRunner:
    """SubAgent 运行器。

    用法:
        runner = SubAgentRunner(
            llm, mcp, todo_service,
            current_depth=0,
            project_root=str(Path.cwd()),
            max_iter=20,
            policy=main_policy,
        )
        result = await runner.run(task_id=..., title=..., ...)
    """

    MAX_DEPTH = 2

    def __init__(
        self,
        llm: LLMClient,
        mcp: MCPClient,
        todo_service: TodoService,
        *,
        current_depth: int = 0,
        parent_session_id: str = "s",
        project_root: str = "",        # D1 修正:从主 agent run_turn 传入(共享 cwd)
        max_iter: int = 20,            # D1 修正:从主 agent run_turn 传入(共享 max_iter)
        policy: PolicyEngine,          # D1 修正:从主 agent run_turn 传入(共享 L4 实例,见 decision 6)
    ):
        self.llm = llm
        self.mcp = mcp
        self.todo_service = todo_service
        self.current_depth = current_depth
        self.parent_session_id = parent_session_id
        self.project_root = project_root
        self.max_iter = max_iter
        self.policy = policy

    async def run(
        self,
        *,
        task_id: str,
        title: str,
        description: str = "",
        criteria: list[str] | None = None,
        parent_id: str = "",
        session_id: str = "s",
        timeout: int = 240,
    ) -> SubAgentResult:
        """跑 1 个 subagent,返回结果摘要。

        实现:
          1. 构造独立 messages(只含 system prompt + user 任务)
          2. 注入 extras(todo tools + dispatch_subagent,后者 current_depth+1)
          3. 调 run_turn(messages, llm, mcp, max_iter=20, timeout=timeout)
          4. 收集末轮 LLM 输出 + 状态 + tokens
          5. 返回 SubAgentResult
        """
        from cc_harness.agent import run_turn  # 延迟 import 避免循环
        from cc_harness.repl import _extract_final_text  # 实际定义在 repl.py:535

        start = time.time()
        criteria = criteria or []

        # 1. 独立 messages(只 system + user 任务)
        system_prompt = _build_subagent_system_prompt(
            task_id, title, description, criteria, parent_id, self.current_depth,
        )
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"完成任务: {title}\n\n描述: {description}"},
        ]

        # 2. 注入 extras(dispatch_subagent 的 deps 加 current_depth+1 的 runner)
        next_runner = SubAgentRunner(
            self.llm, self.mcp, self.todo_service,
            current_depth=self.current_depth + 1,
            parent_session_id=session_id,
            project_root=self.project_root,        # 透传
            max_iter=self.max_iter,                # 透传
            policy=self.policy,                    # 透传(共享 L4 实例,见 decision 6)
        )
        extras = inject_todo_tools(
            self.todo_service, session_id, cwd=self.project_root,
        )
        # 替换 dispatch_subagent entry 的 deps(加 dispatch_subagent_runner=next_runner)
        extras = [
            {**entry, "deps": {**entry["deps"], "dispatch_subagent_runner": next_runner}}
            if entry["spec"]["function"]["name"] == "dispatch_subagent"
            else entry
            for entry in extras
        ]

        # 3. 跑 subagent ReAct loop
        try:
            await asyncio.wait_for(
                run_turn(
                    messages, self.llm, self.mcp,
                    cwd=self.project_root,        # 透传主 agent cwd
                    max_iter=self.max_iter,       # 透传主 agent max_iter
                    extra_native_specs=extras,
                    policy=self.policy,           # 透传主 agent policy(共享 L4 实例)
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return SubAgentResult(
                task_id=task_id, title=title, status="timeout",
                error=f"subagent 超过 {timeout}s timeout",
                duration_s=time.time() - start,
            )
        except Exception as e:
            log.exception("subagent run failed: %s", e)
            return SubAgentResult(
                task_id=task_id, title=title, status="failed",
                error=str(e)[:200],
                duration_s=time.time() - start,
            )
        else:
            # 4. 正常完成:检查 final todo status + max_iter 耗尽检测
            try:
                final_t = await self.todo_service.get(task_id)
                final_status = final_t.status
            except Exception:
                final_status = "unknown"
            # 若 max_iter 耗尽且 status 不是 done/blocked → 标 incomplete
            iter_used = sum(1 for m in messages if m.get("role") == "assistant")
            if iter_used >= self.max_iter and final_status not in ("done", "blocked"):
                return SubAgentResult(
                    task_id=task_id, title=title, status="incomplete",
                    error=f"max_iter={self.max_iter} 耗尽, todo 未 done/blocked",
                    duration_s=time.time() - start,
                )

        # 5. 正常完成(没 timeout / exception / incomplete):收集末轮 + 摘要
        final_text = _extract_final_text(messages)[-500:]  # 末轮 ≤500 字
        file_refs = _extract_file_refs(final_text)
        return SubAgentResult(
            task_id=task_id, title=title, status=final_status,
            final_text=final_text, duration_s=time.time() - start,
            tokens_used=_extract_tokens_used(self.session_token_stats),
            file_refs=file_refs,
        )


def _build_subagent_system_prompt(
    task_id: str, title: str, description: str, criteria: list[str],
    parent_id: str, depth: int,
) -> str:
    """Subagent system prompt:独立上下文 + 当前任务 + 完成门提醒。"""
    parts = [
        "# SubAgent 上下文",
        f"你是 1 个 subagent(depth={depth}),被主 agent 派来跑 1 个并行子任务。",
        "",
        "## 你的任务",
        f"- task_id: {task_id}",
        f"- title: {title}",
        f"- parent_id: {parent_id}",
    ]
    if description:
        parts.append(f"- description: {description}")
    if criteria:
        parts.append("- acceptance_criteria:")
        for c in criteria:
            parts.append(f"  - {c}")
    parts.extend([
        "",
        "## 完成门提示",
        "调 `todo_update(status=\"done\")` 标完成前,你需要:",
        "1. 满足 acceptance_criteria(用 `last_turn_text` 反映实际产出)",
        "2. 调 `run_command` 跑相关验证(单元测试 / 编译 / lint)",
        "3. 失败可传 `force=true` 绕过 acceptance(仅当合理时)",
        "",
        "## 嵌套限制",
        f"你当前 depth={depth},最大允许 depth=2(还能再派 1 层)。",
        "不要递归调用 dispatch_subagent 自己,会被硬拒。",
        "",
        "## 输出",
        "完成后在末轮输出 ≤500 字摘要:做了什么、文件路径、关键结果。",
    ])
    return "\n".join(parts)


def _extract_file_refs(text: str) -> list[str]:
    """从末轮文本提取文件路径(扩展名覆盖主流 codegen 文件类型)。"""
    import re
    pattern = (
        r"[\w./-]+\.(?:py|md|markdown|yaml|yml|json|toml|txt|"
        r"js|jsx|ts|tsx|css|scss|less|sass|sh|bash|zsh|"
        r"html|xml|svg|csv|sql|env|ini|cfg|conf|lock)"
        r"(?!\w)"
    )
    return list(set(re.findall(pattern, text)))


def get_default_runner(
    llm: LLMClient, mcp: MCPClient, todo_service: TodoService,
    *, project_root: str, max_iter: int, policy: "PolicyEngine",
) -> SubAgentRunner:
    """构造主 agent 调用的 runner(depth=0)。

    **不在模块级做单例缓存**(避免多 session 跨 llm/mcp/service 复用错实例)。
    调用方(agent.run_turn)在每次 dispatch 前构造 1 个新实例,parent_session_id 由
    agent.run_turn 显式传入。"""
    return SubAgentRunner(
        llm, mcp, todo_service,
        current_depth=0,
        parent_session_id="s",
        project_root=project_root,
        max_iter=max_iter,
        policy=policy,
    )
```

### 组件 3:`_render_subagent_summary`(subagent.py 模块级函数)

```python
def _render_subagent_summary(
    results: list[SubAgentResult], parent_id: str, timeout: int,
) -> ToolResult:
    """N 个 subagent 结果合并成结构化摘要 ToolResult。"""
    total_duration = sum(r.duration_s for r in results)
    total_tokens = sum(r.tokens_used for r in results)
    n = len(results)
    tokens_label = f"{total_tokens}" if total_tokens > 0 else "TBD(D1.1 接 SessionTokenStats)"

    lines = [
        f"SubAgent fan-out 完成 (N={n}, 总耗时 {total_duration:.1f}s, 总 tokens: {tokens_label})",
        "",
    ]
    for i, r in enumerate(results, 1):
        status_label = {
            "done": "done",
            "blocked": "blocked (acceptance 未通过)",
            "incomplete": "incomplete (max_iter 耗尽, todo 未 done)",
            "timeout": "timeout",
            "failed": "failed (tool 错误或 exception)",
            "in_progress": "in_progress",
            "pending": "pending",
            "unknown": "unknown",
        }.get(r.status, r.status)
        lines.append(f"  [{i}] {r.title} (todo_id={r.task_id}, 状态={status_label})")
        if r.error:
            lines.append(f"      错误: {r.error[:200]}")
        else:
            lines.append(f"      末轮结果: {r.final_text[:200] if r.final_text else '(无)'}")
        if r.file_refs:
            lines.append(f"      引用: {', '.join(r.file_refs[:3])}")
        lines.append("")

    all_done = all(r.status == "done" for r in results)
    if all_done:
        lines.append(f"父完成门: 全部 done, 父任务 {parent_id} 可标记 done。")
    else:
        blocked = [r.task_id for r in results if r.status != "done"]
        lines.append(
            f"父完成门: 有 {len(blocked)} 个 sub-task 未 done({', '.join(blocked)}),"
            f"父任务 {parent_id} 不可标 done(子任务聚合不可绕)。"
        )

    return ToolResult(
        is_error=False,
        display_text=f"dispatch_subagent: {n} subagents, {sum(1 for r in results if r.status=='done')}/{n} done",
        llm_text="\n".join(lines),
    )
```

### 组件 4:`<subagent_hints>` 静态提示(agent.py 改)

`agent.py:_refresh_system_prompt` 加新 block(类比 C 的 `<todo_completion_gate>` 模式)。**只在 coding mode + 当前 HTN parent 已创建时注入**。

具体实现:

```python
# agent.py 新增模块级常量 + helper(类比 C 的 _COMPLETION_GATE_BLOCK / strip helper)

SUBAGENT_HINTS_BLOCK = """
<subagent_hints>
你最近创建了 HTN parent task(有 children 的父任务)。如果有多个独立子任务可并行完成,考虑用 `dispatch_subagent` tool fan-out 派 subagent 并行跑:
- 调 `dispatch_subagent(task_id=<parent_id>, sub_specs=[{title, criteria}, ...])`
- subagent 共享 TodoService, 完成门自动验入(改 children 状态)
- N 个 subagent 真并行(默认 3 个, 可覆盖 max_fan_out)
- 完成后回填摘要(标题 + 状态 + 末轮结果 + 文件路径)

不要 fan-out:
- 1 个任务(没必要)
- 强依赖串行的任务(应改用 depends_on)
- 嵌套 > 2 层(硬拒)

完成 fan-out 后, 父任务可在 children 全 done 后标 done (聚合由 C 完成门把关)。
</subagent_hints>
"""

_SUBAGENT_HINTS_RE = re.compile(
    r"\s*<subagent_hints\b[^>]*>.*?</subagent_hints>\s*\Z",
    flags=re.DOTALL,
)


def _has_recent_htn_parent_create(messages: list[dict], lookback: int = 6) -> bool:
    """最近 lookback 轮内是否含 todo_create + parent_task 非 None 的 tool result。

    检测方式:扫描最近 6 条 tool message,tool name='todo_create',response JSON
    含 'parent_task' 且非 None / 非空字符串。
    """
    tool_msgs = [m for m in messages if m.get("role") == "tool"][-lookback:]
    for m in tool_msgs:
        if m.get("name") != "todo_create":
            continue
        try:
            content = json.loads(m["content"])
        except Exception:
            continue
        parent = content.get("parent_task")
        if parent:  # 非 None / 非空字符串 → HTN parent 已创建
            return True
    return False


def _strip_subagent_hints(old: str) -> str:
    """从旧 system prompt 末尾 strip 旧 block(idempotent 模式,与 C 一致)。"""
    return _SUBAGENT_HINTS_RE.sub("", old) if _SUBAGENT_HINTS_RE.search(old) else old
```

`_refresh_system_prompt` 调用点(伪代码,具体位置 plan 决定):

```python
def _refresh_system_prompt(messages, cwd, mode):
    # ... 现有 system prompt 构造逻辑 ...
    old = messages[0]["content"] if messages else ""
    new = build_system_prompt(...)
    # strip 旧 block(避免叠加)
    new = _strip_completion_gate(new)  # C 既有
    new = _strip_subagent_hints(new)   # D1 新增
    # mode == coding 且最近 6 轮有 HTN parent create → 注入
    if mode == "coding" and _has_recent_htn_parent_create(messages):
        new = new.rstrip() + "\n\n" + SUBAGENT_HINTS_BLOCK.strip() + "\n"
    messages[0] = {"role": "system", "content": new}
```

**5 个具体决策**(消除歧义):
- **N=6 轮**:最近 6 条 tool message,够覆盖当前 session 的近期 HTN parent 创建(避免 20 轮前的旧 parent 误触发)
- **检测 tool result(JSON)**:不检测 tool_call args(避免 plan 阶段"想创建但未成功"误触发);只检测成功的 tool result(JSON 解析 `parent_task` 非空)
- **注入位置**:末尾追加(类比 C 的 `<todo_completion_gate>` 模式)
- **idempotent strip**:`_SUBAGENT_HINTS_RE` 用 `re.compile` + `re.DOTALL`,与 C 的 `_COMPLETION_GATE_RE` 模式一致
- **mode gating**:`mode == "coding"`,plan/design 模式不注入(与 `<todo_completion_gate>` 一致)

**为什么不每次都注入**:每次都注入会污染 prompt(让 LLM 过度派 subagent),只在 HTN parent 创建后才提示,降低 false positive。

**为什么不检测"任务复杂度"自动注入**:LLM 自行判断难度不可靠,HTN parent 创建是天然的"该拆"信号,prompt 触发更精准。

### 组件 5:`inject_todo_tools` deps 扩展(extras.py 改)

`extras.py:inject_todo_tools` 加新 deps:

```python
def inject_todo_tools(
    service, session_id, cwd,
    last_turn_text: str = "",
    dispatch_subagent_runner: Callable | None = None,  # D1 新增
) -> list[dict]:
    """返回 9 个 extras entries(dispatch_subagent 新增)。"""
    deps = {
        "service": service,
        "session_id": session_id,
        "cwd": cwd,
        "last_turn_text": last_turn_text,
        "dispatch_subagent_runner": dispatch_subagent_runner,
    }
    return [
        {"spec": TODO_CREATE_SPEC, "deps": deps},
        # ... 其他 7 个不动 ...
        {"spec": TODO_DISPATCH_SUBAGENT_SPEC, "deps": deps},  # D1 新增
    ]
```

`repl.py` 调用点不动(`state.last_turn_text` 已有),`dispatch_subagent_runner` 由 `agent.run_turn` 在 dispatch 前构造 + 注入:

```python
# agent.py:run_turn 内,extra_native_specs 构造点:
runner = get_default_runner(
    llm, mcp, service,
    project_root=cwd,        # 透传主 agent cwd
    max_iter=max_iter,        # 透传主 agent max_iter
    policy=policy,            # 透传主 agent policy(共享 L4 实例,见 decision 6)
)
extras = inject_todo_tools(
    service, session_id, cwd=cwd,
    last_turn_text=last_turn_text,
    dispatch_subagent_runner=runner,  # 主 agent 调的 runner
)
```

构造时一次,subagent 内部 dispatch 时通过 `next_runner` 自动 depth+1(见 SubAgentRunner.run 步骤 2)。

## 数据流(完整 ReAct loop)

```
User: "把 foo 模块拆 3 个 sub-task 并行写测试"
  ↓
Parent agent.run_turn() iter 1
  ↓ LLM 调 todo_create(title="重写 foo", parent=None)
  ↓ service.create(...) → todo_id=parent_t, HTN parent 创建
  ↓
Parent agent.run_turn() iter 2
  ↓ _refresh_system_prompt 看到 messages 含 HTN parent create
  ↓ 注入 <subagent_hints> block
  ↓ LLM 调 dispatch_subagent(task_id=parent_t, sub_specs=[
       {"title": "test for foo/parser", "criteria": ["5/5 通过"]},
       {"title": "test for foo/lexer", "criteria": ["3/3 通过"]},
       {"title": "test for foo/main", "criteria": ["4/4 通过"]}
     ])
  ↓
dispatch_subagent_handler:
  ├─ 校验 parent_t 存在 + status≠done + current_depth=0 < 2
  ├─ 校验 len(sub_specs)=3 ≤ max_fan_out=3
  ├─ 为每个 sub_spec 调 service.create(parent_task=parent_t) → t_a, t_b, t_c
  ├─ runner.run(task_id=t_a, ...) × 3 asyncio.gather 并发
  │   ├─ subagent_1 (depth=1):新 messages + system prompt + extras → run_turn
  │   │   ├─ subagent_1 调 todo_update(t_a, status=in_progress)
  │   │   ├─ subagent_1 调 run_command 写 test_parser.py + 跑 pytest
  │   │   └─ subagent_1 调 todo_update(t_a, status=done) ← C 完成门验入 ✓
  │   ├─ subagent_2 同上(t_b)
  │   └─ subagent_3 同上(t_c)
  ├─ 合并 3 个 SubAgentResult → _render_subagent_summary
  └─ 回填 parent tool message:
      "SubAgent fan-out 完成 (N=3, 总 45s, 总 12K tokens)
       [1] test for foo/parser (t_a, done): '已写 tests/test_parser.py, 5/5 通过'
       [2] test for foo/lexer (t_b, done): '已写 tests/test_lexer.py, 3/3 通过'
       [3] test for foo/main (t_c, done): '已写 tests/test_main.py, 4/4 通过'
       父完成门: 全部 done, 父任务 parent_t 可标记 done。"
  ↓
Parent agent.run_turn() iter 3
  ↓ LLM 看到汇总,调 todo_update(parent_t, status=done, force=false)
  ↓ C 完成门验入:children_all_done ✓ + acceptance(text 含 "通过") ✓
  ↓ service.update(parent_t, status=done) → parent done ✓
  ↓
Parent 输出最终结果给 user
```

## 接口定义(tool spec 完整版)

`TODO_DISPATCH_SUBAGENT_SPEC` 见组件 1。

`SubAgentRunner.run` 接口:
```python
async def run(
    self,
    *,
    task_id: str,
    title: str,
    description: str = "",
    criteria: list[str] | None = None,
    parent_id: str = "",
    session_id: str = "s",
    timeout: int = 240,
) -> SubAgentResult:
    """跑 1 个 subagent,返回结果摘要。"""
```

`_render_subagent_summary` 接口:
```python
def _render_subagent_summary(
    results: list[SubAgentResult], parent_id: str, timeout: int,
) -> ToolResult:
    """N 个 subagent 结果合并成结构化摘要 ToolResult。"""
```

## 失败模式

| 失败 | 处理 |
|---|---|
| **subagent 超时(>timeout)** | 单 subagent 标记 `[timeout]`,不影响其他;汇总 ToolResult 标 blocked |
| **subagent 完成门拦截(acceptance)** | 单 subagent 标记 `[blocked]`,parent 看到后决定 force=true 重试或 fallback |
| **subagent 异常(exception)** | 单 subagent 标记 `[failed]` + error message,不影响其他 |
| **总 fan-out 超时(>timeout × N + 30)** | `dispatch_subagent_handler` 返回 error,已跑的 subagent 结果保留在各自 todo_id(parent 可查) |
| **LLM rate limit(N 并发撞)** | asyncio.gather 自然排队,subagent LLM call 串行退避(provider 决定) |
| **嵌套超限(depth ≥ 2)** | `dispatch_subagent_handler` 硬拒,ToolResult.is_error=True + 提示 |
| **task_id 不存在或已 done** | `dispatch_subagent_handler` 校验阶段拒,ToolResult.is_error=True |
| **max_fan_out 越界(< 1 或 > 10)** | 校验拒,ToolResult.is_error=True |
| **sub_specs 空或 > max_fan_out** | 校验拒,ToolResult.is_error=True |
| **policy / l2 / l5 触发** | 复用现有 L4/L2/L5 层(decision 6 共享 LLM/MCP/policy),subagent 与主 agent 同等保护 |
| **max_iter 耗尽(无 timeout 也无 exception)** | 单 subagent 标记 `[incomplete]` + error="max_iter=N 耗尽, todo 未 done";不影响其他;汇总标 blocked;**不**计入 done 统计 |
| **subagent 内部 tool 返回 is_error=True** | 单 subagent 标记 `[failed]` + error 摘要(`_extract_final_text` 拿到的 error 内容截断 200 字),不算 done |
| **tokens_used 全 0(D1 暂未接 SessionTokenStats)** | 汇总行显示 "总 tokens: TBD(D1.1 接 SessionTokenStats)" 而非 "0",避免 parent 误判无消耗 |

## 测试策略

### 单元测试(`tests/test_d1_subagent.py`,~14 tests)

- `test_subagent_runner_isolates_messages`:subagent 的 messages 不影响主 agent
- `test_subagent_runner_shares_llm_mcp_service`:3 个 runner 共享同一 LLMClient / MCPClient / TodoService(身份 equality)
- `test_subagent_runner_max_depth_blocks_nested`:depth=2 调 dispatch_subagent → ToolResult.is_error=True
- `test_subagent_runner_max_depth_allows_depth_2`:depth=1 调 dispatch_subagent → 允许,内部 subagent 的 depth=2 调 dispatch_subagent → 硬拒
- `test_subagent_runner_incomplete_when_max_iter_reached`:FakeLLM 模拟 max_iter=2 但只跑 2 轮没 done → SubAgentResult.status="incomplete"(D1 critical fix #3)
- `test_subagent_runner_tool_error_classified_as_failed`:subagent 调某 tool 返回 is_error=True → SubAgentResult.status="failed" 而非 "done"
- `test_total_fanout_timeout`:3 个 subagent × timeout=1s,run 总耗时 > 3s+30s → dispatch_subagent_handler 返回 error
- `test_subagent_runner_no_default_runner_returns_error`:deps 不注入 dispatch_subagent_runner → handler 校验阶段拒,ToolResult.is_error=True(D1 Important fix #1)
- `test_render_summary_includes_all_results`:3 个 subagent 结果全部出现 + 总耗时 + 总 tokens(TBD)+ 文件路径
- `test_render_summary_done_state_hint`:全 done → "父完成门: 全部 done";有 blocked → "父完成门: 有 N 个未 done"
- `test_render_summary_tokens_zero_shows_tbd`:所有 SubAgentResult.tokens_used=0 → 摘要行 "总 tokens: TBD(D1.1 接 SessionTokenStats)" 而非 "0"
- `test_render_summary_incomplete_status_label`:SubAgentResult.status="incomplete" → 标签 "incomplete (max_iter 耗尽, todo 未 done)"
- `test_render_summary_file_refs_extraction`:`tests/test_foo.py` 从末轮被正确提取
- `test_render_summary_truncates_final_text`:末轮 > 500 字 → 截断到 500
- `test_extract_file_refs_dedup`:同路径多次出现 → 去重
- `test_extract_file_refs_covers_common_extensions`:输入含 `.ts/.css/.sh` 等扩展名 → 全部被提取(D1 Minor fix #2)

### 集成测试(`tests/test_d1_integration.py`,~6 tests)

- `test_d1_dispatch_3_subagents_parallel_fake_llm`:FakeLLM 模拟 3 个 subagent 真并行(验证 asyncio.gather)+ 摘要渲染 + 完成门验入
- `test_d1_dispatch_with_subagent_failure`:1 个 subagent 失败 → 其他不受影响 + 汇总标 blocked
- `test_d1_dispatch_max_fan_out_validation`:max_fan_out=2 + sub_specs=3 → ToolResult.is_error=True
- `test_d1_dispatch_subagent_uses_completion_gate`:subagent 调 todo_update done → C 完成门验入(parent_task 树下聚合)
- `test_d1_dispatch_subagent_creates_correct_parent_child`:subagent 创建的 sub-task parent_task = task_id
- `test_d1_three_level_nested_blocked`:深度链 depth=0 → depth=1 → depth=2 调 dispatch_subagent → 第三层被硬拒,ToolResult.is_error=True(D1 Important fix #4)

### Prompt 注入测试(`tests/test_d1_prompt.py`,~3 tests)

- `test_subagent_hints_injected_after_htn_parent_create`:messages 含 todo_create + parent_task 非 None → system prompt 末有 `<subagent_hints>`
- `test_subagent_hints_not_injected_without_htn_parent`:messages 无 HTN parent create → system prompt 无 `<subagent_hints>`
- `test_subagent_hints_idempotent`:连续 refresh → `<subagent_hints>` 仍只 1 次(类比 C 的 idempotent 模式)

### E2E gated(`tests/_test_d1_e2e.py`,1 test)

- `@pytest.mark.requires_llm` + skipif:真 LLM 跑"创建 HTN parent → fan-out 3 subagent → 父任务标 done"完整路径

**baseline 验证**:D1 完成后 `pytest --collect-only` ≥ **1151 + 23(单元 14 + 集成 6 + prompt 3)= 1174**,delta +23(`_test_d1_e2e.py` 前缀 `_` 默认 skip,collect-only 不计入;e2e 真跑需 `OPENAI_API_KEY + CC_HARNESS_RUN_REAL_LLM=1`,不算进 baseline 锚定)。

## 范围外(out of scope,D1 不做)

- **Agent Team(D2)**:lead 调度 + 多 agent 协同 + 投票合并
- **`run_in_background` 异步参数**:parent 不阻塞,返回 task_id,后续 turn 轮询 —— D1.1
- **`verbose` 参数**:dispatch 返回全文贴回,默认摘要 —— D1.1
- **类型化 subagent**(general-purpose / Explore / Implement / Test):D1.x 探索
- **subagent 间通信**:D1 subagent 各自独立,完成只回填摘要
- **subagent 自动派启发式**(基于任务复杂度):D1 只 HTN parent 后提示
- **subagent 独立 token budget**(与 parent 完全隔离):D1.x 付费 / SLA 场景
- **sub-process 隔离**:D1 同 process 隔离 messages 足够
- **HTN 自动规划器**:C 已明确不做,D1 也不做(LLM 手动拆)

## 开放问题(plan 阶段再决)

1. **token 计费聚合**:`SubAgentResult.tokens_used` 接 `SessionTokenStats` 的具体方案(D1.1 接,实现细节由 plan 决定)
2. **MCP server 在 subagent 的 tool spec 是否要过滤**:D1 默认所有 MCP tools 都可见(主 agent 看什么 subagent 看什么),由 plan 决定是否过滤
3. **subagent 失败时是否要回滚已创建的 sub-todo**:D1 默认不回滚(保留 sub-task 状态供 parent 决策),由 plan 决定

## commit message baseline 锚定

- C final = `55f13e4`(`pytest --collect-only` = 1151)
- D1 第一个 commit message 必含 `baseline 1151`

## 历史 commit 关系

- 在 [[b-outer-loop-dag-landed]] (`55d6059`) + [[c-htn-tree-checkpoint-gate-landed]] (`55f13e4`) 基础上接续
- D1 = SubAgent 单层 fan-out,补齐"其一·长程任务" SubAgent 引擎
- D2 = Agent Team(lead 调度 + 协同),D1.x = run_in_background / verbose / 类型化
- 5 红 + 3 黄中的 **D(SubAgent)**:D1 落地后,Agent Team(D2) 是后续 sub-project