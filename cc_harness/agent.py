"""ReAct loop: streams one LLM turn, routes finish_reason, dispatches tools.

Output is the classic 4-phase ReAct format (per user spec):
    思考: <LLM's full reasoning text>
    行动: <tool call>
    观察: <tool result>
    [loop]
    结果: <final answer>

Modes (see task #4 / #6):
    "coding"  — full ReAct loop, tools enabled (default)
    "plan"    — one-shot final answer, no tool execution, no tools passed to LLM
    "design"  — one-shot final answer, no tool execution, output saved to disk
    "chat"    — same as coding (tools enabled, full ReAct loop)
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from rich.console import Console

from cc_harness.audit import log_decision
from cc_harness.config import ContextConfig
from cc_harness.l5 import L5Engine
from cc_harness.loop_control import (
    ActionJournal,
    CompletionVerifier,
    LoopControlConfig,
    ScheduledCall,
    StallController,
    ToolErrorKind,
    ToolScheduler,
    WorkingState,
    action_signature,
)
from cc_harness.mcp_client import ToolResult
from cc_harness.native_tools import NATIVE_FILE_TOOLS
from cc_harness.policy import Action, PolicyEngine
from cc_harness.reflection.events import (  # E2 T2.2:反思事件工厂
    empty_turn_loop,
    max_iter_reached,
    tool_error_burst,
    tool_retry_burst,
)
from cc_harness.render import (
    print_action,
    print_error,
    print_info,
    print_observation,
    print_result,
    print_thought,
    print_warn,
)
from cc_harness.schema import set_mcp_schemas, validate_mcp, validate_native
from cc_harness.tokens import TokenCounter, TurnTokenStats, UsageRecord
from cc_harness.tools import RUN_COMMAND_SPEC, confirm_tool, run_command

if TYPE_CHECKING:
    from cc_harness.project.models import TodoTask
    from cc_harness.project.service import TodoService  # D1 Task 7:TodoService 类型注解
    from cc_harness.reflection.engine import ReflectionEngine  # E2 T2.2:类型注解(运行时 = None)

_logger = logging.getLogger(__name__)

_VALID_MODES = ("coding", "plan", "design", "chat")

SUBAGENT_HINTS_BLOCK = """
<subagent_hints>
你最近创建了 HTN parent task(有 children 的父任务)。如果有多个独立子任务可并行完成,考虑用 `dispatch_subagent` tool fan-out 派 subagent 并行跑:
- 调 `dispatch_subagent(task_id=<parent_id>, sub_specs=[{title, criteria}, ...])`
- 派发数 N = len(sub_specs)(根据你的 todo 列表动态传),不是默认派 3 个
- subagent 共享 TodoService, 完成门自动验入(改 children 状态)
- N 个 subagent 真并行(默认上限 3 个;实际派发数 = sub_specs 长度,根据你的 todo 列表动态传 N,需要更多可覆盖 max_fan_out 到 ≤10)
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
# F T6 Standards: cross_session_tools block strip pattern 预编译(沿 _SUBAGENT_HINTS_RE 模式)
_CROSS_SESSION_TOOLS_BLOCK_RE = re.compile(
    r"\n<cross_session_tools>.*?</cross_session_tools>\n",
    flags=re.DOTALL,
)


# --- Native (non-MCP) tool registry ---
# Tools registered here are exposed to the LLM alongside MCP tools, but
# dispatched directly inside the agent (no protocol round-trip, no extra
# process). Each entry: {"spec": <OpenAI tool spec>, "handler": async fn}.
NATIVE_TOOLS: dict[str, dict] = {
    "run_command": {
        "spec": RUN_COMMAND_SPEC,
        "handler": run_command,
    },
    **NATIVE_FILE_TOOLS,
}


def _message_text(message: dict) -> str:
    """Return textual content from string or OpenAI multimodal messages."""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content or "")


