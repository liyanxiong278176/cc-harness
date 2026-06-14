"""CLI orchestrator for the GAIA context-management eval."""
from __future__ import annotations
import argparse
import asyncio as _asyncio
import json as _json
import subprocess
import sys as _sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from cc_harness.config import ContextConfig
from eval.metrics.collector import build_per_task_diffs, compare_sessions
from eval.metrics.schema import SessionMetrics, TaskMetrics
from eval.reports.csv_report import write_csv_report
from eval.reports.markdown import render_comparison_report
from eval.runners.session_runner import import_from_worktree, run_session


@dataclass
class Args:
    level: str
    limit: int
    seed: int
    include_attachments: bool
    branches: list[str]
    worktree_dir: Path
    keep_worktrees: bool
    mcp_config: Path
    context_window: int | None
    tier_overrides: str | None
    max_iter: int
    parallel: bool
    on_error: str
    checkpoint_every: int
    abort_after_overflows: int
    dry_run: bool
    output_dir: Path | None
    report_format: list[str]
    no_report: bool


def _csv(v: str) -> list[str]:
    return [s.strip() for s in v.split(",") if s.strip()]


def parse_args(argv: list[str] | None = None) -> Args:
    p = argparse.ArgumentParser(prog="eval.run",
                                description="GAIA context-management A/B eval")
    p.add_argument("--level", default="1", choices=["1", "2", "3", "all"])
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--include-attachments",
                   type=lambda v: v.lower() != "false", default=True)
    p.add_argument("--branches", type=_csv, default=["master", "context-compaction"])
    p.add_argument("--worktree-dir", type=Path, default=Path(".eval-worktrees"))
    p.add_argument("--keep-worktrees", action="store_true")
    p.add_argument("--mcp-config", type=Path, default=Path("mcp.json"))
    p.add_argument("--context-window", type=int, default=None)
    p.add_argument("--tier-overrides", default=None,
                   help='e.g. "1=0.05,2=0.10,3=0.15"')
    p.add_argument("--max-iter", type=int, default=20)
    p.add_argument("--parallel", action="store_true")
    p.add_argument("--on-error", default="continue", choices=["continue", "abort"])
    p.add_argument("--checkpoint-every", type=int, default=5)
    p.add_argument("--abort-after-overflows", type=int, default=3)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--report-format", type=_csv, default=["markdown", "csv", "json"])
    p.add_argument("--no-report", action="store_true")
    ns = p.parse_args(argv)
    if ns.limit < 30:
        p.error("--limit must be >= 30 for meaningful report")
    return Args(**vars(ns))


# --- 6.2: preflight helpers + main preflight ---

def _git_status_clean() -> bool:
    try:
        r = subprocess.run(["git", "status", "--porcelain"],
                           capture_output=True, text=True, check=True)
        return r.stdout.strip() == ""
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _branch_exists(name: str) -> bool:
    try:
        r = subprocess.run(["git", "rev-parse", "--verify", name],
                           capture_output=True, text=True)
        return r.returncode == 0
    except FileNotFoundError:
        return False


def _hf_logged_in() -> bool:
    import os
    if os.getenv("HF_TOKEN"):
        return True
    try:
        from huggingface_hub import HfApi
        HfApi().whoami()
        return True
    except Exception:
        return False


def preflight(args: "Args") -> list[str]:
    issues: list[str] = []
    if not _git_status_clean():
        issues.append("uncommitted changes — please commit/stash before running")
    for b in args.branches:
        if not _branch_exists(b):
            issues.append(f"branch not found: {b!r}")
    if not _hf_logged_in():
        issues.append("HuggingFace not logged in (HF_TOKEN env or `huggingface-cli login`)")
    if not args.mcp_config.exists():
        issues.append(f"mcp config not found: {args.mcp_config}")
    return issues


# --- 6.3: worktree helpers ---

def worktree_add(path: Path, branch: str) -> None:
    subprocess.run(["git", "worktree", "add", str(path), branch],
                   check=True, capture_output=True)


