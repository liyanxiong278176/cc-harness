"""`cc-harness todo {list,get,create,update,delete,resolve,validate}` 子命令入口。

spec 组件 8 七子命令;`cmd_todo(args, cwd) -> int` 单 dispatcher,按
`args.subcommand` 分派。

设计:
    - 所有写入操作通过 TodoService,绝不直接读写 yaml/md 文件(spec 约束 1)
    - session_id:`cli_session_id()` 一次性生成,append 到 active_sessions
    - 错误:`except TodoError as e` → stderr `[cmd] ✗ Type: msg` + exit 1
    - 退出码:0 成功 / 1 业务错(TodoError)/ 2 系统错(OSError 等)
    - 输出格式:args.json=True → JSON;否则 rich Table (tty) / 纯文本 (pipe)

更新(update)特殊 — `clear_*`:
    CLI 把 `None` 用 `--clear-X` 表示清空(因为 argparse 的 `--X` 没法发 None);
    update 把 `None` → 清空,把非 None → 设置。
"""
from __future__ import annotations

import asyncio
import json
import sys
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console

from cc_harness.cli._shared import (
    JsonOrText,
    cli_session_id,
    load_manifest_or_exit,
    print_error,
    print_text,
)
from cc_harness.project.exceptions import TodoError
from cc_harness.project.service import TodoService

# ---------------------------------------------------------------------------
# Helpers — TodoService factory
# ---------------------------------------------------------------------------


def _make_service(cwd: Path) -> TodoService:
    manifest = load_manifest_or_exit(cwd)
    return TodoService(project_root=cwd, manifest=manifest)


# ---------------------------------------------------------------------------
# Subcommand handlers — 每个返回 0
# ---------------------------------------------------------------------------


