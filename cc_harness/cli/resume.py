"""`cc-harness --resume / --resume-id / --no-resume` CLI 入口。

本模块是 resume 行为的"可见化 stub":
    - REPL 启动时的自动 resume 在 Task 6 通过 `_select_resume_task`
      + SECTION_POOL 注入(system prompt 段)落地。
    - CLI 入口只把要 resume 的 task 信息打到 stdout,exit 0;
      真正的 attach to REPL 不在本任务范围。

实现策略:
    - TodoService.list/get 是 async 但底层走 sync storage;
    - 命令行运行时无 event loop — 直接 `asyncio.run()` 调一次;
    - 测试时(在 pytest-asyncio loop 内)需要同步拿到结果 — 通过 `_run_async()`
      自适应:有 running loop 时跑 sync 副本;否则 asyncio.run。
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from rich.console import Console

from cc_harness.cli._shared import (
    print_error,
    print_text,
)
from cc_harness.project.exceptions import TaskNotFound
from cc_harness.project.models import TodoTask
from cc_harness.project.storage import TodoStorage


# ---------------------------------------------------------------------------
# sync helpers — 直接走 TodoStorage (避免 asyncio.run 与 event loop 冲突)
# ---------------------------------------------------------------------------


def _load_all_sync(cwd: Path) -> list[TodoTask]:
    """sync 读 todo 列表(等价 TodoService.list 无过滤)。"""
    from cc_harness.project.manifest import load_manifest

    manifest = load_manifest(cwd)
    if manifest is None:
        return []
    storage = TodoStorage(cwd, manifest)
    return storage.load_all()


def _get_sync(cwd: Path, task_id: str) -> TodoTask:
    """sync 取单个 task,TaskNotFound 由 caller 兜底。"""
    tasks = _load_all_sync(cwd)
    for t in tasks:
        if t.id == task_id:
            return t
    raise TaskNotFound(f"task {task_id!r} not found")


# ---------------------------------------------------------------------------
# Pure resume selector(spec line 569-580)
# ---------------------------------------------------------------------------


def select_resume_task(tasks: list[TodoTask]) -> TodoTask | None:
    """从 task 列表选 resume 目标。

    规则:
        - 0 个 in_progress → None
        - 多个 in_progress → updated_at 最大的一个(spec 假设最近动过的最相关)

    Args:
        tasks: TodoService.list() 返回的完整列表。

    Returns:
        选中的 task 或 None。
    """
    in_progress = [t for t in tasks if t.status == "in_progress"]
    if not in_progress:
        return None
    return max(in_progress, key=lambda t: t.updated_at)


# ---------------------------------------------------------------------------
# Task 摘要格式化
# ---------------------------------------------------------------------------


def _format_resume_summary(task: TodoTask) -> str:
    """人类可读的 resume 候选摘要(单 task)。"""
    lines = [
        "[resume candidate]",
        f"  id:        {task.id}",
        f"  title:     {task.title}",
        f"  status:    {task.status}",
        f"  priority:  {task.priority or '(none)'}",
        f"  updated:   {task.updated_at.isoformat()}",
    ]
    if task.acceptance_criteria:
        lines.append("  acceptance:")
        for c in task.acceptance_criteria:
            lines.append(f"    - {c}")
    if task.depends_on:
        lines.append(f"  depends_on: {task.depends_on}")
    if task.active_sessions:
        lines.append(f"  active_sessions: {len(task.active_sessions)} entries")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# cmd_resume — argparse dispatcher
# ---------------------------------------------------------------------------


def cmd_resume(args: Namespace, cwd: Path) -> int:
    """`cc-harness --resume / --resume-id / --no-resume` dispatcher。

    Args:
        args: argparse.Namespace,字段:
            - resume: bool (--resume)
            - resume_id: str | None (--resume-id <id>)
            - no_resume: bool (--no-resume)
        cwd: 项目根目录。

    Returns:
        exit code:0 OK / 1 业务错。
    """
    console = Console()

    # --no-resume 优先
    if args.no_resume:
        print_text(console, "(no resume — skipping)")
        return 0

    # 既无 --resume 也无 --resume-id:等价为 no-resume(spec 友好)
    if not args.resume and not args.resume_id:
        print_text(console, "(no resume flag — skipping)")
        return 0

    try:
        # --resume-id <id> 优先
        if args.resume_id:
            task = _get_sync(cwd, args.resume_id)
            print_text(console, _format_resume_summary(task))
            return 0

        # --resume(无 id)→ 选最新 in_progress
        tasks = _load_all_sync(cwd)
        selected = select_resume_task(tasks)
        if selected is None:
            print_text(
                console,
                "no in_progress task to resume "
                "(run `cc-harness todo create` to start one)",
            )
            return 0
        print_text(
            console,
            f"(auto-selected from {len(tasks)} tasks by updated_at)\n"
            + _format_resume_summary(selected),
        )
        return 0
    except TaskNotFound as e:
        print_error(console, f"{type(e).__name__}: {e}")
        return 1


__all__ = ["cmd_resume", "select_resume_task"]