def worktree_remove(path: Path, *, force: bool = True) -> None:
    args = ["git", "worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    try:
        subprocess.run(args, check=True, capture_output=True)
    except subprocess.CalledProcessError:
        pass


# --- 6.4: main() + dry-run ---

# Imported at module level so test monkeypatches can rebind them on this module.
from eval.datasets.gaia_loader import (  # noqa: E402  (after dataclass/Args)
    filter_tasks as _filter_tasks,
    load_gaia_validation as _load_gaia_validation,
    stratified_sample as _stratified_sample,
)


async def main(args: "Args | None" = None) -> int:
    if args is None:
        args = parse_args(_sys.argv[1:])
    print("[preflight] checking…")
    issues = preflight(args)
    for i in issues:
        print(f"  - {i}")
    if issues:
        print(f"[preflight] FAIL — {len(issues)} issue(s)")
        return 2

    print("[dataset] loading GAIA validation…")
    all_tasks = _load_gaia_validation()
    if args.level != "all":
        all_tasks = [t for t in all_tasks if t.level == int(args.level)]
    runnable, skipped = _filter_tasks(all_tasks, include_attachments=args.include_attachments)
    sample = _stratified_sample(runnable, limit=args.limit, seed=args.seed)
    print(f"[dataset] {len(all_tasks)} total → {len(runnable)} runnable → "
          f"{len(sample)} sampled (skipped {len(skipped)} for tool-gap)")

    if args.dry_run:
        print("[Dry run] would execute:")
        for b in args.branches:
            print(f"  branch {b}: {len(sample)} tasks")
        for t in sample[:10]:
            print(f"    - {t.task_id[:8]} L{t.level}: {t.question[:60]}…")
        if len(sample) > 10:
            print(f"    … and {len(sample) - 10} more")
        return 0

    # --- 6.5: full run path ---

    # Resolve output dir
    if args.output_dir is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        args.output_dir = Path("eval/runs") / f"{date}-L{args.level}-{args.limit}q-{head}"
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Build ContextConfig
    ctx_kwargs: dict = {}
    if args.context_window:
        ctx_kwargs["context_window"] = args.context_window
    if args.tier_overrides:
        for pair in args.tier_overrides.split(","):
            k, v = pair.split("=")
            ctx_kwargs[f"tier{k.strip()}_threshold"] = float(v.strip())
    ctx_config = ContextConfig(**ctx_kwargs) if ctx_kwargs else ContextConfig()

    # Setup worktrees
    args.worktree_dir.mkdir(parents=True, exist_ok=True)
    branch_to_wt: dict[str, Path] = {}
    for branch in args.branches:
        wt = args.worktree_dir / branch
        if not wt.exists():
            print(f"[worktree] adding {branch} → {wt}")
            worktree_add(wt, branch)
        branch_to_wt[branch] = wt

    # Run each branch SERIALLY (parallel disabled in v1)
    branch_sessions: dict[str, SessionMetrics] = {}
    branch_task_metrics: dict[str, list] = {}
    # .env lives at the REPO root, not inside the worktree (worktrees don't
    # copy untracked files; .env is gitignored). Use the main repo's .env
    # so credentials are available to LLMClient / MCPClient in the worktree.
    repo_env = Path(".env").resolve()
    for branch, wt in branch_to_wt.items():
        print(f"[branch] {branch} → {wt}")
        out_dir = args.output_dir / branch
        out_dir.mkdir(parents=True, exist_ok=True)
        commit = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                                capture_output=True, text=True).stdout.strip()[:7]

        with import_from_worktree(wt):
            from cc_harness.config import load_config as _load_cfg
            from cc_harness.llm import LLMClient as _LLM
            from cc_harness.mcp_client import MCPClient as _MCP
            cfg = _load_cfg(env_path=repo_env, mcp_json_path=args.mcp_config)
            llm = _LLM(api_key=cfg.openai_api_key, base_url=cfg.openai_base_url,
                       model=cfg.openai_model)
            mcp = _MCP(cfg.mcp_servers)
            await mcp.start()
            try:
                sm = await run_session(
                    tasks=sample, llm=llm, mcp=mcp,
                    branch=branch, out_dir=out_dir,
                    context_config=ctx_config, max_iter=args.max_iter,
                    checkpoint_every=args.checkpoint_every,
                    abort_after_overflows=args.abort_after_overflows,
                    git_commit=commit, cwd=str(wt),
                )
            finally:
                await mcp.shutdown()
        branch_sessions[branch] = sm

        # Re-read trace to collect TaskMetrics list for per_task_diffs
        tms: list = []
        with (out_dir / "trace.jsonl").open(encoding="utf-8") as f:
            for line in f:
                d = _json.loads(line)
                d.pop("per_iter_snapshots", None)
                tms.append(TaskMetrics(**d, per_iter_snapshots=[]))
        branch_task_metrics[branch] = tms

    # Compare (requires both branches; otherwise just print per-branch summary)
    if "master" in branch_sessions and "context-compaction" in branch_sessions:
        cmp = compare_sessions(
            branch_sessions["master"], branch_sessions["context-compaction"],
        )
        cmp.per_task_diffs = build_per_task_diffs(
            branch_task_metrics["master"],
            branch_task_metrics["context-compaction"],
        )
        (args.output_dir / "comparison.json").write_text(
            _json.dumps(asdict(cmp), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if not args.no_report and "markdown" in args.report_format:
            (args.output_dir / "report.md").write_text(
                render_comparison_report(cmp), encoding="utf-8",
            )
        if not args.no_report and "csv" in args.report_format:
            write_csv_report(cmp, args.output_dir / "report.csv")
        print(f"[report] written to {args.output_dir}")
    else:
        print(
            f"[report] skipped comparison — needs both 'master' and "
            f"'context-compaction' branches; got {list(branch_sessions)}"
        )
        for branch, sm in branch_sessions.items():
            print(f"  - {branch}: {sm.tasks_correct}/{sm.tasks_total} correct, "
                  f"{sm.overflow_count} overflows, {sm.api_total_tokens_sum:,} API tokens")

    # Cleanup
    if not args.keep_worktrees:
        for branch, wt in branch_to_wt.items():
            worktree_remove(wt)
    return 0


if __name__ == "__main__":
    _sys.exit(_asyncio.run(main()))
