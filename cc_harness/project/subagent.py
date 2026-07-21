"""SubAgent fan-out 结果数据层 (D1 Task 1) + 提示/摘要层 (D1 Task 2)
+ 运行器底座 (D1 Task 3)。

定义 SubAgentResult dataclass(单个 subagent 跑完的结果,8 种 status 值)
和 _extract_file_refs(从末轮文本提取文件路径)。

Task 2 新增:
- _build_subagent_system_prompt:SubAgent 派发前构造独立 system prompt
  (含 task 元数据 + 完成门提示 + 嵌套限制)。
- _render_subagent_summary:N 个 SubAgentResult 合并成结构化 ToolResult
  (display_text 给用户、llm_text 给 LLM、is_error 反映子任务聚合)。

Task 3 新增:
- SubAgentRunner 类骨架(__init__ 存 llm/mcp/service/depth/root/max_iter/policy;
  `run()` 方法在 Task 4 实现)。
- get_default_runner 工厂(depth=0,无模块级单例缓存 — 重要 fix #1)。
- _subagent_err 本地 helper(避免与 tools.py:_err 重名 + TodoError 签名混淆)。

决策 6:SubAgentRunner 接收主 agent 透传的 policy(共享 L4 闸门,
不允许 subagent 单独降级)。MAX_DEPTH = 2 硬限(decision 5)。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from cc_harness.project.tools import ToolResult

log = logging.getLogger(__name__)

if TYPE_CHECKING:  # 避免循环依赖:agent.run_turn 不在模块级 import(Task 4 再引)
    from cc_harness.l5 import L5Engine
    from cc_harness.llm import LLMClient
    from cc_harness.mcp_client import MCPClient
    from cc_harness.policy import PolicyEngine
    from cc_harness.project.service import TodoService
    from cc_harness.reflection.engine import ReflectionEngine


# spec decision 5:8 个合法 status 值(Literal 做静态约束)
SubAgentStatus = Literal[
    "done",            # sub-task 完成
    "blocked",         # sub-task 完成但 acceptance 失败(C 完成门拦截)
    "incomplete",      # max_iter 耗尽(未 timeout 也未 exception,只是没做完)
    "timeout",         # 超过 timeout 秒
    "failed",          # subagent 内 tool 调 is_error=True 或抛 exception
    "in_progress",     # 仍在跑(预留;runner 在 resume 报告里填)
    "pending",         # 已入队尚未启动(resume 阶段)
    "unknown",         # runner 异常前状态未知(兜底)
]


# D1.1 (P2 §2.1 子项 4):异常分类 — auth/config、网络/限流、参数/数据、兜底。
# 用 importorskip 模式避免硬依赖 openai 包(subagent 模块无 openai import 也能
# import 进 IDE / 测试 fixture);缺 openai 时退化为空 tuple → 落到兜底 except。
try:
    import openai  # noqa: F401  (用于异常分类)
    _AUTH_EXCEPTIONS: tuple = (
        openai.AuthenticationError,
        openai.PermissionDeniedError,
        openai.NotFoundError,
    )
    _NETWORK_EXCEPTIONS: tuple = (
        openai.RateLimitError,
        openai.APIConnectionError,
        openai.APITimeoutError,
        openai.InternalServerError,
    )
except ImportError:
    _AUTH_EXCEPTIONS = ()
    _NETWORK_EXCEPTIONS = ()


@dataclass
class SubAgentResult:
    """单个 subagent 跑完的结果。

    status 取值(SubAgentStatus):
      - "done": sub-task 完成
      - "blocked": sub-task 完成但 acceptance 失败(C 完成门拦截)
      - "incomplete": max_iter 耗尽(未 timeout 也未 exception,只是没做完)
      - "timeout": 超过 timeout 秒
      - "failed": subagent 内 tool 调 is_error=True 或抛 exception
      - "in_progress": 仍在跑(resume 报告兜底)
      - "pending": 已入队尚未启动
      - "unknown": runner 异常前状态未知
    """
    task_id: str                    # subagent 改的 todo_id
    title: str                      # 原始 sub_spec.title
    status: SubAgentStatus          # sub-task 最终状态(见上)
    final_text: str = ""            # subagent 末轮 LLM 结果(≤500 字)
    duration_s: float = 0.0
    tokens_used: int = 0            # D1 暂不接 SessionTokenStats(见 decision 4)
    file_refs: list[str] = field(default_factory=list)  # 末轮提取的文件路径
    error: str | None = None        # 失败原因
    fatal_error: bool = False       # E2 T3.2 兼容字段:任何 fatal 路径失败时 = True(供 reflection 判定)


# Minor #1:path prefix 允许 0+ 字符,前后加锚定(so .env 单独出现也能匹配)
_FILE_REF_PATTERN = re.compile(
    r"(?:^|(?<=[\s/,\"\']))"        # start OR after whitespace/slash/quote
    r"[\w./-]*"                      # 0+ path chars(was 1+)
    r"\.(?:py|md|markdown|yaml|yml|json|toml|txt|"
    r"js|jsx|ts|tsx|css|scss|less|sass|sh|bash|zsh|"
    r"html|xml|svg|csv|sql|env|ini|cfg|conf|lock)"
    r"(?!\w)"
)


def _extract_file_refs(text: str) -> list[str]:
    """从末轮文本提取文件路径(扩展名覆盖主流 codegen 类型)。

    排序后去重(set 顺序不确定 → 排序保证测试可重复)。
    """
    return sorted(set(_FILE_REF_PATTERN.findall(text)))


# ---------------------------------------------------------------------------
# D1 Task 2:SubAgent system prompt 构造 + 摘要渲染
# ---------------------------------------------------------------------------


def _build_subagent_system_prompt(
    task_id: str, title: str, description: str, criteria: list[str],
    parent_id: str, depth: int,
) -> str:
    """Subagent system prompt:独立上下文 + 当前任务 + 完成门提醒。

    - description / criteria 为空时跳过对应行,不留视觉 wart。
    - depth 显式标记,提示 max_fan_out 上限 = 2。
    - 强制提醒完成门(todo_update 完成门 + acceptance 校验,非绕)。
    - D1 final:prepend `instruction_hierarchy` always-on block(从 prompts.SECTION_POOL),
      保持 subagent 同样受信任边界保护 — 防止 subagent 透过工具返回
      `<untrusted>` 块被 prompt-injection 劫持(纵深防御)。
    """
    from cc_harness.prompts import SECTION_POOL  # 延迟 import(避免 cc_harness 启动期拽 subagent 链)
    # E2 T3.3 fix: SECTION_POOL was refactored from dict to list-of-tuples in
    # 738706b (T2.1). Look up by name instead of dict key.
    hierarchy = next(
        builder({"always_included": True})
        for name, builder, _ in SECTION_POOL
        if name == "instruction_hierarchy"
    )

    parts = [
        hierarchy,
        "",
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


_STATUS_LABEL: dict[str, str] = {
    "done": "done",
    "blocked": "blocked (acceptance 未通过)",
    "incomplete": "incomplete (max_iter 耗尽, todo 未 done)",
    "timeout": "timeout",
    "failed": "failed (tool 错误或 exception)",
    "in_progress": "in_progress",
    "pending": "pending",
    "unknown": "unknown",
}


def _render_subagent_summary(
    results: list[SubAgentResult],
    parent_id: str,
    *,
    reflection_engine: "ReflectionEngine | None" = None,
) -> ToolResult:
    """N 个 subagent 结果合并成结构化摘要 ToolResult。

    - display_text:简短"N/M done"统计,给用户看。
    - llm_text:每个 subagent 的 title/status/末轮/引用 + 父完成门 hint,给 LLM 看。
    - is_error:始终 False(失败聚合已在 llm_text 中表达,ToolResult 层不报错
      让主 agent 走正常路径而不是 ask 兜底)。

    E2 T3.2:reflection_engine 非 None 时,llm_text 尾部追加 "## 最近反思(E2)" 段,
    列出 engine.get_recent(limit=3) 拿到的反思文本(供父 agent 看见自己之前 fan-out
    失败过的原因,避免重蹈覆辙)。

    无 timeout 参数(decision 3 + 开放 round 2 fix #3)。
    """
    total_duration = sum(r.duration_s for r in results)
    total_tokens = sum(r.tokens_used for r in results)
    n = len(results)
    tokens_label = f"{total_tokens}" if total_tokens > 0 else "TBD(D1.1 接 SessionTokenStats)"

    lines = [
        f"SubAgent fan-out 完成 (N={n}, 总耗时 {total_duration:.1f}s, 总 tokens: {tokens_label})",
        "",
    ]
    for i, r in enumerate(results, 1):
        status_label = _STATUS_LABEL.get(r.status, r.status)
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
        not_done = [r.task_id for r in results if r.status != "done"]
        lines.append(
            f"父完成门: 有 {len(not_done)} 个 sub-task 未 done({', '.join(not_done)}),"
            f" 父任务 {parent_id} 不可标 done(子任务聚合不可绕)。"
        )

    # E2 T3.2:追加 recent_reflections 段(失败类反思,供父 agent 看见原因)
    if reflection_engine is not None:
        try:
            recent = reflection_engine.get_recent(limit=3)
        except Exception:
            recent = []
        if recent:
            lines.append("")
            lines.append("## 最近反思(E2)")
            for r in recent:
                lines.append(f"- {r}")

    return ToolResult(
        is_error=False,
        display_text=f"dispatch_subagent: {n} subagents, {sum(1 for r in results if r.status=='done')}/{n} done",
        llm_text="\n".join(lines),
    )


# ---------------------------------------------------------------------------
# D1 Task 3:SubAgentRunner 类骨架 + get_default_runner 工厂 + _subagent_err helper
# ---------------------------------------------------------------------------


def _subagent_err(tool_name: str, msg: str) -> ToolResult:
    """dispatch_subagent 专用 error 构造器(避免与 tools.py:_err 重名 + 签名混淆)。

    tools.py 已有的 _err(tool_name, e: TodoError) 第二个参数必须是 TodoError 实例,
    dispatch_subagent 的错误不是 TodoError 语义,本地 helper 构造。
    """
    return ToolResult.error(display=msg, llm=f"[{tool_name}] {msg}")


class SubAgentRunner:
    """SubAgent 运行器骨架(decision 6:共享 LLM/MCP/Service/Policy)。

    当前实现:`__init__` 存 7 字段 + `MAX_DEPTH = 2` 常量。
    完整 `run()` 在 Task 4 落地。

    用法 (Task 4 后):
        runner = SubAgentRunner(llm, mcp, todo_service, current_depth=0,
                                 project_root=cwd, max_iter=max_iter, policy=policy)
        result = await runner.run(task_id=..., title=..., ...)
    """

    # decision 5 / spec line 378:max subagent nesting depth = 2
    MAX_DEPTH = 2

    def __init__(
        self,
        llm: "LLMClient",
        mcp: "MCPClient",
        todo_service: "TodoService",
        *,
        current_depth: int = 0,
        project_root: str = "",
        max_iter: int = 20,
        policy: PolicyEngine,
        l5: "L5Engine | None" = None,  # D1 Task 4 fix:subagent 继承主 agent 的 L5 引擎
        # E2 T3.2:反思引擎注入(失败类 status 触发 emit subagent_failed)
        reflection_engine: "ReflectionEngine | None" = None,
        session_id: str | None = None,
        turn_idx: int | None = None,
    ):
        self.llm = llm
        self.mcp = mcp
        self.todo_service = todo_service
        self.current_depth = current_depth
        self.project_root = project_root
        self.max_iter = max_iter
        self.policy = policy
        self.l5 = l5
        self.reflection_engine = reflection_engine
        self.session_id = session_id
        self.turn_idx = turn_idx

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

        5 步实现:
          1. 构造独立 messages(system + user 任务)
          2. 注入 extras(dispatch_subagent 的 deps 加 current_depth+1 runner)
          3. 调 run_turn + asyncio.wait_for timeout
          4. 收集末轮 LLM 输出 + status(判 incomplete if max_iter 耗尽)
          5. 返回 SubAgentResult(tokens_used 默认 0,decision 4)

        status 取值:
          - "timeout":超过 timeout 秒
          - "failed":subagent 内异常(fatal provider error / tool handler 抛 / L5 不丢链)
          - "incomplete":max_iter 耗尽但 todo 未 done/blocked
          - "done" / "blocked" / "in_progress":todo 实际最终状态
        """
        # 延迟 import:subagent.py 是 agent.py 的下游,避免循环
        from cc_harness.agent import run_turn
        from cc_harness.project.extras import inject_todo_tools
        from cc_harness.repl import _extract_final_text

        start = time.time()
        criteria = criteria or []

        # 1. 独立 messages
        system_prompt = _build_subagent_system_prompt(
            task_id, title, description, criteria, parent_id, self.current_depth,
        )
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": (
                f"完成任务: {title}\n\n描述: {description}" if description
                else f"完成任务: {title}"
            )},
        ]

        # 2. 注入 extras(deps 加 dispatch_subagent_runner=self 的下一层)
        # D1 Task 4 fix:next_runner 透传 l5,保持脱敏链路(subagent 思考/结果同主 agent)。
        next_runner = SubAgentRunner(
            self.llm, self.mcp, self.todo_service,
            current_depth=self.current_depth + 1,
            project_root=self.project_root,
            max_iter=self.max_iter,
            policy=self.policy,
            l5=self.l5,
        )
        extras = inject_todo_tools(
            self.todo_service, session_id, cwd=self.project_root,
            last_turn_text="",  # sub-todo 不带 criteria,完成门不查 acceptance
        )
        extras = [
            {**entry, "deps": {**entry["deps"], "dispatch_subagent_runner": next_runner}}
            if entry["spec"]["function"]["name"] == "dispatch_subagent"
            else entry
            for entry in extras
        ]

        # 3. 跑 subagent ReAct loop(超时由外层 asyncio.wait_for 控)
        # D1 Task 4 fix:传 system_prompt 跳过 mode-aware rebuild(避免主 agent
        # prompt 覆盖 subagent 独立 system);传 l5 继承脱敏(决策 6)。
        # D1.1 (P2 §2.1 子项 4):broad Exception 收窄为可识别类别 — auth/网络/
        # 参数 vs 兜底未分类 — error 字段带 type name 便于审计/log triage。
        # E2 T3.2:失败类 status 走单点 emit — 用局部 result + 末尾 try/finally 统一处理。
        result_obj: SubAgentResult | None = None
        try:
            try:
                stats = await asyncio.wait_for(
                    run_turn(
                        messages, self.llm, self.mcp,
                        cwd=self.project_root,
                        max_iter=self.max_iter,
                        extra_native_specs=extras,
                        policy=self.policy,
                        l5=self.l5,
                        system_prompt=system_prompt,
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                result_obj = SubAgentResult(
                    task_id=task_id, title=title, status="timeout",
                    error=f"subagent 超过 {timeout}s timeout",
                    duration_s=time.time() - start,
                )
            except _AUTH_EXCEPTIONS as e:
                result_obj = SubAgentResult(
                    task_id=task_id, title=title, status="failed",
                    error=f"auth/config 错误 ({type(e).__name__}): {e}",
                    duration_s=time.time() - start,
                )
            except _NETWORK_EXCEPTIONS as e:
                result_obj = SubAgentResult(
                    task_id=task_id, title=title, status="failed",
                    error=f"网络/限流 错误 ({type(e).__name__}): {e}",
                    duration_s=time.time() - start,
                )
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                result_obj = SubAgentResult(
                    task_id=task_id, title=title, status="failed",
                    error=f"参数/数据 错误 ({type(e).__name__}): {e}",
                    duration_s=time.time() - start,
                )
            except Exception as e:
                # 兜底(未识别类型)。仍 broad catch — 避免子 agent 抛意外把 dispatch
                # 路径整个挂掉,但 error 字段带 type name,便于事后 triage 升级。
                log.exception("subagent run failed (unclassified): %s", e)
                result_obj = SubAgentResult(
                    task_id=task_id, title=title, status="failed",
                    error=f"未分类错误 ({type(e).__name__}): {e}",
                    duration_s=time.time() - start,
                )

            if result_obj is None:
                # D1 Task 4 fix (Important #1):run_turn 不再 raise fatal provider
                # error,而是塞 stats.error。SubAgentRunner 检测到 → status="failed"。
                if stats.error:
                    result_obj = SubAgentResult(
                        task_id=task_id, title=title, status="failed",
                        error=f"run_turn 错误: {stats.error}",
                        duration_s=time.time() - start,
                    )

                # D1 final:扫 messages 找任一 is_error=True 的 tool message(subagent 内
                # 业务错误,如 todo_update 完成门拦) → status="failed"。
                # agent.run_turn 现在把 ToolResult.is_error 透传到 tool message;否则仅看
                # final_t.status 可能错过"工具拦了但 todo 状态没变"的失败信号。
                for m in messages:
                    if m.get("role") != "tool":
                        continue
                    if m.get("is_error"):
                        err_content = (m.get("content") or "")[:200]
                        err_name = m.get("name") or "tool"
                        result_obj = SubAgentResult(
                            task_id=task_id, title=title, status="failed",
                            error=f"tool 业务错误 ({err_name}): {err_content}",
                            duration_s=time.time() - start,
                        )
                        break

            if result_obj is None:
                # 4. 检测 max_iter 耗尽(没 timeout/exception/fatal 的话)
                try:
                    final_t = await self.todo_service.get(task_id)
                    final_status = final_t.status
                except Exception:
                    final_status = "unknown"
                iter_used = sum(1 for m in messages if m.get("role") == "assistant")
                if iter_used >= self.max_iter and final_status not in ("done", "blocked"):
                    result_obj = SubAgentResult(
                        task_id=task_id, title=title, status="incomplete",
                        error=f"max_iter={self.max_iter} 耗尽, todo 未 done/blocked",
                        duration_s=time.time() - start,
                    )

            if result_obj is None:
                # 5. 正常完成
                final_text = _extract_final_text(messages)[-500:]
                file_refs = _extract_file_refs(final_text)
                # D1.1 (P2 §2.1 子项 2):tokens_used 接 SessionTokenStats / TurnTokenStats。
                # api_total_tokens 优先(API 报告的权威账单数);若 run_turn 没报告(无 stream
                # usage)则用 breakdown_subtotal(tiktoken 估算)兜底,保证数值非 0。
                tokens = getattr(stats, "api_total_tokens", 0) or 0
                if not tokens:
                    tokens = getattr(stats, "breakdown_subtotal", 0) or 0
                result_obj = SubAgentResult(
                    task_id=task_id, title=title, status=final_status,
                    final_text=final_text, duration_s=time.time() - start,
                    tokens_used=tokens,
                    file_refs=file_refs,
                )
        except Exception:
            # 兜底:整个 run 路径异常(理论上 inner except 已收)— 保持原始行为,不 throw
            raise

        # E2 T3.2:失败类 status 单点 emit subagent_failed
        # 工厂 subagent_failed 内部按 status 映射 severity(neg/ambig/pos)
        if self.reflection_engine is not None and result_obj.status in {
            "failed", "incomplete", "timeout", "blocked"
        }:
            try:
                from cc_harness.reflection.events import subagent_failed as _sf
                result_dict = {
                    "status": result_obj.status,
                    "task_id": result_obj.task_id,
                    "final_text": result_obj.final_text,
                }
                await self.reflection_engine.emit(
                    _sf(
                        session_id=self.session_id or "default",
                        turn_idx=self.turn_idx if self.turn_idx is not None else 0,
                        result=result_dict,
                    )
                )
            except Exception:
                # 反思 emit 失败不影响主流程(silent fallback)
                pass

        return result_obj


def get_default_runner(
    llm, mcp, todo_service,
    *, project_root: str, max_iter: int, policy: PolicyEngine,
    l5: "L5Engine | None" = None,  # D1 Task 4 fix:透传主 agent L5
) -> SubAgentRunner:
    """构造主 agent 调用的 runner(depth=0)。

    **不在模块级做单例缓存**(避免多 session 跨 llm/mcp/service/policy
    复用错实例 — 重要 fix #1)。调用方(agent.run_turn)在每次 dispatch 前
    构造 1 个新实例;policy 由主 agent 透传(decision 6,共享 L4 闸门);
    l5 由主 agent 透传(D1 Task 4 fix,共享 L5 脱敏链路)。
    """
    return SubAgentRunner(
        llm, mcp, todo_service,
        current_depth=0,
        project_root=project_root,
        max_iter=max_iter,
        policy=policy,
        l5=l5,
    )


# _DEFAULT_RUNNER 单例禁留:历史 D1 plan 阶段提过"主 agent 派发前全局兜底
# 单例",已被 Important fix #1 撤掉(单例会让多 session 跨 llm / mcp / service
# 复用错实例,并冻结 policy 使 L4 共享失效)。需要显式构造时调
# get_default_runner(每次返回新实例)。