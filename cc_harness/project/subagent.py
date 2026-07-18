"""SubAgent fan-out 结果数据层 (D1 Task 1)。

定义 SubAgentResult dataclass(单个 subagent 跑完的结果,8 种 status 值)
和 _extract_file_refs(从末轮文本提取文件路径)。SubAgentRunner 将在 Task 3-4 落地。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal


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