async def run_turn(
    messages: list[dict],
    llm,                    # any object with async chat(messages, tools) -> AsyncIterator[StreamEvent]
    mcp,                    # any object with list_tools() and async call_tool(name, args) -> ToolResult
    *,
    max_iter: int | None = 20,
    mode: str = "coding",
    cwd: str | None = None,
    design_dir: Path | None = None,
    token_counter: TokenCounter | None = None,
    policy: PolicyEngine | None = None,
    l5: L5Engine | None = None,
    extra_native_specs: list[dict] | None = None,
    context_config: ContextConfig | None = None,
    memory_layer: dict | None = None,
    offload_deps: dict | None = None,
    qa_context: dict | None = None,
    resume_task: "TodoTask | None" = None,
    todo_hints: list[str] | None = None,  # B 阶段 Task 5: verify hook hints
    prior_messages: list[dict] | None = None,  # E3 D1:cross-session 旧 messages 摘要上下文
    tool_diff: list[str] | None = None,  # E3 D7:mcp tool 变更 warn 列表
    system_prompt: str | None = None,  # D1 Task 4 fix:subagent override
    todo_service: "TodoService | None" = None,  # D1 Task 7:TodoService 实例,非 None 时自动构造 SubAgentRunner + 注入 extras
    session_id: str = "",  # D1 Task 7:handler 用作 active_sessions(显式,不靠 env var)
    last_turn_text: str = "",  # D1 Task 7:C 阶段 todo_update 完成门 acceptance 校验用
    reflection_engine: "ReflectionEngine | None" = None,  # E2 T2.2:默认 None 保持向后兼容
    e1_decompose_enabled: bool = True,  # E1 D7:kill-switch(从 main.py 透传 policy.e1_decompose_enabled,默认 True 向后兼容)
    prompt_capabilities: dict[str, bool] | None = None,
    event_emitter: Callable[[dict], Awaitable[None]] | None = None,
    subagent_progress_cb: Callable[[str, str, str], Awaitable[None]] | None = None,
    confirm_handler: Callable[[str, dict, str], Awaitable[str]] | None = None,
    direct_render: bool | None = None,
    loop_control_config: LoopControlConfig | None = None,
    context_artifact_dir: Path | None = None,
    context_projection=None,
    refresh_system_prompt: bool = True,
    retry_empty_response: bool = True,
) -> TurnTokenStats:
    """Run one user turn in the given mode.

    In `coding` mode: full ReAct loop with tool execution.
    In `chat` mode: same as coding (tools enabled, full ReAct loop).
    In `plan` mode: one-shot LLM call (no tools passed, tool_calls dropped if any).
    In `design` mode: same as plan, plus the final assistant content is
        persisted to `design_dir` (default: ~/.cc-harness/designs/).

    `extra_native_specs` lets callers inject native-style tools alongside the
    built-in NATIVE_TOOLS (e.g. the locomo runner's memory_recall / memory_save).
    Each entry: ``{"spec": <OpenAI tool spec>, "handler": async fn,
    "deps": <dict splatted as kwargs at dispatch>}``. The LLM sees the merged
    spec list; tool_calls to an extra name dispatch to that entry's handler
    with ``(args, cwd=str(project_root), **deps)``.

    If `cwd` is provided, the system prompt at `messages[0]` is refreshed
    to match the current mode before the first LLM call. If `cwd` is None,
    the caller is responsible for having the right system prompt in place.

    `memory_layer`(Q3 Task7)可选分层记忆注入:``{"recall": async callable(query)
    -> RecallResult}``。recall 由 caller 注入(agent 不 import layered_recall,
    便于测试替身);pre-turn 调用,把 persona/scenarios 拼到 system 段。None
    或缺 "recall" 键 = kill-switch,不注入。fail-soft:recall 抛异常不崩主循环。

    `offload_deps`(Q4 Task5)可选短期符号化卸载:after-tool-call hook,tool result
    token > threshold → 落 refs + 摘要 + Mermaid canvas,messages 历史只留 pointer。
    独立于 memory_layer(两参数,不合并)。None 或 ``enabled=False`` = kill-switch。
    keys:enabled/threshold/offload(async closure)/canvas(async closure)。
    仅 allow + ask-yes 分支走 hook(其余 4 处短错误天然不撞阈值)。fail-soft:
    offload/canvas 抛异常 → 回退原文,不崩主循环。

    `qa_context`(Phase 1 Q1 uplift)可选 QA 模式标记:``{"q_type": int, "must_answer": bool}``。
    设了之后系统段会渲染 qa_intro 段(必须答规则 + 简洁风格),并把 q_type 注入
    模板 `{qa_category}`。None = 不渲染(向后兼容,test_agent.py 不受影响)。

    `resume_task`(Task 6)可选续干任务:``TodoTask``实例。非 None 时,在
    `_refresh_system_prompt` 末尾 append 一个 `<resume_task>...</resume_task>`
    块(用 append 不 rebuild,与 Q3 persona / Q4 canvas 同样的模式),每次 turn
    重写 system prompt 时自动刷新。condition:`mode == "coding" and resume_task
    is not None`,plan/design 模式不渲染。None = kill-switch(向后兼容,
    test_agent.py 不受影响)。

    `system_prompt`(D1 Task 4 fix)可选显式 system prompt override:非 None 时,
    跳过 `_refresh_system_prompt()`(不重建 system 段),直接把此字符串写入
    `messages[0]["content"]`(若 `messages[0]` 是 system)或 insert 新 system
    消息。**专为 subagent 用**(SubAgentRunner 构造独立 system prompt,不能
    被主 agent 的 mode-aware rebuild 覆盖)。None = 走默认 _refresh 路径
    (向后兼容,REPL/test_agent 无感)。

    `todo_service`(D1 Task 7)TodoService 实例:非 None 时,run_turn 在构造
    tool_specs **之前**自动:
      1. 调 `get_default_runner(llm, mcp, todo_service, project_root=cwd,
         max_iter=max_iter, policy=policy, l5=l5)` 构造 depth=0 SubAgentRunner
         (共享 4 资源 — decision 6:llm / mcp / todo_service / policy,
         加 l5 — D1 Task 4 fix)。
      2. 调 `inject_todo_tools(todo_service, session_id, cwd=cwd,
         last_turn_text=last_turn_text, dispatch_subagent_runner=runner)`
         构造 9 个 todo entries(含 dispatch_subagent),runner 注入 deps。
      3. 把 todo entries append 到 `extra_native_specs`(若 caller 已传
         extra_native_specs,合并而非替换 — REPL 当前既传 memory_extras 又
         将来传 todo_service 时不丢 memory_extras)。None = 跳过 auto-build
         (向后兼容,旧 caller / test_agent.py 不受影响)。

    `session_id`(D1 Task 7)handler 用作 active_sessions(显式,不靠 env var)。
    与 `todo_service` 配对使用;todo_service 非 None 时建议传非空 session_id。

    `last_turn_text`(D1 Task 7)C 阶段 todo_update 完成门 acceptance 校验用。
    todo_service 非 None 时透传给 inject_todo_tools。

    Mutates `messages` in place. Async so the repl can call it from its
    persistent event loop without `asyncio.run` overhead.
    """
    if mode not in _VALID_MODES:
        raise ValueError(f"unknown mode: {mode!r} (expected one of {_VALID_MODES})")

    # UI entrypoints own terminal state. When an event consumer is attached,
    # suppress the legacy Rich prints unless explicitly requested; the REPL
    # may opt back in with direct_render=True during its migration window.
    if direct_render is None:
        direct_render = event_emitter is None
    console = Console() if direct_render else Console(file=io.StringIO(), force_terminal=False)
    iter_count = 0
    _empty_retried = False  # one-shot retry guard for empty-content turns
    tool_call_log: list = []  # Plan1 Task4: [{name, args, ok, result}] per tool dispatch
    # Task 3 (Codex Web UI): event_emitter 转发 hook,REPL(None)路径零调用。
    # _safe_emit 包成 fail-soft call site,任意 emit 异常都不会破主 ReAct 循环。
    # 字段 schema 由 tests/test_agent_event_emitter.py 与 Task 4 pydantic 锁定。
    # Fix round 1:emit 异常 swallow 但记 debug 日志(只记异常 repr,不记 event
    # payload — payload 可能含 L5 脱敏后的残留 / secrets,不外泄)。
    async def _safe_emit(ev: dict) -> None:
        if event_emitter is None:
            return
        try:
            await event_emitter(ev)
        except Exception as exc:
            _logger.debug("event_emitter raised: %s", exc)
    # E2 T2.2 Step 3e:同 tool+args 调 2+ 次的检测(独立于 tool_call_log 避免冲突,
    # tool_call_log 仍按 Plan1 契约存 dict 给 TurnTokenStats.tool_call_log 用)
    _tool_retry_log: list[tuple] = []  # [(name, arguments_json), ...]
    # E2 T2.2 Step 3d:tool is_error 累计计数器(本 turn 内连续 2+ 触发 emit)
    _tool_error_count = 0

    # E2 T2.2 Step 3d:tool is_error 计数 + 触发 emit(2+ 触发一次,清零防刷)。
    # fail-soft:emit 异常 → pass,绝不影响主循环。
    async def _note_tool_error(tool_name: str, error_text: str) -> None:
        nonlocal _tool_error_count
        _tool_error_count += 1
        if _tool_error_count >= 2 and reflection_engine is not None:
            try:
                await reflection_engine.emit(
                    tool_error_burst(
                        session_id=session_id or "default",
                        turn_idx=iter_count,
                        errors=[{"tool": tool_name, "error": error_text[:200]}],
                    )
                )
                _tool_error_count = 0  # 避免每个 tool 都 emit
            except Exception:
                pass

    # E1 D2:todo_create 成功后 → user 摘要(plan 视图)。
    # 守卫:mode==coding + not is_error + p.name=='todo_create' + todo_service 非 None。
    # 从 result.llm_text 提取 task id → todo_service.get(tid) 拿 TodoTask → _print_decomp_summary。
    # fail-soft:任何异常 → 跳过,不崩主循环(纯 cosmetic,不影响 turn).
    async def _maybe_print_decomp_summary(p, result) -> None:
        if mode != "coding":
            return
        if p.name != "todo_create":
            return
        if todo_service is None:
            return
        if getattr(result, "is_error", False):
            return
        try:
            _m = re.search(r"created task (\S+)", result.llm_text or "")
            if not _m:
                return
            _tid = _m.group(1)
            _task = await todo_service.get(_tid)
            _print_decomp_summary([_task], console=console)
        except Exception:
            pass

    last_compaction = None  # Plan3: CompactionStats from maybe_compact (or None)
    _last_node = None  # Q4 Task5: offload edge chain — node_id of last offloaded tool result

    # D1 Task 4 fix:system_prompt override 优先于 cwd-driven rebuild(专为
    # subagent 用 — subagent 构造的独立 prompt 不能被主 agent 的 mode-aware
    # refresh 覆盖)。非 None → 直接写入/插入 messages[0],跳过 _refresh。
    if system_prompt is not None:
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = system_prompt
        else:
            messages.insert(0, {"role": "system", "content": system_prompt})
    elif refresh_system_prompt and cwd is not None:
        _prompt_capabilities = {
            "todo_available": True,
            "subagent_available": True,
            "visible_thought_required": True,
        }
        if prompt_capabilities:
            _prompt_capabilities.update(prompt_capabilities)
        # E2 T2.2: 把 reflection_engine.get_last_neg_reflection() 注入 extra_ctx,
        # 让 SECTION_POOL 拼装把反思段加到 system prompt 末尾(只走 extra_ctx,绝不
        # 走 messages 之外的旁路)。reflection_engine=None 时 key 不出现 = 不渲染。
        _neg_extra = (
            {"last_neg_reflection": reflection_engine.get_last_neg_reflection()}
            if reflection_engine is not None
            else {}
        )
        # E1 D7:分解契约 hint 注入 — 仅 iter==0 且 policy kill-switch 开启时 True
        # (section 自身再三重 gate 防 leak:flag + mode + iter_count)。iter_count 是
        # run_turn 局部变量,同作用域闭包可见。policy 关掉 → flag 永远 False →
        # section 不渲染(T1 test_decomposition_hint_skips_when_kill_switch_off 覆盖)。
        _e1_extra = {
            "e1_decompose_hint": (
                (iter_count == 0)
                and e1_decompose_enabled
                and _prompt_capabilities["todo_available"]
                and _prompt_capabilities["subagent_available"]
            ),
            "iter_count": iter_count,
            **_prompt_capabilities,
        }
        # Phase 1 Q1 uplift: qa_context → render qa_intro section
        if qa_context and qa_context.get("q_type") is not None:
            _refresh_system_prompt(
                messages, cwd, mode,
                extra_ctx={"qa_category": qa_context["q_type"], **_neg_extra, **_e1_extra,
                           "e3_prior_messages": prior_messages},  # E3
                resume_task=resume_task,
                todo_hints=todo_hints,
                tool_diff=tool_diff,  # E3 D7
            )
        else:
            _refresh_system_prompt(
                messages, cwd, mode,
                extra_ctx={**_neg_extra, **_e1_extra,
                           "e3_prior_messages": prior_messages},  # E3
                resume_task=resume_task,
                todo_hints=todo_hints,
                tool_diff=tool_diff,  # E3 D7
            )

    # --- Q3 Task7: 分层记忆 pre-turn 注入 ---
    # memory_layer = {"recall": async callable(query) -> RecallResult}
    # recall 由 caller 注入(agent.py 不 import layered_recall);fail-soft。
    # Q3 Recall Hardening (2026-07-30 LoCoMo full-run bug fix): 外层加
    # asyncio.wait_for(timeout=10) 防 recall 永远 hang 把整条 run_turn 卡死。
    # 原始 bug: runner.py:223 await run_turn → agent.py:315 await recall → 永远
    # hang → 用户 Ctrl+C → KeyboardInterrupt → aiosqlite 写者线程在 loop
    # 关闭后 call_soon_threadsafe 抛 Event loop is closed。10s 上限配合 recall
    # 自身的 timeout_s=5.0(见 cc_harness/memory/extras.py:68)留 5s 余量,够
    # 健康 query 用尽预算返回,挂死 query 走 TimeoutError → except 吞掉 →
    # print_warn 跳过,run_turn 继续。
    if memory_layer and memory_layer.get("recall") and messages:
        try:
            _q = next((_message_text(m) for m in reversed(messages)
                       if m.get("role") == "user"), "")
            recall = await asyncio.wait_for(
                memory_layer["recall"](_q), timeout=10.0)
            if recall.atoms and messages[0].get("role") == "system":
                atom_lines = []
                for item in recall.atoms:
                    memory = item[0] if isinstance(item, tuple) else item
                    text = getattr(memory, "text", "")
                    if text:
                        atom_lines.append(f"- {text[:240]}")
                if atom_lines:
                    messages[0]["content"] += (
                        "\n\n## Relevant memory facts\n" + "\n".join(atom_lines)
                    )
            await _safe_emit({
                "type": "capability_activation",
                "capability": "memory",
                "stage": "recall",
                "persona": recall.persona is not None,
                "scenario_count": len(recall.scenarios),
                "atom_count": len(recall.atoms),
            })
            if recall.persona and messages[0].get("role") == "system":
                messages[0]["content"] += f"\n\n## 用户画像\n{recall.persona.summary[:200]}"
            if recall.scenarios and messages[0].get("role") == "system":
                messages[0]["content"] += "\n\n## 相关场景\n" + "\n".join(
                    f"- {s.summary[:120]}" for s in recall.scenarios)
        except (asyncio.TimeoutError, Exception) as e:
            await _safe_emit({
                "type": "capability_activation",
                "capability": "memory",
                "stage": "recall",
                "error": f"{type(e).__name__}: {e}",
            })
            print_warn(console, f"memory inject failed: {e}")

    # --- Q4 Task6: pre-turn Mermaid 画布注入(预算 + 顺序)---
    # canvas_inject 开关 + canvas.md 存在 + token<=预算(mermaid_max_token_ratio ×
    # context_window)→ 系统段追加。顺序:基线 → Q3 persona → Q3 scenarios → Q4 mermaid
    # (本块紧跟 Q3 之后,顺序由 placement 保证)。fail-soft:文件读/编码/计数异常 →
    # 静默跳过,不崩主循环。canvas_path=None 或文件不存在(首次回合)→ 跳过。
    # offload_deps=None → 不注入(向后兼容,test_agent.py / test_repl.py 不受影响)。
    if offload_deps and offload_deps.get("canvas_inject", True) and messages:
        try:
            _canvas_path = offload_deps.get("canvas_path")
            if _canvas_path is not None and messages[0].get("role") == "system":
                _canvas_p = Path(_canvas_path)
                if _canvas_p.exists():
                    _tc = token_counter or TokenCounter()
                    _ratio = offload_deps.get("mermaid_max_token_ratio", 0.2)
                    _window = offload_deps.get("context_window", 1_000_000)
                    _budget = _ratio * _window
                    _canvas_text = _canvas_p.read_text(encoding="utf-8")
                    if _tc.count_text(_canvas_text) <= _budget:
                        messages[0]["content"] += (
                            "\n\n## 任务画布(Mermaid)\n" + _canvas_text
                        )
                    else:
                        print_warn(console, (
                            f"mermaid inject skipped: canvas "
                            f"{_tc.count_text(_canvas_text)}t > budget {_budget:.0f}t"))
        except Exception as e:
            print_warn(console, f"mermaid inject failed: {e}")

    # --- L4 policy gate setup ---
    project_root = Path(cwd or ".").resolve()
    # ``None`` preserves the historical direct-call contract. Production
    # entrypoints pass an enabled config explicitly.
    _loop_cfg = loop_control_config or LoopControlConfig(enabled=False)
    _working_state = WorkingState.new(project_root)
    _journal: ActionJournal | None = None
    _completion_verifier: CompletionVerifier | None = None
    _stall_controller: StallController | None = None
    _completion_rechecks = 0
    if _loop_cfg.enabled:
        _completion_verifier = CompletionVerifier(_loop_cfg.completion_contract)
        _stall_controller = StallController(_loop_cfg.stall_repeat_threshold)
        if _loop_cfg.action_journal:
            _safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id or "default")
            _journal = ActionJournal(
                project_root / ".cc-harness" / "action-journal" / f"{_safe_session}.jsonl",
                session_id=session_id or "default",
            )
            _working_state = _journal.recover_state(project_root)
            _interrupted = _journal.incomplete_actions()
            if _interrupted and messages and messages[0].get("role") == "system":
                messages[0]["content"] += (
                    "\n\n<loop_recovery>\n"
                    "The prior process stopped during these actions: "
                    + ", ".join(_interrupted)
                    + ". Inspect current workspace state before repeating a mutating action.\n"
                    "</loop_recovery>"
                )
    if policy is None:
        policy = PolicyEngine(project_root=project_root)
    # Inject MCP schemas so schema.validate_mcp can check MCP tool args.
    # F T7 fix:防御 sync/async MCP(production async + test mock sync 共存,沿 repl.py:253 iscoroutine pattern)
    import inspect as _inspect
    try:
        _tools_result = mcp.list_tools()
        if _inspect.iscoroutine(_tools_result):
            _tools_result = await _tools_result
        set_mcp_schemas({
            t["function"]["name"]: t["function"].get("parameters", {})
            for t in (_tools_result or [])
        })
    except Exception:
        pass
    audit_path = project_root / ".cc-harness" / "logs" / "policy.jsonl"
    l5_audit_path = project_root / ".cc-harness" / "logs" / "l5.jsonl"

    def _redact(text: str, stage: str) -> str:
        """L5 脱敏 + 审计。stage ∈ {'thought','result'}。engine=None/非 str/空 → 原文直通。
        命中即审计(只记类型计数,绝不记明文)。"""
        if l5 is None or not isinstance(text, str) or not text:
            return text
        out = l5.scan(text)
        if out.findings:
            log_decision(
                l5_audit_path, iter_n=iter_count, tool=f"llm_{stage}",
                args={"findings": out.findings, "text_len": len(text)},
                action="l5_redact", outcome="redacted",
                rule_id=",".join(sorted(out.findings)), reason="", mode=mode,
            )
        return out.sanitized_text

    # In plan/design mode, the LLM should not see any tool definitions, so
    # it physically cannot emit tool_calls. In coding mode, expose both the
    # MCP tool set and the native tool registry (built-in + caller-injected).
    if mode in ("coding", "chat"):
        # --- D1 Task 7: todo_service → 自动构造 SubAgentRunner + 注入 extras ---
        # 共享 4 资源(decision 6: llm / mcp / todo_service / policy) + l5
        # (D1 Task 4 fix);runner 注入 dispatch_subagent entry 的 deps dict。
        # 合并而非替换 caller 的 extra_native_specs(REPL 既有 memory_extras
        # 又传 todo_service 时不丢 memory_extras)。fail-soft: 构造异常时
        # 跳过 auto-build,继续走默认路径,不崩主循环。
        if todo_service is not None:
            try:
                from cc_harness.project.extras import inject_todo_tools
                from cc_harness.project.subagent import get_default_runner
                _runner = get_default_runner(
                    llm, mcp, todo_service,
                    project_root=str(project_root),
                    max_iter=max_iter,
                    policy=policy,
                    l5=l5,
                    loop_control_config=_loop_cfg,
                )
                _todo_extras = inject_todo_tools(
                    todo_service, session_id,
                    cwd=str(project_root),
                    last_turn_text=last_turn_text,
                    dispatch_subagent_runner=_runner,
                    progress_cb=subagent_progress_cb,
                )
                if extra_native_specs is None:
                    extra_native_specs = _todo_extras
                else:
                    extra_native_specs = list(extra_native_specs) + _todo_extras
            except Exception as _e:
                print_warn(console, f"subagent runner 注入失败: {_e}; 跳过 dispatch_subagent")

        # F T7 fix:防御 sync/async MCP(production async + test mock sync 共存,沿 repl.py:253 iscoroutine pattern)
        _tools_result = mcp.list_tools()
        if _inspect.iscoroutine(_tools_result):
            _tools_result = await _tools_result
        tool_specs = list(_tools_result or [])
        for native in NATIVE_TOOLS.values():
            tool_specs.append(native["spec"])
        for entry in (extra_native_specs or []):
            tool_specs.append(entry["spec"])
    else:
        tool_specs = None

    iter_usages: list[UsageRecord] = []   # per-iter API-reported usage

    async def _dispatch(p, args: dict, project_root: Path):
        """Route a tool call to its handler.

        Precedence: NATIVE_TOOLS (built-in) > extra_native_specs (caller-
        injected) > mcp.call_tool (existing fallback). Handlers return a
        mcp_client.ToolResult; the caller reads `.llm_text` for the message
        appended to `messages`.
        """
        if p.name in NATIVE_TOOLS:
            return await NATIVE_TOOLS[p.name]["handler"](args, cwd=str(project_root))
        extra_entry = next(
            (e for e in (extra_native_specs or [])
             if e["spec"]["function"]["name"] == p.name),
            None,
        )
        if extra_entry is not None:
            h_kwargs = {"cwd": str(project_root), **extra_entry.get("deps", {})}
            return await extra_entry["handler"](args, **h_kwargs)
        return await mcp.call_tool(p.name, args)

    def _append_journal(
        *, kind: str, action_id: str, tool: str, args: dict, outcome: dict,
    ) -> None:
        if _journal is None:
            return
        try:
            _journal.append(
                kind=kind,
                action_id=action_id,
                tool=tool,
                args=args,
                outcome=outcome,
                state=_working_state,
            )
        except Exception as exc:  # noqa: BLE001 - journal failure cannot break the agent
            _logger.warning("action journal append failed: %s", exc)

    async def _dispatch_controlled(p, args: dict, action_id: str) -> ToolResult:
        """Dispatch with deterministic retry, state tracking, and journaling."""
        _action_signature = action_signature(p.name, args)
        if (
            _loop_cfg.enabled
            and _loop_cfg.stall_detection
            and _stall_controller is not None
            and _stall_controller.should_block(_action_signature)
        ):
            result = ToolResult.error(
                display="repeated stalled action blocked",
                llm=(
                    "[Loop Controller] Repeated stalled action blocked. "
                    "State a new hypothesis and choose a different action."
                ),
            )
            fingerprint = _working_state.observe(
                p.name,
                args,
                is_error=True,
                result_text=result.llm_text,
                error_kind=ToolErrorKind.EXECUTION,
            )
            _append_journal(
                kind="tool_blocked",
                action_id=action_id,
                tool=p.name,
                args=args,
                outcome={"reason": "stalled_action", "result_hash": fingerprint},
            )
            await _safe_emit({
                "type": "loop_stall_blocked",
                "name": p.name,
                "args": args,
                "iteration": iter_count,
                "ts": time.time(),
            })
            return result
        _append_journal(
            kind="tool_started", action_id=action_id, tool=p.name, args=args, outcome={},
        )
        attempt = 0
        final_kind: ToolErrorKind | None = None
        while True:
            attempt += 1
            try:
                result = await _dispatch(p, args, project_root)
            except Exception as exc:  # noqa: BLE001 - tool boundary normalizes plugin failures
                result = ToolResult.error(
                    display=f"raised: {exc}",
                    llm=f"[Tool Error] dispatch 异常: {type(exc).__name__}: {exc}",
                )
                decision = _loop_cfg.recovery_policy.decide(
                    result.llm_text, attempt=attempt, exception=exc,
                )
            else:
                decision = _loop_cfg.recovery_policy.decide(
                    result.llm_text, attempt=attempt,
                ) if result.is_error else None

            if (
                _loop_cfg.enabled
                and _loop_cfg.error_recovery
                and decision is not None
                and decision.retry
            ):
                _append_journal(
                    kind="tool_retry",
                    action_id=action_id,
                    tool=p.name,
                    args=args,
                    outcome={"attempt": attempt, "kind": decision.kind.value},
                )
                await _safe_emit({
                    "type": "retrying",
                    "attempt": attempt + 1,
                    "max_attempts": _loop_cfg.recovery_policy.max_transient_retries + 1,
                    "delay_seconds": _loop_cfg.recovery_policy.retry_delay_seconds,
                    "reason": decision.kind.value,
                    "iteration": iter_count,
                    "ts": time.time(),
                })
                if _loop_cfg.recovery_policy.retry_delay_seconds > 0:
                    await asyncio.sleep(_loop_cfg.recovery_policy.retry_delay_seconds)
                continue

            if decision is not None:
                final_kind = decision.kind
                if _loop_cfg.enabled and _loop_cfg.error_recovery:
                    result = ToolResult.error(
                        display=result.display_text,
                        llm=(
                            f"{result.llm_text}\n"
                            f"[Recovery: {decision.kind.value}] {decision.instruction}"
                        ),
                    )
            break

        fingerprint = _working_state.observe(
            p.name,
            args,
            is_error=result.is_error,
            result_text=result.llm_text,
            error_kind=final_kind,
        )
        if _loop_cfg.enabled and _loop_cfg.stall_detection and _stall_controller is not None:
            stall = _stall_controller.observe(
                fingerprint, action_signature=_action_signature,
            )
            if stall.stalled:
                result.llm_text += f"\n[Loop Controller] {stall.instruction}"
                await _safe_emit({
                    "type": "loop_stall",
                    "repeated": stall.repeated,
                    "instruction": stall.instruction,
                    "iteration": iter_count,
                    "ts": time.time(),
                })
        _append_journal(
            kind="tool_finished",
            action_id=action_id,
            tool=p.name,
            args=args,
            outcome={
                "ok": not result.is_error,
                "attempts": attempt,
                "error_kind": final_kind.value if final_kind else None,
                "result_hash": fingerprint,
            },
        )
        await _safe_emit({
            "type": "working_state",
            "state": _working_state.to_dict(),
            "iteration": iter_count,
            "ts": time.time(),
        })
        return result

    def _prepare_parallel_read_batch() -> list[tuple[int, object, dict, object]] | None:
        if not (
            _loop_cfg.enabled
            and _loop_cfg.parallel_read_tools
            and len(pending) > 1
        ):
            return None
        prepared: list[tuple[int, object, dict, object]] = []
        scheduled: list[ScheduledCall] = []
        for index, call in enumerate(pending):
            if call.name is None:
                return None
            try:
                parsed = json.loads(call.arguments_json) if call.arguments_json else {}
            except json.JSONDecodeError:
                return None
            if not isinstance(parsed, dict):
                return None
            ok, _ = validate_native(call.name, parsed)
            if not ok:
                return None
            decision = policy.evaluate(call.name, parsed, {"project_root": project_root})
            if not decision.allow:
                return None
            prepared.append((index, call, parsed, decision))
            scheduled.append(ScheduledCall(index, call.name, parsed))
        batches = ToolScheduler().plan(scheduled)
        if len(batches) != 1 or not batches[0].parallel:
            return None
        return prepared

    async def _maybe_offload_content(result_text: str, tool_name: str,
                                     tool_args: dict) -> str:
        """Q4 Task5 offload hook:胖 tool result → refs + 摘要 + Mermaid canvas,
        返回应放入 tool message 的 content(pointer_msg 若卸载,否则原文 untrusted 包裹)。

        仅由 allow + ask-yes 分支调(其余 4 处短错误天然不撞阈值,不走 hook)。
        两层 fail-soft:外层 try(threshold-check + offload 调用,offload 自身 LLM/disk
        失败 → 回退 _external);内层 try(canvas best-effort 装饰,失败不丢 pointer、
        不留 refs 孤儿、不 stale edge)。_last_node 无论 canvas 成败都推进(它在
        canvas 调用之后赋值,但内层 except 不 return,故赋值一定执行)。kill-switch:
        offload_deps=None 或 enabled=False → 直返 untrusted 原文。
        """
        nonlocal _last_node
        _external = f"<untrusted>{result_text}</untrusted>"
        if not (offload_deps and offload_deps.get("enabled", True)):
            return _external
        try:
            _tc = token_counter or TokenCounter()
            if _tc.count_text(result_text) > offload_deps["threshold"]:
                _off = await offload_deps["offload"](
                    result_text, tool_name, tool_args,
                    threshold=offload_deps["threshold"], token_counter=_tc)
                if _off is not None:
                    # canvas 是 best-effort 装饰,失败不影响 offload 已达成的减载:
                    # 独立 try/except — 否则 canvas 抛会丢 pointer(胖文回 messages)+
                    # refs 孤儿 + _last_node 不更新(下次 edge 指向陈旧前驱)。
                    try:
                        await offload_deps["canvas"](
                            _off.node_id, tool_name, _off.summary, edge_from=_last_node)
                    except Exception as ce:
                        print_warn(console, f"offload canvas (best-effort) failed: {ce}")
                    _last_node = _off.node_id   # 无论 canvas 成败,edge 链推进
                    return _off.pointer_msg
        except Exception as e:
            print_warn(console, f"offload hook failed: {e}")
        return _external

    def _stats() -> TurnTokenStats:
        """Build TurnTokenStats from current messages + tool_specs + iter_usages."""
        counter = token_counter
        if counter is None:
            counter = TokenCounter()
        cats = counter.categorize(messages, tools=tool_specs)
        return TurnTokenStats(
            user_input=cats["user_input"],
            tool_calls=cats["tool_calls"],
            llm_output=cats["llm_output"],
            system_prompt=cats["system_prompt"],
            summary=cats["summary"],
            tool_definitions=cats["tool_definitions"],
            api_prompt_tokens=sum(u.prompt_tokens for u in iter_usages),
            api_uncached_prompt_tokens=sum(u.uncached_prompt_tokens for u in iter_usages),
            api_cache_read_prompt_tokens=sum(
                u.cache_read_prompt_tokens for u in iter_usages
            ),
            api_cache_creation_prompt_tokens=sum(
                u.cache_creation_prompt_tokens for u in iter_usages
            ),
            api_completion_tokens=sum(u.completion_tokens for u in iter_usages),
            api_total_tokens=sum(u.total_tokens for u in iter_usages),
            iter_count=len(iter_usages),
            api_reported=bool(iter_usages),
            tool_call_log=tool_call_log,
            compaction=last_compaction,
        )

    async def _stream_one_turn(
        model_messages: list[dict],
    ) -> tuple[str, list, str | None, UsageRecord | None]:
        """Stream exactly one LLM turn. Returns (content, pending, finish_reason, usage).

        Buffers content (no real-time printing) because the routing decision —
        has_tool_calls vs final answer — is only known after the "done" event.
        Returns empty content only if the LLM genuinely produced nothing.
        """
        content_parts: list[str] = []
        pending: list = []
        finish_reason: str | None = None
        usage: UsageRecord | None = None
        stream = llm.chat(model_messages, tool_specs)
        try:
            async for ev in stream:
                if ev.kind == "content":
                    content_parts.append(ev.text)
                    await _safe_emit({
                        "type": "content_delta",
                        "text": ev.text,
                        "ts": time.time(),
                        "iteration": iter_count,
                    })
                elif ev.kind == "tool_call_delta":
                    pass  # accumulation handled inside llm.chat
                elif ev.kind == "done":
                    finish_reason = ev.finish_reason
                    pending = ev.pending
                    usage = ev.usage
                    # Prefer the consolidated content on the done event if set;
                    # fall back to the streamed parts we collected above.
                    content_parts = [ev.content] if ev.content else content_parts
        finally:
            close_stream = getattr(stream, "aclose", None)
            if close_stream is not None:
                await close_stream()
        return "".join(content_parts), pending, finish_reason, usage

    from cc_harness.context import ContextProjection

    _context_projection = context_projection or ContextProjection(
        messages, artifact_dir=context_artifact_dir
    )

    while max_iter is None or iter_count < max_iter:
        iter_count += 1
        iter_usage: UsageRecord | None = None   # usage for this iter (set on done)
        # The runtime appends the current user message before calling run_turn.
        # Keep a projection supplied at session boot in sync even when both
        # offload and context compaction are disabled.
        _context_projection.sync(messages)

        # Q4 Task7: ratio 批量兜底(无 count_messages,用 sum count_text)。
        # context 总 token 超 offload_ratio × context_window → 批量卸载剩余大 tool
        # result(reversible pointer)。置于 Plan3 maybe_compact 之前:Q4 可逆卸载先
        # 减载,Plan3 不可逆 summarize 后兜底(Q4 ratio 0.5 < Plan3 tier1 0.6,先触发)。
        # fail-soft:外层 try(构造/读 key)+ 每条 offload 独立 try(单条失败不破批/不崩轮)。
        # offload_deps=None 或 enabled=False → 跳过(向后兼容,test_agent/test_repl 无感)。
        if offload_deps and offload_deps.get("enabled", True):
            try:
                _cw = offload_deps.get("context_window") or (
                    context_config.context_window if context_config else 1_000_000)
                _tc = token_counter or TokenCounter()
                _projected = _context_projection.messages
                _total = sum(_tc.count_text(_message_text(m)) for m in _projected)
                if _cw > 0 and _total / _cw > offload_deps.get("offload_ratio", 0.5):
                    for m in _projected:
                        # prefix match(非子串):pointer 形如 [offloaded node=...];
                        # 子串 "offloaded" 会误跳含该字面的 legit 大 result(源码/log)。
                        _content = m.get("content") or ""
                        if (m.get("role") == "tool"
                                and not _content.lstrip().startswith("[offloaded node=")):
                            if _tc.count_text(_content) > offload_deps["threshold"]:
                                try:
                                    _off = await offload_deps["offload"](
                                        m["content"], "(batch)", {},
                                        threshold=offload_deps["threshold"], token_counter=_tc)
                                    if _off:
                                        m["content"] = _off.pointer_msg
                                except Exception:
                                    pass
            except Exception:
                pass

        # Plan3: maybe compact context before LLM call (all modes). Catches own
        # errors (returns stats.error) so compaction never kills the ReAct loop.
        if context_config and context_config.enabled:
            _counter = token_counter or TokenCounter()
            last_compaction = await _context_projection.compact(
                messages, tool_specs, _counter, context_config, llm
            )
            await _safe_emit({
                "type": "capability_activation",
                "capability": "context",
                "phase": "projection",
                "tier": last_compaction.tier.name.lower(),
                "error": last_compaction.error,
                "artifact": last_compaction.artifact_path,
                "summary_version": last_compaction.summary_version,
                "ratio_before": last_compaction.ratio_before,
                "ratio_after": last_compaction.ratio_after,
            })
            if last_compaction.error and context_config.fail_closed:
                _err_stats = _stats()
                _err_stats.error = f"context projection failed: {last_compaction.error}"
                return _err_stats

        # 1. Stream one LLM turn (buffered — see _stream_one_turn).
        try:
            content, pending, finish_reason, iter_usage = await _stream_one_turn(
                _context_projection.messages
            )
        except Exception as e:
            print_error(console, f"LLM stream failed: {e}")
            await _safe_emit({
                "type": "observation",
                "text": str(e),
                "is_error": True,
                "duration_ms": 0,
                "iteration": iter_count,
            })
            # D1 Task 4 fix (Important #1):把 fatal 错误塞 stats.error,
            # 让 SubAgentRunner.run() 检测到 → status="failed"。
            _err_stats = _stats()
            _err_stats.error = f"{type(e).__name__}: {e}"
            return _err_stats

        if iter_usage is not None:
            iter_usages.append(iter_usage)

        # 2. Compute routing
        has_tool_calls = (finish_reason == "tool_calls") and bool(pending)

        if has_tool_calls and mode in ("coding", "chat"):
            # Coding mode: full ReAct loop with tool execution.
            # 3. Build assistant message (with tool_calls; content may be None)
            if content:
                content = _redact(content, "thought")
            # E2 T2.2 Step 3e:同 tool+args 在本 turn 累计 2+ 次 → emit tool_retry_burst
            # (ambig)。仅在 reflection_engine 非 None 时跑;不阻塞 assistant 构造。
            if reflection_engine is not None and pending:
                for p in pending:
                    _sig = (p.name, p.arguments_json or "")
                    if _tool_retry_log.count(_sig) >= 1:
                        try:
                            await reflection_engine.emit(
                                tool_retry_burst(
                                    session_id=session_id or "default",
                                    turn_idx=iter_count,
                                    calls=[{
                                        "tool": p.name,
                                        "args": json.loads(p.arguments_json or "{}"),
                                        "count": _tool_retry_log.count(_sig) + 1,
                                    }],
                                )
                            )
                        except Exception:
                            pass
            # Finding 1 fix:生成稳定 tc_id BEFORE 构建 assistant message,
            # assistant tool_calls[].id 与后续 tool message tool_call_id 共用
            # 一份 id,避免 id 缺失/不匹配导致 messages history 走形。
            tc_ids: list[str] = [p.id or f"call_{iter_count}_{i}"
                                 for i, p in enumerate(pending)]
            assistant_msg: dict = {
                "role": "assistant",
                "content": content if content else None,
                "tool_calls": [_pending_to_openai_tc(p, tc_id) for p, tc_id in zip(pending, tc_ids)],
            }
            messages.append(assistant_msg)
            # 记录到 _tool_retry_log(放在 append 之后,下次再遇同 sig 才计数)
            for p in pending:
                _tool_retry_log.append((p.name, p.arguments_json or ""))

            # 3.5 Print the 思考 block (full LLM text for this iter)
            if content:
                print_thought(console, content)
                # Task 3: emit thought 事件(LLM stream 缓冲完成,出 thought)
                # text = post-L5 脱敏 content;iteration = 当前 iter;ts = time.time()。
                await _safe_emit({
                    "type": "thought",
                    "text": content,
                    "ts": time.time(),
                    "iteration": iter_count,
                })

            # 4. Execute independent native reads concurrently. MCP calls and
            # all mutations remain serial because their side effects are not
            # provably independent.
            _parallel_reads = _prepare_parallel_read_batch()
            if _parallel_reads is not None:
                for index, p, args, decision in _parallel_reads:
                    print_action(console, p.name, args)
                    log_decision(
                        audit_path,
                        iter_n=iter_count,
                        tool=p.name,
                        args=args,
                        action=decision.action.value,
                        outcome="executed_parallel_read",
                        rule_id=decision.rule_id,
                        reason=decision.reason,
                        mode=mode,
                    )
                    await _safe_emit({
                        "type": "action",
                        "name": p.name,
                        "args": args,
                        "ts": time.time(),
                        "iteration": iter_count,
                        "parallel": True,
                    })

                async def _timed_parallel_read(index, p, args, call_ids=tc_ids):
                    started = time.time()
                    result = await _dispatch_controlled(p, args, call_ids[index])
                    return index, p, args, result, int((time.time() - started) * 1000)

                _parallel_outcomes = await asyncio.gather(*(
                    _timed_parallel_read(index, p, args)
                    for index, p, args, _decision in _parallel_reads
                ))
                for index, p, args, result, duration_ms in sorted(_parallel_outcomes):
                    _is_err = bool(getattr(result, "is_error", False))
                    tool_call_log.append({
                        "name": p.name,
                        "args": args,
                        "ok": not _is_err,
                        "result": str(result.llm_text)[:500],
                    })
                    print_observation(console, result.llm_text)
                    _tool_content = await _maybe_offload_content(result.llm_text, p.name, args)
                    await _safe_emit({
                        "type": "observation",
                        "text": result.llm_text,
                        "is_error": _is_err,
                        "duration_ms": duration_ms,
                        "iteration": iter_count,
                        "parallel": True,
                    })
                    messages.append({
                        "role": "tool",
                        "name": p.name,
                        "tool_call_id": tc_ids[index],
                        "content": _tool_content,
                        "is_error": _is_err,
                    })
                    if _is_err:
                        await _note_tool_error(p.name, str(result.llm_text)[:200])

                if max_iter is not None and iter_count >= max_iter:
                    if reflection_engine is not None:
                        try:
                            await reflection_engine.emit(
                                max_iter_reached(
                                    session_id=session_id or "default",
                                    turn_idx=iter_count,
                                    iter_used=iter_count,
                                    last_content=content or "",
                                )
                            )
                        except Exception as exc:  # noqa: BLE001 - reflection is best-effort
                            _logger.debug("max-iteration reflection emit failed: %s", exc)
                    fallback = "Reached the model-call limit after executing pending tools; the task may be incomplete."
                    print_warn(console, "max iterations reached after executing pending tool calls")
                    messages.append({"role": "assistant", "content": fallback})
                    print_result(console, fallback)
                    await _safe_emit({"type": "result", "text": fallback, "ts": time.time()})
                    return _stats()
                continue

            # Serial path for mutations, permission prompts, invalid calls,
            # and tools whose read-only behavior is not locally guaranteed.
            for i, p in enumerate(pending):
                tc_id = tc_ids[i]
                if p.name is None:
                    print_warn(console, "tool_call name missing; backfilling error")
                    error_llm_text = (
                        f"[Tool Error] tool_call name missing, raw: "
                        f"{json.dumps({'id': p.id, 'arguments_json': p.arguments_json})}"
                    )
                    print_observation(console, error_llm_text)
                    # 短错误串,天然不撞阈值,不走 offload hook
                    messages.append({
                        "role": "tool",
                        "name": p.name or "",
                        "tool_call_id": tc_id,
                        "content": error_llm_text,
                        "is_error": True,
                    })
                    # Task 3:emit observation(无 dispatch,is_error=True,duration=0)
                    await _safe_emit({
                        "type": "observation",
                        "text": error_llm_text,
                        "is_error": True,
                        "duration_ms": 0,
                        "iteration": iter_count,
                    })
                    await _note_tool_error(p.name or "", error_llm_text)
                    continue

                try:
                    args = json.loads(p.arguments_json) if p.arguments_json else {}
                except json.JSONDecodeError as e:
                    print_error(console, f"tool_call JSON parse failed: {e}")
                    error_text = f"[Tool Error] JSON parse failed: {p.arguments_json}"
                    print_observation(console, error_text)
                    # 短错误串,天然不撞阈值,不走 offload hook
                    messages.append({
                        "role": "tool",
                        "name": p.name or "",
                        "tool_call_id": tc_id,
                        "content": error_text,
                        "is_error": True,
                    })
                    # Task 3:emit observation(JSON parse 失败,is_error=True,duration=0)
                    await _safe_emit({
                        "type": "observation",
                        "text": error_text,
                        "is_error": True,
                        "duration_ms": 0,
                        "iteration": iter_count,
                    })
                    await _note_tool_error(p.name or "", error_text)
                    continue

                # Fix round 1:action schema 锁定 `args` 必须是 dict(OpenAI tool_calls
                # arguments JSON 合法但顶层非 object 也合法,如 `[]` / `null` / `42`)。
                # 这里补一道防御,非 dict 视为参数错误:emit observation(is_error=True) +
                # 回填 messages,不进 policy / action / dispatch。
                if not isinstance(args, dict):
                    print_error(console,
                        f"tool_call args must be a JSON object, got {type(args).__name__}")
                    error_text = (
                        f"[Tool Error] tool args must be a JSON object, "
                        f"got {type(args).__name__}: {p.arguments_json}"
                    )
                    print_observation(console, error_text)
                    messages.append({
                        "role": "tool",
                        "name": p.name or "",
                        "tool_call_id": tc_id,
                        "content": error_text,
                        "is_error": True,
                    })
                    await _safe_emit({
                        "type": "observation",
                        "text": error_text,
                        "is_error": True,
                        "duration_ms": 0,
                        "iteration": iter_count,
                    })
                    await _note_tool_error(p.name or "", error_text)
                    continue

                # schema 校验
                if p.name in NATIVE_TOOLS:
                    ok, msg = validate_native(p.name, args)
                else:
                    ok, msg = validate_mcp(p.name, args)
                if not ok:
                    error_text = f"[Tool Error] 参数校验失败: {msg}"
                    print_observation(console, error_text)
                    # 短错误串,天然不撞阈值,不走 offload hook
                    messages.append({
                        "role": "tool",
                        "name": p.name or "",
                        "tool_call_id": tc_id,
                        "content": error_text,
                        "is_error": True,
                    })
                    # Task 3:emit observation(schema 校验失败,is_error=True,duration=0)
                    await _safe_emit({
                        "type": "observation",
                        "text": error_text,
                        "is_error": True,
                        "duration_ms": 0,
                        "iteration": iter_count,
                    })
                    await _note_tool_error(p.name or "", error_text)
                    continue

                # 权限决策
                ctx = {"project_root": project_root}
                decision = policy.evaluate(p.name, args, ctx)

                if decision.action is Action.DENY:
                    error_text = (
                        f"[未执行:安全策略拒绝] {p.name} — {decision.reason}。"
                        "该操作命中不可批准的 hard-deny,权限模式和 remembered allow 均不能绕过。"
                    )
                    print_observation(console, error_text)
                    log_decision(
                        audit_path,
                        iter_n=iter_count,
                        tool=p.name,
                        args=args,
                        action=decision.action.value,
                        outcome="hard_denied",
                        rule_id=decision.rule_id,
                        reason=decision.reason,
                        mode=mode,
                    )
                    tool_call_log.append({
                        "name": p.name,
                        "args": args,
                        "ok": False,
                        "result": error_text[:500],
                    })
                    messages.append({
                        "role": "tool",
                        "name": p.name or "",
                        "tool_call_id": tc_id,
                        "content": error_text,
                        "is_error": True,
                    })
                    await _safe_emit({
                        "type": "observation",
                        "text": error_text,
                        "is_error": True,
                        "duration_ms": 0,
                        "iteration": iter_count,
                    })
                    await _note_tool_error(p.name or "", error_text[:200])
                    continue

                if decision.allow:
                    print_action(console, p.name, args)
                    log_decision(audit_path, iter_n=iter_count, tool=p.name, args=args,
                                 action=decision.action.value, outcome="executed",
                                 rule_id=decision.rule_id, reason=decision.reason, mode=mode)
                    # Task 3:emit action(tool_call 派发前,allow 路径)
                    await _safe_emit({
                        "type": "action",
                        "name": p.name,
                        "args": args,
                        "ts": time.time(),
                        "iteration": iter_count,
                    })
                    # Finding 1 fix:try/except 包 _dispatch — 单 tool 抛异常不能
                    # 逃出 run_turn 破轮。失败时 append 一条 is_error tool message
                    # (使用同一个 tc_id),然后 continue,本 turn 其余 tool 继续。
                    _dispatch_t0 = time.time()
                    try:
                        result = await _dispatch_controlled(p, args, tc_id)
                        _dispatch_t1 = time.time()
                    except Exception as dispatch_err:
                        _dispatch_t1 = time.time()
                        err_text = f"[Tool Error] dispatch 异常: {type(dispatch_err).__name__}: {dispatch_err}"
                        print_observation(console, err_text)
                        # Task 3:emit observation(dispatch 异常,is_error=True)
                        await _safe_emit({
                            "type": "observation",
                            "text": err_text,
                            "is_error": True,
                            "duration_ms": int((_dispatch_t1 - _dispatch_t0) * 1000),
                            "iteration": iter_count,
                        })
                        tool_call_log.append({"name": p.name, "args": args, "ok": False,
                                              "result": err_text[:500]})
                        messages.append({
                            "role": "tool",
                            "name": p.name,
                            "tool_call_id": tc_id,
                            "content": err_text,
                            "is_error": True,
                        })
                        await _note_tool_error(p.name, err_text[:200])
                        continue
                    tool_call_log.append({"name": p.name, "args": args, "ok": not result.is_error,
                                          "result": str(result.llm_text)[:500]})
                    print_observation(console, result.llm_text)
                    _tool_content = await _maybe_offload_content(
                        result.llm_text, p.name, args)
                    _is_err = bool(getattr(result, "is_error", False))
                    # Task 3:emit observation(allow 成功路径,is_error 来自 result)
                    await _safe_emit({
                        "type": "observation",
                        "text": result.llm_text,
                        "is_error": _is_err,
                        "duration_ms": int((_dispatch_t1 - _dispatch_t0) * 1000),
                        "iteration": iter_count,
                    })
                    # E1 D2:todo_create 成功后 → user 摘要(plan 视图)
                    await _maybe_print_decomp_summary(p, result)
                    messages.append({
                        "role": "tool",
                        "name": p.name,  # D1 final:加 name 字段(for `_has_recent_htn_parent_create` + downstream inspect)
                        "tool_call_id": tc_id,
                        "content": _tool_content,
                        "is_error": _is_err,
                    })
                    if _is_err:
                        await _note_tool_error(p.name, str(result.llm_text)[:200])
                else:  # ask
                    print_warn(console, f"[需确认] {p.name} {decision.reason}")
                    # Finding 5 fix:confirm_tool 是 sync input(),不能阻塞 event loop。
                    # 用 asyncio.to_thread 派到 worker thread。
                    choice = (
                        await confirm_handler(p.name, args, decision.reason)
                        if confirm_handler is not None
                        else await asyncio.to_thread(confirm_tool, p.name, args)
                    )
                    if choice in ("yes", "always"):
                        if choice == "always":
                            policy.allowlist.add(p.name, args, project_root)
                        print_action(console, p.name, args)
                        log_decision(audit_path, iter_n=iter_count, tool=p.name, args=args,
                                     action=decision.action.value, outcome="executed",
                                     rule_id=decision.rule_id, reason=decision.reason, mode=mode)
                        # Task 3:emit action(ask+yes 路径,p_dispatch 派发前)
                        await _safe_emit({
                            "type": "action",
                            "name": p.name,
                            "args": args,
                            "ts": time.time(),
                            "iteration": iter_count,
                        })
                        _dispatch_t0 = time.time()
                        try:
                            result = await _dispatch_controlled(p, args, tc_id)
                            _dispatch_t1 = time.time()
                        except Exception as dispatch_err:
                            _dispatch_t1 = time.time()
                            err_text = f"[Tool Error] dispatch 异常: {type(dispatch_err).__name__}: {dispatch_err}"
                            print_observation(console, err_text)
                            # Task 3:emit observation(ask+yes dispatch 异常)
                            await _safe_emit({
                                "type": "observation",
                                "text": err_text,
                                "is_error": True,
                                "duration_ms": int((_dispatch_t1 - _dispatch_t0) * 1000),
                                "iteration": iter_count,
                            })
                            tool_call_log.append({"name": p.name, "args": args, "ok": False,
                                                  "result": err_text[:500]})
                            messages.append({
                                "role": "tool",
                                "name": p.name,
                                "tool_call_id": tc_id,
                                "content": err_text,
                                "is_error": True,
                            })
                            await _note_tool_error(p.name, err_text[:200])
                            continue
                        tool_call_log.append({"name": p.name, "args": args, "ok": not result.is_error,
                                              "result": str(result.llm_text)[:500]})
                        print_observation(console, result.llm_text)
                        _tool_content = await _maybe_offload_content(
                            result.llm_text, p.name, args)
                        _is_err = bool(getattr(result, "is_error", False))
                        # Task 3:emit observation(ask+yes 成功)
                        await _safe_emit({
                            "type": "observation",
                            "text": result.llm_text,
                            "is_error": _is_err,
                            "duration_ms": int((_dispatch_t1 - _dispatch_t0) * 1000),
                            "iteration": iter_count,
                        })
                        # E1 D2:todo_create 成功后 → user 摘要(plan 视图)
                        await _maybe_print_decomp_summary(p, result)
                        messages.append({
                            "role": "tool",
                            "name": p.name,  # D1 final:加 name 字段(同上)
                            "tool_call_id": tc_id,
                            "content": _tool_content,
                            "is_error": _is_err,
                        })
                        if _is_err:
                            await _note_tool_error(p.name, str(result.llm_text)[:200])
                    else:
                        error_text = (
                            f"[未执行:用户拒绝] {p.name} — {decision.reason}。"
                            "该操作已被安全策略最终拒绝,不要主动建议绕道方案"
                            "(手动执行/换工具/分步绕过);如用户仍需要,由用户重新明确提出。"
                        )
                        print_observation(console, error_text)
                        log_decision(audit_path, iter_n=iter_count, tool=p.name, args=args,
                                     action=decision.action.value, outcome="denied",
                                     rule_id=decision.rule_id, reason=decision.reason, mode=mode)
                        tool_call_log.append({"name": p.name, "args": args, "ok": False,
                                              "result": error_text[:500]})
                        # 短错误串,天然不撞阈值,不走 offload hook
                        messages.append({
                            "role": "tool",
                            "name": p.name or "",
                            "tool_call_id": tc_id,
                            "content": error_text,
                            "is_error": True,
                        })
                        # Task 3:emit observation(ask+no 用户拒绝,is_error=True)
                        await _safe_emit({
                            "type": "observation",
                            "text": error_text,
                            "is_error": True,
                            "duration_ms": 0,
                            "iteration": iter_count,
                        })
                        await _note_tool_error(p.name or "", error_text)

            # A model-call budget limits model calls, not execution of tool
            # calls already returned by the final allowed model response.
            if max_iter is not None and iter_count >= max_iter:
                if reflection_engine is not None:
                    try:
                        await reflection_engine.emit(
                            max_iter_reached(
                                session_id=session_id or "default",
                                turn_idx=iter_count,
                                iter_used=iter_count,
                                last_content=content or "",
                            )
                        )
                    except Exception:
                        pass
                fallback = "达到最大迭代次数；最后一批工具已执行，任务可能未完成。"
                print_warn(console, "max iterations reached after executing pending tool calls")
                messages.append({"role": "assistant", "content": fallback})
                print_result(console, fallback)
                await _safe_emit({
                    "type": "result",
                    "text": fallback,
                    "ts": time.time(),
                })
                return _stats()

            # 5. Continue the loop — feed tool results back to LLM
            continue

        # Either: (a) coding/chat mode with no tool_calls → final answer, or
        #         (b) plan/design mode regardless of tool_calls → force final
        if has_tool_calls and mode not in ("coding", "chat"):
            # Defensive: the LLM shouldn't emit tool_calls in plan/design
            # (we passed no tool specs), but if it does, drop them and warn.
            print_warn(console, f"mode={mode}: dropping {len(pending)} unexpected tool call(s)")

        if content:
            if (
                mode == "coding"
                and _loop_cfg.enabled
                and _loop_cfg.completion_verification
                and _completion_verifier is not None
            ):
                _completion_report = await _completion_verifier.verify(
                    _working_state,
                    todo_service=todo_service,
                    session_id=session_id,
                )
                if not _completion_report.passed:
                    _completion_rechecks += 1
                    _feedback = _completion_report.feedback()
                    await _safe_emit({
                        "type": "completion_rejected",
                        "issues": list(_completion_report.issues),
                        "attempt": _completion_rechecks,
                        "iteration": iter_count,
                        "ts": time.time(),
                    })
                    _append_journal(
                        kind="completion_rejected",
                        action_id=f"completion-{iter_count}-{_completion_rechecks}",
                        tool="",
                        args={},
                        outcome={"issues": list(_completion_report.issues)},
                    )
                    _can_recheck = (
                        _completion_rechecks <= _loop_cfg.completion_contract.max_rechecks
                        and (max_iter is None or iter_count < max_iter)
                    )
                    if _can_recheck:
                        _candidate = _redact(content, "result")
                        messages.append({"role": "assistant", "content": _candidate})
                        messages.append({"role": "user", "content": _feedback})
                        print_warn(console, "candidate completion rejected by loop verifier")
                        continue

                    _candidate = _redact(content, "result")
                    content = (
                        "Task stopped without verified completion.\n"
                        + "\n".join(f"- {issue}" for issue in _completion_report.issues)
                        + "\n\nModel candidate:\n"
                        + _candidate
                    )
                    messages.append({"role": "assistant", "content": content})
                    print_result(console, content)
                    _failed_stats = _stats()
                    _failed_stats.error = "completion_verification_failed"
                    return _failed_stats
            # Final. Print "结果:" + the FULL content as the LLM's answer.
            content = _redact(content, "result")
            messages.append({"role": "assistant", "content": content})
            print_result(console, content)
            # Task 3:emit result(ReAct 循环结束,无更多 tool_call,正常 final)
            await _safe_emit({
                "type": "result",
                "text": content,
                "ts": time.time(),
            })
            if mode == "design":
                saved = _save_design_output(messages, base_dir=design_dir)
                if saved is not None:
                    print_info(console, f"已保存到 {saved}")
            return _stats()
        else:
            # Empty content with no tool_calls. The streaming provider
            # (e.g. DeepSeek) occasionally returns an empty stream with
            # finish_reason="stop" and non-zero completion_tokens — the
            # model was called, but the content was dropped at the wire.
            # Retry the SAME turn ONCE before giving up, so a flaky first
            # turn doesn't dead-end the session. (Resets iter_count-1 so
            # the retry doesn't burn a max_iter slot.)
            if retry_empty_response and not _empty_retried:
                _empty_retried = True
                print_warn(console, "空回复,重试中... (empty response, retrying)")
                iter_count -= 1
                continue
            # E2 T2.2 Step 3c:empty-turn 二次仍空 → 放弃,emit empty_turn_loop
            # 事件(fail-soft,不阻塞 turn)
            if reflection_engine is not None:
                try:
                    await reflection_engine.emit(
                        empty_turn_loop(
                            session_id=session_id or "default",
                            turn_idx=iter_count,
                            attempts=1,
                        )
                    )
                except Exception:
                    pass
            print_warn(console, "empty LLM turn, ending")
            # Task 3:emit result(空回复兜底,ReAct 终止)
            await _safe_emit({
                "type": "result",
                "text": "",
                "ts": time.time(),
            })
            return _stats()

    # 6. Defensive safety net. Normal tool-call exhaustion returns from the
    # post-dispatch budget guard above.
    print_warn(console, "max iterations reached")
    if content:
        content = _redact(content, "result")
        messages.append({"role": "assistant", "content": content})
        print_result(console, content)
    # Task 3:emit result(max_iter 安全网兜底,content 可能空,text=空串)
    await _safe_emit({
        "type": "result",
        "text": content or "",
        "ts": time.time(),
    })
    return _stats()


