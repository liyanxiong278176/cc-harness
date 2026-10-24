#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""cc-harness entry point."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

# Force UTF-8 for stdio on Windows (default codepage is GBK/cp936 on zh-CN
# systems, which breaks the prompt char and any non-ASCII LLM output).
if sys.platform == "win32":
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass  # Python < 3.7 or stream not reconfigurable; user can set PYTHONUTF8=1

PROJECT_ROOT = Path(__file__).parent


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser without parsing process arguments."""
    p = argparse.ArgumentParser(description="cc-harness: terminal coding agent with MCP tools", add_help=False)
    # REPL 默认参数(无 sub-command 时生效)
    p.add_argument("-h", "--help", action="help", help="show this help message and exit")
    p.add_argument(
        "--mode", choices=("coding", "plan", "design", "chat"),
        default="coding",
        help="Initial sticky mode (switchable at runtime via /plan /design /coding /chat)",
    )
    p.add_argument(
        "--design-dir", type=Path, default=None,
        help="Where design-mode outputs are saved (default: ~/.cc-harness/designs/)",
    )
    p.add_argument("--emit-events", default=None, metavar="PATH",
                   help="Emit each ReAct event (thought/action/observation/result) to a JSONL file for eval trajectory analysis")

    # Sub-commands(spec 组件 8)
    sub = p.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="Initialize .cc-harness/project.yaml")
    p_init.add_argument("--name", type=str, default=None,
                        help="Project name (default: dir name)")
    p_init.add_argument("--no-prompt", action="store_true",
                        help="Non-interactive (requires --name or uses dir name)")
    p_init.add_argument("--resume-mode", choices=("ask", "auto", "manual"), default=None,
                        help="Resume mode (default: ask)")
    p_init.add_argument("--no-live", action="store_true",
                        help="Disable Live panel (live.position='off')")
    p_init.add_argument("--force-reinit", action="store_true",
                        help="Overwrite existing manifest")

    # todo <subcommand>
    p_todo = sub.add_parser("todo", help="Manage todos")
    p_todo.add_argument(
        "subcommand", choices=("list", "get", "create", "update", "delete", "resolve", "validate"),
        help="todo sub-command",
    )
    # list / common
    p_todo.add_argument("--status", choices=("pending", "in_progress", "done", "blocked", "cancelled"))
    p_todo.add_argument("--parent", type=str, default=None)
    p_todo.add_argument("--no-done", action="store_true")
    p_todo.add_argument("--json", action="store_true")
    p_todo.add_argument("--format", choices=("table", "csv"), default=None)
    p_todo.add_argument("--sort", choices=("status", "priority", "created_at", "updated_at"), default=None)
    p_todo.add_argument("--limit", type=int, default=None)
    # get
    p_todo.add_argument("task_id", nargs="?", default=None)
    p_todo.add_argument("--raw", action="store_true")
    # create
    p_todo.add_argument("--title", type=str, default=None)
    p_todo.add_argument("--description", type=str, default="")
    p_todo.add_argument("--depends-on", dest="depends_on", action="append", default=None)
    p_todo.add_argument("--assigned-to", dest="assigned_to", type=str, default=None)
    p_todo.add_argument("--priority", choices=("low", "medium", "high", "critical"), default=None)
    p_todo.add_argument("--label", action="append", default=None)
    p_todo.add_argument("--due-date", dest="due_date", type=str, default=None)
    p_todo.add_argument("--effort-estimate", dest="effort_estimate", type=str, default=None)
    p_todo.add_argument("--acceptance-criteria", dest="acceptance_criteria",
                        action="append", default=None)
    # update
    p_todo.add_argument("--append-acceptance-criteria",
                        dest="append_acceptance_criteria", action="append", default=None)
    p_todo.add_argument("--clear-parent-task", dest="clear_parent_task", action="store_true")
    p_todo.add_argument("--clear-assigned-to", dest="clear_assigned_to", action="store_true")
    p_todo.add_argument("--clear-priority", dest="clear_priority", action="store_true")
    p_todo.add_argument("--clear-due-date", dest="clear_due_date", action="store_true")
    p_todo.add_argument("--clear-effort-estimate", dest="clear_effort_estimate", action="store_true")
    # delete
    p_todo.add_argument("--force", action="store_true")
    # validate
    p_todo.add_argument("--strict", action="store_true")

    # resume (legacy form: --resume / --resume-id / --no-resume 是 REPL 入口的 flag)
    # 作为 sub-command 时只支持 resume-id / no-resume(spec 表 line 509)
    p_resume = sub.add_parser("resume", help="Resume in-progress task")
    p_resume.add_argument("--resume-id", dest="resume_id", type=str, default=None)
    p_resume.add_argument("--no-resume", dest="no_resume", action="store_true")

    # backward-compat: `cc-harness --resume` (legacy REPL flag 形式)
    p.add_argument("--resume", action="store_true",
                   help="[deprecated] Resume most recent in_progress task (use `resume` sub-command)")
    p.add_argument("--resume-id", dest="resume_id_legacy", type=str, default=None,
                   help="[deprecated] Resume specific task by id (use `resume` sub-command)")
    p.add_argument("--no-resume", dest="no_resume_legacy", action="store_true",
                   help="[deprecated] Skip resume (use `resume` sub-command)")

    # --- TUI default + --repl 调试入口(spec §6.2 / plan §6.2) ---
    # 默认走 Textual TUI;--repl 切回 legacy REPL
    # (--mode 已在上方定义,choices 含 coding/plan/design/chat)
    p.add_argument("--repl", action="store_true",
                   help="Use legacy REPL (default: Textual TUI)")
    p.add_argument("--cwd", default=".",
                   help="Working directory (default: current dir)")

    return p


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    return _build_arg_parser().parse_args()


def main() -> None:
    """Compatibility shim; the only execution path is the Durable Runtime."""
    from cc_harness.entrypoint import main as package_main

    package_main()
    return

# The legacy REPL/TUI assembly that used to live below the delegation is gone:
# it was unreachable (``main()`` returns first) and kept 250 lines of dead
# startup logic alive.  The single execution path is the Durable Runtime in
# ``cc_harness.entrypoint``; ``cc_harness.agent.run_turn`` stays importable
# only for the legacy test corpus and data-migration helpers.

if __name__ == "__main__":
    # The installed command and source script share the same package
    # entrypoint. ``main()`` above remains importable for legacy integration
    # tests and third-party callers during the migration window.
    from cc_harness.entrypoint import main as _package_main
    _package_main()
