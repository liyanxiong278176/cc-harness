"""Unified resumable context-memory benchmark CLI."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from eval.cc_only.storage import read_json
from eval.context_memory.adapters import (
    LoCoMoAdapter,
    LongMemEvalAdapter,
    MemoryAgentBenchAdapter,
)
from eval.context_memory.aggregate import aggregate_reports, result_profile
from eval.context_memory.contracts import MODEL, EvalProfile
from eval.context_memory.prepare import prepare_benchmark
from eval.context_memory.runner import run_context_memory_benchmark

ADAPTERS = {
    "longmemeval": LongMemEvalAdapter,
    "locomo": LoCoMoAdapter,
    "memoryagentbench": MemoryAgentBenchAdapter,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark", choices=(*ADAPTERS, "aggregate"))
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--profile", choices=("portfolio", "full"), default="portfolio")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Run only the first N deterministic tasks in an isolated result root",
    )
    parser.add_argument("--check", action="store_true", help="Run zero-model-call readiness checks")
    parser.add_argument(
        "--prepare", action="store_true", help="Prepare pinned upstream inputs first"
    )
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--watchdog-seconds", type=int, default=7_200)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.project_root.resolve()
    profile = EvalProfile(args.profile)
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    if args.benchmark == "aggregate":
        paths = aggregate_reports(
            root, profile=profile, task_limit=args.limit, check_only=args.check
        )
        for name, path in paths.items():
            print(f"{name}={path}")
        status = read_json(paths["summary"])["status"]
        print("model_calls=0")
        return 0 if status == "complete" else 2
    if args.prepare or args.prepare_only:
        manifest = prepare_benchmark(root, args.benchmark)
        print(f"prepared_files={len(manifest.get('files') or [])}")
        print(f"revision={manifest.get('revision')}")
        if args.prepare_only:
            return 0
    if not args.check and not args.confirm_live:
        raise SystemExit(
            "live execution requires --confirm-live; use --check for zero-model readiness"
        )
    adapter = ADAPTERS[args.benchmark]()
    base = root / "eval" / "result" / "cc-only" / "context-memory" / MODEL
    profile_slug = result_profile(profile, args.limit)
    output = (
        base / "check" / profile_slug / adapter.slug
        if args.check
        else base / profile_slug / adapter.slug
    )
    paths = asyncio.run(
        run_context_memory_benchmark(
            adapter,
            root,
            output,
            profile=profile,
            task_limit=args.limit,
            check_only=args.check,
            watchdog_seconds=args.watchdog_seconds,
        )
    )
    for name, path in paths.items():
        print(f"{name}={path}")
    summary = read_json(paths["summary"])
    print(f"model_calls={0 if args.check else 'see-summary-usage'}")
    return 0 if summary["status"] in {"ready", "complete"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
