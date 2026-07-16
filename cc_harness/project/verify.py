"""B 阶段组件 2: verify hook(heuristic + state 双轨)。

3 个纯函数 + 1 dataclass:
- heuristic_check(criteria, text) -> (passed, missing)
- state_check(task, all_tasks) -> (deps_ready, hint_or_none)
- run_verify(task, all_tasks, last_turn_text) -> VerifyResult
- VerifyResult dataclass

字段语义:
- passed — heuristic AND state 整体是否通过
- missing_criteria — heuristic 失败的 criterion(只在 heuristic 失败时填)
- hints — 辅助信号(state 失败 / 无产出),调用方无条件采纳

本模块是纯逻辑,不依赖 agent / repl / tools / memory / l2 / l5 / policy /
executor / sandbox。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from cc_harness.project.models import TodoTask


# ---------------------------------------------------------------------------
# Tokenizer + stopword(简陋版,YAGNI 起步,误判多再扩)
# ---------------------------------------------------------------------------


# 任务指令 verbatim stopword 列表:中英文各 10 个
_STOPWORDS: frozenset[str] = frozenset({
    # 英文
    "the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "on",
    # 中文
    "的", "了", "和", "是", "在", "有", "我", "你", "他", "她", "它",
})

# 拆词:英文 ASCII word + 中文 [一-鿿] 单字
# (Python 3 下 \w 默认含 CJK,会吞整段中文为一个 token,故显式 ASCII)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[一-鿿]")


def _keywords(text: str) -> set[str]:
    """拆词 + 去 stopword。返回非停关键词集合。"""
    tokens = _TOKEN_RE.findall(text.lower())
    return {t for t in tokens if t not in _STOPWORDS}


# ---------------------------------------------------------------------------
# heuristic_check
# ---------------------------------------------------------------------------


def heuristic_check(
    criteria: list[str], text: str
) -> tuple[bool, list[str]]:
    """启发式检查 text 是否覆盖 criteria 每一条。

    规则:
        - 空 criteria -> (True, [])
        - text 空 -> (False, criteria)
        - text 拆词后空(全 stopword) -> (False, criteria)
        - criterion 拆词去 stopword 后至少 1 个关键词在 text 拆词集合 -> 通过
        - criterion < 3 字符 -> 跳过(视为通过)
        - criterion 拆词后空(全 stopword) -> 跳过(视为通过)

    Returns:
        (passed, missing) — passed=True 当所有非跳过的 criterion 都覆盖
    """
    if not criteria:
        return True, []
    if not text:
        return False, list(criteria)

    text_kw = _keywords(text)
    if not text_kw:
        # text 全 stopword 极端情况
        return False, list(criteria)

    missing: list[str] = []
    for criterion in criteria:
        if len(criterion) < 3:
            continue  # 短 criterion 跳过
        kw = _keywords(criterion)
        if not kw:
            continue  # 全 stopword 跳过
        if not (kw & text_kw):
            missing.append(criterion)
    return (len(missing) == 0), missing


# ---------------------------------------------------------------------------
# VerifyResult
# ---------------------------------------------------------------------------


@dataclass
class VerifyResult:
    """单 task 的 verify 输出。"""

    task_id: str
    passed: bool                                  # 整体是否通过
    missing_criteria: list[str] = field(default_factory=list)  # heuristic 未命中
    hints: list[str] = field(default_factory=list)             # 给 LLM 的提示


# ---------------------------------------------------------------------------
# state_check
# ---------------------------------------------------------------------------


def state_check(
    task: TodoTask, all_tasks: dict[str, TodoTask]
) -> tuple[bool, str | None]:
    """状态机检查 — depends_on 全 done?

    'done' 视为已就绪;不存在于字典的依赖 id 视为不阻塞(由 validate 报)。

    Returns:
        (deps_ready, hint_or_none) — hint 是 "task X 依赖未就绪: Y" 提示
    """
    if not task.depends_on:
        return True, None

    missing_deps: list[str] = []
    for dep_id in task.depends_on:
        if dep_id not in all_tasks:
            continue  # 缺失依赖不阻塞
        if all_tasks[dep_id].status != "done":
            missing_deps.append(dep_id)

    if not missing_deps:
        return True, None
    chain = ", ".join(missing_deps)
    hint = f"task {task.id} 依赖未就绪: {chain}"
    return False, hint


# ---------------------------------------------------------------------------
# run_verify
# ---------------------------------------------------------------------------


def run_verify(
    task: TodoTask,
    all_tasks: dict[str, TodoTask],
    last_turn_text: str,
) -> VerifyResult:
    """组合 heuristic + state。

    短路顺序:
        1. status != in_progress -> passed=True 全空(no-op)
        2. last_turn_text 空 + 有 criteria -> passed=True + "无产出" hint(info, 不阻断)
        3. heuristic 仅在 criteria 非空时跑(text 空时 heuristic_check 已给 fail)
        4. state_check 总是跑(即便 criteria 空,也要看 deps)

    字段语义:
        - passed — heuristic_passed AND deps_ready
        - missing_criteria — heuristic 失败的 criterion(仅 heuristic 失败时填)
        - hints — 辅助信号(state 失败 / 无产出),调用方无条件采纳
    """
    # 1. 非 in_progress -> no-op
    if task.status != "in_progress":
        return VerifyResult(
            task_id=task.id, passed=True, missing_criteria=[], hints=[]
        )

    hints: list[str] = []

    # 2. last_turn_text 空 + 有 criteria -> info hint(不阻断)
    if not last_turn_text.strip() and task.acceptance_criteria:
        hints.append(f"task {task.id} 已 in_progress 但本轮无文本产出")
        return VerifyResult(
            task_id=task.id, passed=True, missing_criteria=[], hints=hints
        )

    # 3. heuristic(criteria 非空且 text 非空才跑;空 text 已在上一步 return)
    heuristic_passed = True
    missing: list[str] = []
    if task.acceptance_criteria:
        heuristic_passed, missing = heuristic_check(
            task.acceptance_criteria, last_turn_text
        )

    # 4. state_check(总是跑,即便 criteria 空)
    deps_ready, dep_hint = state_check(task, all_tasks)
    if not deps_ready and dep_hint:
        hints.append(dep_hint)

    overall_passed = heuristic_passed and deps_ready
    return VerifyResult(
        task_id=task.id,
        passed=overall_passed,
        missing_criteria=missing if not heuristic_passed else [],
        hints=hints,
    )