async def _list(svc: TodoService, args: Namespace, console: Console) -> int:
    include_done = not bool(args.no_done)
    status = args.status
    parent = getattr(args, "parent", None)

    sort_key = args.sort or "status"

    tasks = await svc.list(status=status, parent_task=parent, include_done=include_done)
    if sort_key == "priority":
        prio = {"high": 0, "medium": 1, "low": 2, "critical": 3}
        tasks.sort(key=lambda t: (
            prio.get(t.priority, 4) if t.priority is not None else 4,
            -t.updated_at.timestamp(),
        ))
    elif sort_key == "created_at":
        tasks.sort(key=lambda t: -t.created_at.timestamp())
    elif sort_key == "updated_at":
        tasks.sort(key=lambda t: -t.updated_at.timestamp())
    else:  # status default
        sp = {"in_progress": 0, "blocked": 1, "pending": 2, "cancelled": 3, "done": 4}
        tasks.sort(key=lambda t: (sp.get(t.status, 99), -t.updated_at.timestamp()))

    limit = int(args.limit or 20)
    truncated = len(tasks) > limit
    shown = tasks[:limit]

    # JSON mode 强制全量 dump(便于 jq 处理),不限 limit
    if args.json:
        out_data = [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "priority": t.priority,
                "depends_on": t.depends_on,
                "parent_task": t.parent_task,
                "assigned_to": t.assigned_to,
                "labels": t.labels,
                "updated_at": t.updated_at.isoformat(),
                "created_at": t.created_at.isoformat(),
            }
            for t in tasks
        ]
        sys.stdout.write(json.dumps(out_data, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
        return 0

    # header 含总量(不受 limit 影响)
    all_tasks = await svc.list(include_done=include_done)
    counts: dict[str, int] = {}
    for t in all_tasks:
        counts[t.status] = counts.get(t.status, 0) + 1
    header = (
        f"[todo_list] {len(all_tasks)} tasks "
        f"({counts.get('done', 0)} done / "
        f"{counts.get('in_progress', 0)} in_progress / "
        f"{counts.get('pending', 0)} pending)"
    )

    sink = JsonOrText(console, args)  # json 已经被上方早退,这里仅 tty/text
    sink.print_text(header + "\n")

    body_lines = []
    for t in shown:
        icon = {
            "done": "✓", "in_progress": "⠋", "pending": "○",
            "blocked": "!", "cancelled": "✗",
        }.get(t.status, "?")
        prio_str = f"[{t.priority}] " if t.priority else ""
        body_lines.append(
            f"{icon} {t.id}  {t.title}  {prio_str}[{t.status}]"
        )
    suffix = (
        f"\n(+{len(tasks) - limit} more, narrow with --status=X or --limit=N)"
        if truncated else ""
    )
    sink.print_text("\n".join(body_lines) + suffix + "\n")
    return 0


async def _get(svc: TodoService, args: Namespace, console: Console) -> int:
    task_id = args.task_id
    if not task_id:
        print_error(console, "task_id is required")
        return 1
    try:
        task = await svc.get(task_id)
    except TodoError as e:
        print_error(console, f"{type(e).__name__}: {e}")
        return 1

    if getattr(args, "description_only", False):
        # 只输出 description 体,无装饰
        sys.stdout.write((task.description or "") + "\n")
        sys.stdout.flush()
        return 0

    payload = {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
        "depends_on": task.depends_on,
        "parent_task": task.parent_task,
        "assigned_to": task.assigned_to,
        "labels": task.labels,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "effort_estimate": task.effort_estimate,
        "acceptance_criteria": task.acceptance_criteria,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
        "active_sessions": task.active_sessions,
        "description": task.description,
    }
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
        return 0
    # 人类可读
    print_text(console, f"[todo_get] {task.id}")
    print_text(console, f"title:    {task.title}")
    print_text(console, f"status:   {task.status}")
    print_text(console, f"priority: {task.priority or '(none)'}")
    print_text(console, f"depends_on:    {task.depends_on or '(none)'}")
    print_text(console, f"parent_task:   {task.parent_task or '(none)'}")
    print_text(console, f"assigned_to:   {task.assigned_to or '(none)'}")
    print_text(console, f"labels:        {task.labels or '(none)'}")
    print_text(console,
        f"due_date:      {task.due_date.isoformat() if task.due_date else '(none)'}"
    )
    print_text(console,
        f"effort_estimate: {task.effort_estimate if task.effort_estimate is not None else '(none)'}"
    )
    print_text(console,
        f"acceptance_criteria: {task.acceptance_criteria or '(none)'}"
    )
    print_text(console, f"created_at:    {task.created_at.isoformat()}")
    print_text(console, f"updated_at:    {task.updated_at.isoformat()}")
    print_text(console, f"active_sessions: {task.active_sessions}")
    print_text(console, f"description:   {task.description[:200]}{'...' if len(task.description) > 200 else ''}")
    return 0


async def _create(svc: TodoService, args: Namespace, console: Console) -> int:
    title = args.title
    if not title:
        print_error(console, "InvalidFieldError: title is required")
        return 1

    # 解析 depends_on / labels(可重复或逗号分隔)— args 已经是 list 或者 None
    deps = args.depends_on or []
    labels = args.label or []
    ac = args.acceptance_criteria or []

    due_date: datetime | None = None
    if args.due_date:
        try:
            s = str(args.due_date)
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            due_date = datetime.fromisoformat(s)
        except (ValueError, TypeError):
            print_error(console, f"InvalidFieldError: due_date {args.due_date!r} not ISO 8601")
            return 1

    effort = args.effort_estimate
    if effort is not None:
        try:
            effort = float(effort)
        except (TypeError, ValueError):
            print_error(console, f"InvalidFieldError: effort_estimate {effort!r} not a number")
            return 1

    try:
        task = await svc.create(
            title=title,
            description=args.description or "",
            depends_on=deps,
            parent_task=args.parent or None,
            assigned_to=args.assigned_to or None,
            priority=args.priority or None,
            labels=labels,
            due_date=due_date,
            effort_estimate=effort,
            acceptance_criteria=ac,
            session_id=cli_session_id(),
        )
    except TodoError as e:
        print_error(console, f"{type(e).__name__}: {e}")
        return 1

    payload = {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
    }
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 0
    print_text(console, f"[todo_create] ✓ created task {task.id}")
    print_text(console, f"title:    {task.title}")
    print_text(console, f"status:   {task.status}")
    if task.priority:
        print_text(console, f"priority: {task.priority}")
    print_text(console, f"id:       {task.id}")
    return 0


async def _update(svc: TodoService, args: Namespace, console: Console) -> int:
    task_id = args.task_id
    if not task_id:
        print_error(console, "InvalidFieldError: task_id is required")
        return 1

    fields: dict[str, Any] = {}

    # 标量字段 — 直接复制
    for k in ("title", "description", "status", "parent", "assigned_to",
              "priority", "label", "depends_on", "acceptance_criteria"):
        v = getattr(args, k, None)
        if v is not None:
            if k == "parent":
                fields["parent_task"] = v
            elif k == "label":
                fields["labels"] = v
            elif k == "depends_on":
                fields["depends_on"] = v
            elif k == "acceptance_criteria":
                fields["acceptance_criteria"] = v
            else:
                fields[k] = v

    # clear_* 显式 None 化(可空字段)
    clearable_simple = ("parent_task", "assigned_to", "priority", "due_date",
                        "effort_estimate")
    for k in clearable_simple:
        if getattr(args, f"clear_{k}", False):
            fields[k] = None

    # due_date 解析
    if "due_date" in fields and fields["due_date"] is not None:
        try:
            s = str(fields["due_date"])
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            fields["due_date"] = datetime.fromisoformat(s)
        except (ValueError, TypeError):
            print_error(console, f"InvalidFieldError: due_date {fields['due_date']!r} not ISO 8601")
            return 1

    if "effort_estimate" in fields and fields["effort_estimate"] is not None:
        try:
            fields["effort_estimate"] = float(fields["effort_estimate"])
        except (TypeError, ValueError):
            print_error(console, f"InvalidFieldError: effort_estimate not a number")
            return 1

    if not fields:
        print_error(console, "no fields provided to update")
        return 1

    try:
        updated = await svc.update(task_id, session_id=cli_session_id(), **fields)
    except TodoError as e:
        print_error(console, f"{type(e).__name__}: {e}")
        return 1

    payload = {"id": updated.id, "status": updated.status}
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 0
    print_text(console, f"[todo_update] ✓ updated task {updated.id}")
    print_text(console, f"status:   {updated.status}")
    print_text(console, f"changed:  {', '.join(fields.keys())}")
    return 0


async def _delete(svc: TodoService, args: Namespace, console: Console) -> int:
    task_id = args.task_id
    if not task_id:
        print_error(console, "InvalidFieldError: task_id is required")
        return 1
    force = bool(getattr(args, "force", False))
    try:
        await svc.delete(task_id, force=force)
    except TodoError as e:
        print_error(console, f"{type(e).__name__}: {e}")
        return 1
    suffix = " (force=True, dangling references left)" if force else ""
    print_text(console, f"[todo_delete] ✓ deleted task {task_id}{suffix}")
    return 0


async def _resolve(svc: TodoService, args: Namespace, console: Console) -> int:
    task_id = args.task_id
    if not task_id:
        print_error(console, "InvalidFieldError: task_id is required")
        return 1
    include_done = not bool(args.no_done)
    try:
        chain = await svc.resolve(task_id, include_done=include_done)
    except TodoError as e:
        print_error(console, f"{type(e).__name__}: {e}")
        return 1

    if args.json:
        out = [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "depth": i,
                "marker": "target" if i == 0 else f"depth {i}",
            }
            for i, t in enumerate(chain)
        ]
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 0

    lines = [f"[todo_resolve] {task_id} chain ({len(chain)} tasks)"]
    icon = {"done": "✓", "in_progress": "⠋", "pending": "○", "blocked": "!", "cancelled": "✗"}
    for i, t in enumerate(chain):
        m = icon.get(t.status, "?")
        prio = f"[{t.priority}]" if t.priority else ""
        marker = "← target" if i == 0 else f"← depth {i}"
        lines.append(f"{m} {t.id}  {t.title}  {prio}  [{t.status}]  {marker}")
    upstream = chain[1:]
    if not upstream:
        ready = "Ready to work: no upstream."
    elif all(t.status == "done" for t in upstream):
        ready = "Ready to work: all upstream done."
    else:
        remaining = [t.id for t in upstream if t.status != "done"]
        ready = f"Not ready: pending upstream = {remaining}"
    print_text(console, "\n".join(lines) + "\n\n" + ready + "\n")
    return 0


async def _validate(svc: TodoService, args: Namespace, console: Console) -> int:
    try:
        issues = await svc.validate()
    except TodoError as e:
        print_error(console, f"{type(e).__name__}: {e}")
        return 1

    if args.strict:
        # 把 warning 提升为 error(语义对齐 handler 的 strict=True)
        issues = [
            type(i)(
                task_id=i.task_id,
                severity="error",  # type: ignore[arg-type]
                rule_id=i.rule_id,
                message=i.message,
            )
            for i in issues
        ]

    if args.json:
        out = [
            {
                "task_id": i.task_id,
                "severity": i.severity,
                "rule_id": i.rule_id,
                "message": i.message,
            }
            for i in issues
        ]
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        # 业务错(任意 error) → exit 1
        return 0 if not any(i.severity == "error" for i in issues) else 1

    if not issues:
        print_text(console, "[todo_validate] ✓ all clean (0 issues)")
        return 0

    error_n = sum(1 for i in issues if i.severity == "error")
    warn_n = sum(1 for i in issues if i.severity == "warning")
    head = f"[todo_validate] ✗ {len(issues)} issues found ({error_n} error / {warn_n} warning)"
    body_lines = [
        f"  - [{i.severity}] {i.task_id or '(global)'} ({i.rule_id}): {i.message}"
        for i in issues
    ]
    print_error(console, head + "\n" + "\n".join(body_lines))
    if any(i.severity == "error" for i in issues):
        return 1
    return 0


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


_HANDLERS = {
    "list": _list,
    "get": _get,
    "create": _create,
    "update": _update,
    "delete": _delete,
    "resolve": _resolve,
    "validate": _validate,
}


def cmd_todo(args: Namespace, cwd: Path) -> int:
    """`cc-harness todo <subcommand>` 入口。

    单 dispatcher:读 `args.subcommand` → 分派给对应 async handler。
    不认识的 subcommand → exit 1 + stderr 提示。
    """
    subcommand = getattr(args, "subcommand", None)
    if not subcommand:
        print_error(Console(), "no subcommand provided (use `cc-harness todo --help`)")
        return 1

    handler = _HANDLERS.get(subcommand)
    if handler is None:
        print_error(Console(), f"unknown todo subcommand {subcommand!r}; "
                                f"choose one of {sorted(_HANDLERS)}")
        return 1

    try:
        svc = _make_service(cwd)
        console = Console()
        # 强制一次性 session_id 用于本组写操作 — 即使只 1 个 cmd
        rc = asyncio.run(handler(svc, args, console))
    except SystemExit as e:
        return int(e.code) if e.code is not None else 1
    except TodoError as e:
        print_error(Console(), f"[{subcommand}] ✗ {type(e).__name__}: {e}")
        return 1
    except OSError as e:
        print_error(Console(), f"[{subcommand}] system error: {e}")
        return 2
    return rc


__all__ = ["cmd_todo"]
