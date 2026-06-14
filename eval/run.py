"""CLI orchestrator for the GAIA context-management eval."""
from __future__ import annotations
import argparse
import asyncio as _asyncio
import subprocess
import sys as _sys
from dataclasses import dataclass
from pathlib import Path


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
from eval.datasets.gaia_loader import (
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

    # (Full run path is added in Task 6.5; for now it's a stub that errors
    # clearly so users get a helpful message rather than a silent dry-run run.)
    print("[full run] not yet implemented in this build — see Task 6.5")
    return 3


if __name__ == "__main__":
    _sys.exit(_asyncio.run(main()))
