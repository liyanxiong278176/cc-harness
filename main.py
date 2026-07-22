#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""cc-harness entry point."""
from __future__ import annotations
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from rich.console import Console
from cc_harness.config import load_config, ConfigError, load_executor_config, load_context_config
from cc_harness.llm import LLMClient
from cc_harness.mcp_client import MCPClient
from cc_harness.repl import run_repl

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


def _parse_args() -> argparse.Namespace:
    """Argparse:支持 sub-commands(init / todo / resume) + 默认 REPL 入口。

    向后兼容守卫:
        - 无参数 → REPL(原有行为)
        - 仅 --mode / --design-dir → REPL(原有行为)
        - `init` / `todo` / `resume` 子命令 → CLI 分派
    """
    p = argparse.ArgumentParser(description="cc-harness: terminal coding agent with MCP tools")
    # REPL 默认参数(无 sub-command 时生效)
    p.add_argument(
        "--mode", choices=("coding", "plan", "design", "chat"),
        default="coding",
        help="Initial sticky mode (switchable at runtime via /plan /design /coding /chat)",
    )
    p.add_argument(
        "--design-dir", type=Path, default=None,
        help="Where design-mode outputs are saved (default: ~/.cc-harness/designs/)",
    )

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

    return p.parse_args()


def main() -> None:
    args = _parse_args()
    console = Console()
    working_dir = Path.cwd()

    # --- Task 6 / spec 组件 8:CLI sub-command 分派 ---
    if args.command == "init":
        from cc_harness.cli.init import cmd_init
        sys.exit(cmd_init(args, working_dir))
    if args.command == "todo":
        from cc_harness.cli.todo import cmd_todo
        sys.exit(cmd_todo(args, working_dir))
    if args.command == "resume":
        from cc_harness.cli.resume import cmd_resume
        # Bare `resume` sub-command is the explicit opt-in equivalent of legacy --resume.
        args.resume = True
        sys.exit(cmd_resume(args, working_dir))

    # legacy `--resume` / `--resume-id` / `--no-resume`(无 sub-command)→ 走 CLI
    if getattr(args, "resume", False) or getattr(args, "resume_id_legacy", None) or getattr(args, "no_resume_legacy", False):
        from cc_harness.cli.resume import cmd_resume
        # 字段名对齐
        if not hasattr(args, "resume_id") or args.resume_id is None:
            args.resume_id = getattr(args, "resume_id_legacy", None)
        if not hasattr(args, "no_resume") or not args.no_resume:
            args.no_resume = getattr(args, "no_resume_legacy", False)
        sys.exit(cmd_resume(args, working_dir))

    boot_start = time.monotonic()
    try:
        cfg = load_config(
            env_path=PROJECT_ROOT / ".env",
            mcp_json_path=PROJECT_ROOT / "mcp.json",
        )
    except ConfigError as e:
        console.print(f"[red]config error: {e}[/red]")
        raise SystemExit(1)

    llm = LLMClient(
        api_key=cfg.openai_api_key,
        model=cfg.openai_model,
        base_url=cfg.openai_base_url,
    )

    async def boot():
        mcp = MCPClient(cfg.mcp_servers)
        try:
            await mcp.start()
            # Report the real startup time (config load + parallel MCP boot).
            # This is the source of truth for "how long did boot take" — no
            # test threshold, just the measured number on every launch.
            console.print(
                f"[dim]startup: {time.monotonic() - boot_start:.2f}s[/dim]"
            )

            # E4 I-1: 提前构造 memory deps + scheduler — 让 4 件 background op
            # (staleness / TTL / consolidation / conflict) + RecallWeighter 在
            # REPL 实际跑时生效。`repl.py` 仍然接收 mem_deps 注入 run_turn,
            # scheduler 注入 _after_turn_memory(在每轮末 maybe_run)。
            from dotenv import dotenv_values as _dotenv
            _mem_env = {**os.environ, **{k: v for k, v in _dotenv(PROJECT_ROOT / ".env").items() if v}}
            from cc_harness.memory.extras import build_memory_extras as _bme
            from cc_harness.memory.config import load_memory_config as _lmc
            from cc_harness.memory.maintenance.scheduler import MaintenanceScheduler as _MSS
            _memory_extras, _mem_deps = await _bme(
                _mem_env, PROJECT_ROOT / "logs" / "memory.db",
            )
            _mem_cfg = _lmc(PROJECT_ROOT / "policy.yaml")
            _scheduler = (
                _MSS(
                    store=_mem_deps["store"],
                    service=_mem_deps["service"],
                    llm=llm,
                    every_n_turns=_mem_cfg.maintenance_every_n_turns,
                    count_threshold=_mem_cfg.maintenance_count_threshold,
                    interval_s=_mem_cfg.maintenance_interval_s,
                    enabled=_mem_cfg.maintenance_enabled,
                )
                if _mem_deps is not None
                else None
            )
            if _scheduler is not None:
                # consolidation / conflict 需要 embedder;extras.py 不暴露到 deps dict,
                # 从 service.embedder 取出后置注入。
                _svc = _mem_deps.get("service")
                if _svc is not None and getattr(_svc, "embedder", None) is not None:
                    _scheduler._embedder = _svc.embedder
                # staleness LLM recheck + D5/D7 配置
                _scheduler._half_life_days = _mem_cfg.staleness_half_life_days
                _scheduler._llm_recheck_enabled = _mem_cfg.staleness_llm_recheck_enabled
                _scheduler._ttl_threshold = _mem_cfg.ttl_staleness_threshold
                _scheduler._ttl_limit = _mem_cfg.ttl_limit
                _scheduler._consol_threshold = _mem_cfg.consolidation_similarity_threshold
                _scheduler._consol_max = _mem_cfg.consolidation_max_cluster_size

            # E2 T2.3: 构造 ReflectionEngine(同 E4 I-1 wiring 模式)。
            # - memory_service 复用 _mem_deps['service'](同 scheduler)。
            # - llm_client 复用主 llm(同 scheduler / E4 I-1)。
            # - judge_llm 走 JUDGE_* env;未配 → None(engine 内部 _ask_judge_with_fallback
            #   走 local 兜底,沿用 E4 fail-soft 哲学)。
            # - l5_engine 单独构(防与 repl 内自带 l5 复用,boot 走自己的 policy.yaml)。
            # - project_root 走 working_dir(与 repl cwd 一致)。
            from cc_harness.reflection.engine import ReflectionEngine as _RE
            from cc_harness.l5 import build_l5_engine as _b5e
            from cc_harness.config import load_l5_config as _l5c
            _judge_base = os.getenv("JUDGE_BASE_URL")
            _judge_key = os.getenv("JUDGE_API_KEY")
            _judge_model = os.getenv("JUDGE_MODEL")
            _judge_llm = (
                LLMClient(
                    api_key=_judge_key,
                    model=_judge_model,
                    base_url=_judge_base,
                )
                if (_judge_base and _judge_key and _judge_model) else None
            )
            _l5_engine = _b5e(_l5c(PROJECT_ROOT / "policy.yaml"))
            _reflection_engine = (
                _RE(
                    memory_service=_mem_deps["service"],
                    llm_client=llm,
                    judge_llm=_judge_llm,
                    l5_engine=_l5_engine,
                    project_root=working_dir,
                    enabled=_mem_cfg.reflection_enabled,
                    every_n_turns=_mem_cfg.reflection_every_n_turns,
                    max_pending=_mem_cfg.reflection_max_pending,
                    drain_timeout_s=_mem_cfg.reflection_drain_timeout_s,
                )
                if _mem_deps is not None
                else None
            )

            # E5 漂移检测(沿 E4 I-1 / E2 T2.3 wiring 模式)
            # - judge_llm 复用 E2 JUDGE client(未配 → None,detector 内部 _ask_judge fallback)
            # - l5_engine 复用 E2 L5(沿 E2 _b5e)
            # - project_root 走 working_dir
            # - every_n_turns / enabled 走 MemoryConfig 字段(T1.1 已加)
            from cc_harness.drift.detector import DriftDetector
            _drift_detector = (
                DriftDetector(
                    reflection_engine=_reflection_engine,
                    judge_llm=_judge_llm,
                    local_llm=llm,  # F3: 主 LLM 作为本地 fallback
                    l5_engine=_l5_engine,
                    project_root=working_dir,
                    audit_path=working_dir / "logs" / "drift.jsonl",
                    every_n_turns=_mem_cfg.drift_every_n_turns,
                    enabled=_mem_cfg.drift_enabled,
                )
                if _mem_deps is not None and _reflection_engine is not None
                else None
            )

            # 注入到 memory service / retriever(沿 E4 scheduler 模式:直接 setattr)
            if _mem_deps is not None and _drift_detector is not None:
                _mem_deps["service"].drift_detector = _drift_detector
                if "retriever" in _mem_deps and _mem_deps["retriever"] is not None:
                    _mem_deps["retriever"].drift_detector = _drift_detector

            # Pre-warm sandbox server when backend=sandbox.
            # Why: ensure_server() currently only fires on the first command,
            # which (a) hides config errors until something breaks, and
            # (b) adds ~3s cold-start to the first sandboxed run. Pre-warming
            # at boot surfaces failures immediately and removes the cold-start
            # cliff. No-op when backend=native (default).
            exec_cfg = load_executor_config(PROJECT_ROOT / "policy.yaml")
            if str(exec_cfg.backend.value) == "sandbox":
                from cc_harness.sandbox_server import ensure_server
                sb = exec_cfg.sandbox
                console.print(
                    f"[dim]sandbox pre-warm: {sb.server_host}:{sb.server_port}[/dim]"
                )
                state = await ensure_server(
                    sb.server_port, sb.server_host,
                    ready_timeout=sb.timeout_s,
                    allowed_host_paths=[str(PROJECT_ROOT)],
                )
                if state is None:
                    console.print(
                        "[red]sandbox server 起不来 → sandbox 模式不可用"
                        "(Docker 未起 / port 冲突 / 镜像缺失)[/red]"
                    )
                else:
                    console.print(
                        f"[dim]sandbox server up (owned={state.owned})[/dim]"
                    )

            await run_repl(
                llm, mcp,
                cwd=str(working_dir),
                default_mode=args.mode,
                design_dir=args.design_dir,
                context_config=load_context_config(),
                memory_extras=_memory_extras,
                mem_deps=_mem_deps,
                scheduler=_scheduler,
                reflection_engine=_reflection_engine,           # E2 T2.3
                drift_detector=_drift_detector,                  # E5 漂移检测
            )
        finally:
            await mcp.shutdown()

    asyncio.run(boot())


if __name__ == "__main__":
    main()