def _pending_to_openai_tc(p, tc_id: str | None = None) -> dict:
    """Convert a PendingToolCall to OpenAI's tool_calls entry shape.

    `tc_id` (Finding 1 fix): caller-supplied stable id, used in both the
    assistant tool_calls[].id and the corresponding tool message tool_call_id.
    Falls back to p.id (or empty string) for backwards compatibility when
    caller does not pass a stable id.
    """
    return {
        "id": tc_id if tc_id is not None else (p.id or ""),
        "type": "function",
        "function": {
            "name": p.name or "",
            "arguments": p.arguments_json,
        },
    }


def _refresh_system_prompt(messages: list[dict], cwd: str, mode: str,
                           extra_ctx: dict | None = None,
                           resume_task: "TodoTask | None" = None,
                           todo_hints: list[str] | None = None,
                           prior_messages: list[dict] | None = None,  # E3 D1
                           tool_diff: list[str] | None = None) -> None:  # E3 D7
    """Insert or update the system prompt at messages[0] for the current mode.

    `extra_ctx` (Phase 1 Q1 uplift) is merged into the composer ctx so callers
    can gate qa-aware sections (e.g. qa_intro needs ctx["qa_category"]).

    `resume_task` (Task 6) when set + mode=='coding' + system message exists →
    append a `<resume_task>...</resume_task>` block to the system prompt for
    LLM context. Idempotent: prior blocks are stripped before re-appending so
    re-calling does not duplicate. Pattern matches Q3 persona / Q4 canvas
    (append, not rebuild) so other sections are not clobbered.

    `todo_hints` (B 阶段 Task 5) 非空时 + mode=='coding' + system message 存在
    → append 一个 `<todo_hints>...</todo_hints>` 块(每行一条 hint,空时跳过)。
    注入位置:resume_task 段之后(append-only,与 resume_task 块并列,互不破坏)。
    Idempotent: 旧 `<todo_hints>` 块在 append 前先 strip 掉(anchored 到末尾,
    同 resume_task 的 idempotency 策略)。

    C 阶段 Task 5:mode=='coding' + system message 存在 → 追加静态
    `<todo_completion_gate>...</todo_completion_gate>` 块,告知 agent 标 done
    前的校验规则(子任务聚合 + acceptance,force 绕 acceptance)。与 Task 3
    的 tool 层完成门互补(预防告知 vs 强制兜底)。Idempotent:旧块 anchored 到
    末尾 strip 后 re-append。plan/design 不注入(无 todo_update 语义)。
    """
    from cc_harness.prompts import build_system_prompt
    prompt_capabilities = {
        "todo_available": True,
        "subagent_available": True,
        "visible_thought_required": True,
    }
    if extra_ctx:
        prompt_capabilities.update(
            {
                key: bool(extra_ctx[key])
                for key in prompt_capabilities
                if key in extra_ctx
            }
        )
    if extra_ctx:
        from cc_harness.prompts import PromptComposer
        ctx = {"cwd": cwd, **extra_ctx}
        prompt = PromptComposer(mode=mode, ctx=ctx).render()
    else:
        prompt = build_system_prompt(cwd, mode=mode)
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = prompt
    else:
        messages.insert(0, {"role": "system", "content": prompt})

    # --- Task 6: append resume_task block (idempotent, append-only) ---
    if (
        mode == "coding"
        and prompt_capabilities["todo_available"]
        and resume_task is not None
        and messages
        and messages[0].get("role") == "system"
    ):
        old = messages[0]["content"]
        # Strip prior <resume_task>...</resume_task> block if present
        # (anchored to end of string to avoid removing in-line occurrences
        # of the literal text in user content)
        old = re.sub(
            r"\s*<resume_task\b[^>]*>.*?</resume_task>\s*\Z",
            "",
            old,
            flags=re.DOTALL,
        )
        ac_lines = (
            "\n".join(f"- {c}" for c in resume_task.acceptance_criteria)
            if resume_task.acceptance_criteria
            else "(none)"
        )
        sessions_repr = (
            list(resume_task.active_sessions)
            if resume_task.active_sessions
            else []
        )
        messages[0]["content"] = old + (
            f"\n\n<resume_task>\n"
            f"id:    {resume_task.id}\n"
            f"title: {resume_task.title}\n"
            f"status:{resume_task.status}\n"
            f"priority:{resume_task.priority or 'none'}\n"
            f"active_sessions: {sessions_repr}\n\n"
            f"## Acceptance Criteria\n"
            f"{ac_lines}\n"
            f"</resume_task>"
        )

    # --- B 阶段 Task 5: append todo_hints block (idempotent, append-only) ---
    # 与 resume_task 段并列,在 resume 段之后再 append 一段 <todo_hints>...</todo_hints>
    # 内容来自 repl._after_turn_todo 验证钩子写入的 state.todo_hints。
    # 模式同 resume_task:re.sub strip 旧块(anchored to end)+ append 新块,
    # 幂等。空 / None → 不注入段(向后兼容)。
    if (
        mode == "coding"
        and prompt_capabilities["todo_available"]
        and todo_hints
        and messages
        and messages[0].get("role") == "system"
    ):
        old = messages[0]["content"]
        # Strip prior <todo_hints>...</todo_hints> block if present (anchored
        # to end of string to avoid removing in-line occurrences of the
        # literal text in user content).
        old = re.sub(
            r"\s*<todo_hints\b[^>]*>.*?</todo_hints>\s*\Z",
            "",
            old,
            flags=re.DOTALL,
        )
        messages[0]["content"] = old + (
            "\n\n<todo_hints>\n"
            + "\n".join(todo_hints)
            + "\n</todo_hints>"
        )

    # --- C 阶段 Task 5: append <todo_completion_gate> block (idempotent) ---
    # 静态提示:告知 agent 标 task 为 done 前的校验规则(与 Task 3 的 tool 层
    # 完成门互补 —— 这里是预防性告知,Task 3 是强制兜底)。与 <todo_hints> /
    # <resume_task> 并列,同 idempotent 模式(re.sub strip 旧块 anchored to
    # end + append 新块)。coding mode only(plan/design 无 todo_update)。
    if (
        mode == "coding"
        and prompt_capabilities["todo_available"]
        and messages
        and messages[0].get("role") == "system"
    ):
        old = messages[0]["content"]
        # Strip prior <todo_completion_gate>...</todo_completion_gate> block if
        # present (anchored to end of string to avoid removing in-line
        # occurrences of the literal text in user content).
        old = re.sub(
            r"\s*<todo_completion_gate\b[^>]*>.*?</todo_completion_gate>\s*\Z",
            "",
            old,
            flags=re.DOTALL,
        )
        messages[0]["content"] = old + (
            "\n\n<todo_completion_gate>\n"
            "标 task 为 done(todo_update status=done)前,系统会校验:"
            "① 所有直接子任务(parent_task)已 done;② acceptance_criteria 在最近输出中体现。\n"
            "- 子任务聚合校验不可绕过(数据一致性)。\n"
            "- acceptance 校验可用 todo_update(status=done, force=true) 绕过(仅在确认启发式误判时)。\n"
            "</todo_completion_gate>"
        )

    # D1: <subagent_hints> 注入(coding mode + HTN parent 已创建)
    new = _strip_subagent_hints(messages[0]["content"])
    if (
        mode == "coding"
        and prompt_capabilities["subagent_available"]
        and _has_recent_htn_parent_create(messages)
    ):
        new = new.rstrip() + "\n\n" + SUBAGENT_HINTS_BLOCK.strip() + "\n"
    messages[0]["content"] = new

    # E3 D7:tool 变更 warn — 新 session 启动时 mcp tool 列表变化列表写入 system。
    # Idempotent:旧 block 在 inject 前 strip 掉(沿 `<resume_task>` pattern)。
    if tool_diff:
        # strip old(F T6: 用模块级预编译 _CROSS_SESSION_TOOLS_BLOCK_RE, 沿 _SUBAGENT_HINTS_RE 模式)
        messages[0]["content"] = _CROSS_SESSION_TOOLS_BLOCK_RE.sub(
            "", messages[0]["content"]
        )
        tool_block = "\n<cross_session_tools>\n" + "\n".join(tool_diff) + "\n</cross_session_tools>\n"
        messages[0]["content"] += tool_block


