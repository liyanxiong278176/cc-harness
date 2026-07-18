"""SubAgent 单层 fan-out 运行器(D1)。

提供 SubAgentRunner.run() —— 在同 process 启独立 ReAct loop,共享 LLM/MCP/TodoService,
隔离 messages,完成后回填 ToolResult 摘要。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class SubAgentResult:
    """单个 subagent 跑完的结果。

    status 取值:
      - "done": sub-task 完成
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
    tokens_used: int = 0            # D1 暂不接 SessionTokenStats(见 decision 4)
    file_refs: list[str] = field(default_factory=list)  # 末轮提取的文件路径
    error: str | None = None        # 失败原因


_FILE_REF_PATTERN = re.compile(
    r"[\w./-]+\.(?:py|md|markdown|yaml|yml|json|toml|txt|"
    r"js|jsx|ts|tsx|css|scss|less|sass|sh|bash|zsh|"
    r"html|xml|svg|csv|sql|env|ini|cfg|conf|lock)"
    r"(?!\w)"
)


def _extract_file_refs(text: str) -> list[str]:
    """从末轮文本提取文件路径(扩展名覆盖主流 codegen 类型)。

    排序后去重(set 顺序不确定 → 排序保证测试可重复)。
    """
    return sorted(set(_FILE_REF_PATTERN.findall(text)))
