"""Run the versioned advanced cc-harness vs Claude Code canary."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

from eval.canary.live import default_evidence_root, run_live_advanced_canary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--evidence-root", type=Path, default=None)
    parser.add_argument(
        "--claude-settings",
        type=Path,
        default=Path.home() / ".claude" / "settings.json",
    )
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--maximum-attempts", type=int, default=2)
    parser.add_argument("--cooldown-seconds", type=float, default=30.0)
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Required acknowledgement that the command makes paid model API calls.",
    )
    return parser


async def async_main() -> int:
    args = build_parser().parse_args()
    if not args.confirm_live:
        raise SystemExit("refusing live model calls without --confirm-live")
    project_root = args.project_root.resolve()
    evidence_root = (
        args.evidence_root.resolve()
        if args.evidence_root is not None
        else default_evidence_root(project_root)
    )
    result = await run_live_advanced_canary(
        project_root,
        evidence_root,
        claude_settings_path=args.claude_settings,
        task_ids=tuple(args.task),
        maximum_attempts=args.maximum_attempts,
        cooldown_seconds=args.cooldown_seconds,
        progress=_print_progress,
    )
    print(f"evidence_root={result.evidence_root}")
    print(f"summary={result.summary_path}")
    print(f"report={result.report_path}")
    print(f"summary_digest={result.summary_digest}")
    print(f"report_digest={result.report_digest}")
    return 0


def _print_progress(message: str) -> None:
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