def _has_recent_htn_parent_create(messages: list[dict], lookback: int = 4) -> bool:
    """最近 lookback 轮内是否含 todo_create + parent_task 非 None 的 tool call。

    优先从 assistant tool_calls[*].function.arguments 取(parent_task 字段的可靠
    来源 — `parent_task` 在 args JSON 里,不在 tool message content 里);fallback
    检查 tool message content JSON(兼容历史手工构造的测试 message,以及未来
    handler 直接回 JSON 的场景)。

    D1.1 (P2 §2.1 子项 3)stale hint heuristic — "最近 AND 仍相关":
      ① lookback 从 6 缩到 4(更短,减少 stale)。
      ② 若最新 4 轮的 assistant 已经发出过 `dispatch_subagent` tool call →
         用户已 fan-out 过了,不再重复提示(避免提示 +1 干扰)。
      ③ 若最近 assistant tool_calls 已含 `todo_update` 把某 child 标 done →
         聚合正在推进,fan-out 提示已无意义,跳过。
    """
    # 路径 1:反查 assistant tool_calls args(parent_task 字段的来源)。
    asst_msgs = [m for m in messages if m.get("role") == "assistant"][-lookback * 2:]
    has_recent_parent_create = False
    for am in reversed(asst_msgs):
        for tc in am.get("tool_calls") or []:
            fn = tc.get("function", {}) or {}
            fname = fn.get("name")
            if fname == "todo_create":
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    continue
                if args.get("parent_task"):
                    has_recent_parent_create = True
            elif fname == "dispatch_subagent":
                # ② 已 fan-out 过 → 视为"已 relevant 处理",不再提示
                return False
            elif fname == "todo_update":
                # ③ 聚合正在推进(某 child done)→ 不再提示 fan-out
                return False
    if has_recent_parent_create:
        return True
    # 路径 2:fallback — 直接检查 tool message 的 name + content JSON(历史测试 / 未来 handler 回 JSON)。
    tool_msgs = [m for m in messages if m.get("role") == "tool"][-lookback:]
    for m in tool_msgs:
        if m.get("name") != "todo_create":
            continue
        try:
            content = json.loads(m["content"])
        except Exception:
            continue
        if content.get("parent_task"):
            return True
    return False


