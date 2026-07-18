"""SubAgent fan-out 结果数据层 (D1 Task 1) + 提示/摘要层 (D1 Task 2)。

定义 SubAgentResult dataclass(单个 subagent 跑完的结果,8 种 status 值)
和 _extract_file_refs(从末轮文本提取文件路径)。

Task 2 新增:
- _build_subagent_system_prompt:SubAgent 派发前构造独立 system prompt
  (含 task 元数据 + 完成门提示 + 嵌套限制)。
- _render_subagent_summary:N 个 SubAgentResult 合并成结构化 ToolResult
  (display_text 给用户、llm_text 给 LLM、is_error 反映子任务聚合)。

SubAgentRunner 将在 Task 3-4 落地。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from cc_harness.project.tools import ToolResult


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
    """
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
    results: list[SubAgentResult], parent_id: str,
) -> ToolResult:
    """N 个 subagent 结果合并成结构化摘要 ToolResult。

    - display_text:简短"N/M done"统计,给用户看。
    - llm_text:每个 subagent 的 title/status/末轮/引用 + 父完成门 hint,给 LLM 看。
    - is_error:始终 False(失败聚合已在 llm_text 中表达,ToolResult 层不报错
      让主 agent 走正常路径而不是 ask 兜底)。

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

    return ToolResult(
        is_error=False,
        display_text=f"dispatch_subagent: {n} subagents, {sum(1 for r in results if r.status=='done')}/{n} done",
        llm_text="\n".join(lines),
    )