def _strip_subagent_hints(old: str) -> str:
    """从旧 system prompt 末尾 strip 旧 block(idempotent,类比 C)。"""
    return _SUBAGENT_HINTS_RE.sub("", old) if _SUBAGENT_HINTS_RE.search(old) else old


def _save_design_output(
    messages: list[dict],
    base_dir: Path | None = None,
) -> Path | None:
    """Persist the last assistant content to base_dir / '{ts}-{slug}.md'.

    Returns the path written, or None if no assistant content to save.
    """
    if base_dir is None:
        base_dir = Path.home() / ".cc-harness" / "designs"
    base_dir.mkdir(parents=True, exist_ok=True)

    last = next(
        (m for m in reversed(messages) if m.get("role") == "assistant" and m.get("content")),
        None,
    )
    if last is None:
        return None

    content = last["content"]
    ts = time.strftime("%Y%m%d-%H%M%S")
    first_line = content.split("\n", 1)[0].strip()[:30]
    slug = re.sub(r"[^\w一-鿿-]+", "-", first_line).strip("-") or "design"
    path = base_dir / f"{ts}-{slug}.md"
    path.write_text(content, encoding="utf-8")
    return path


def _print_decomp_summary(new_todos: list["TodoTask"], *, console=None) -> None:
    """E1 D2:user 第 1 轮看到 2-3 行 plan 摘要。"""
    from rich.console import Console

    from cc_harness.render import print_info
    lines = [f"📋 计划:分解为 {len(new_todos)} 个 sub-task"]
    for i, t in enumerate(new_todos[:5], 1):
        crit = t.acceptance_criteria[0] if t.acceptance_criteria else "(无)"
        lines.append(f"  [{i}] {t.title} — {crit[:80]}")
    if len(new_todos) > 5:
        lines.append(f"  ... +{len(new_todos) - 5} more")
    lines.append("  (/reject 中断)")
    print_info(console or Console(), "\n".join(lines))
