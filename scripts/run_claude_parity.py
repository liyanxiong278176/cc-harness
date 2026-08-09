"""Run the unified cc-harness versus Claude Code parity evaluation."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

from eval.parity import DEFAULT_CLAUDE_CODE_VERSION, ParitySuite, analyze_imported_parity
from eval.parity.live import default_parity_result_root, run_live_parity


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--evidence-root", type=Path, default=None)
    parser.add_argument("--suite", choices=[item.value for item in ParitySuite], default="smoke")
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--import-evidence", action="append", type=Path, default=[])
    parser.add_argument("--repetitions", type=int, default=None)
    parser.add_argument("--random-seed", type=int, default=20260805)
    parser.add_argument("--maximum-attempts", type=int, default=2)
    parser.add_argument("--cooldown-seconds", type=float, default=30.0)
    parser.add_argument(
        "--observe-unbounded",
        action="store_true",
        help="Record natural harness usage without enforcing task resource limits.",
    )
    parser.add_argument(
        "--emergency-watchdog-seconds",
        type=int,
        default=3600,
        help="Infrastructure-only process watchdog for observational runs.",
    )
    parser.add_argument(
        "--expected-claude-version",
        default=DEFAULT_CLAUDE_CODE_VERSION,
    )
    parser.add_argument(
        "--claude-settings",
        type=Path,
        default=Path.home() / ".claude" / "settings.json",
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Required acknowledgement that this command makes paid model API calls.",
    )
    return parser


async def async_main() -> int:
    args = build_parser().parse_args()
    project_root = args.project_root.resolve()
    evidence_root = (
        args.evidence_root.resolve()
        if args.evidence_root is not None
        else default_parity_result_root(project_root)
    )
    if args.import_evidence:
        result = analyze_imported_parity(
            tuple(args.import_evidence),
            evidence_root,
            suite=ParitySuite(args.suite),
            expected_claude_code_version=args.expected_claude_version,
        )
    else:
        if not args.confirm_live:
            raise SystemExit("refusing live model calls without --confirm-live")
        result = await run_live_parity(
            project_root,
            evidence_root,
            suite=ParitySuite(args.suite),
            claude_settings_path=args.claude_settings,
            task_ids=tuple(args.task),
            repetitions=args.repetitions,
            random_seed=args.random_seed,
            maximum_attempts=args.maximum_attempts,
            cooldown_seconds=args.cooldown_seconds,
            expected_claude_code_version=args.expected_claude_version,
            observe_unbounded=args.observe_unbounded,
            emergency_watchdog_seconds=args.emergency_watchdog_seconds,
            progress=_print_progress,
        )
    print(f"evidence_root={result.evidence_root}")
    print(f"summary={result.summary_path}")
    print(f"report={result.report_path}")
    print(f"integrity={result.integrity_path}")
    print(f"conclusion={result.conclusion}")
    print(f"summary_digest={result.summary_digest}")
    print(f"report_digest={result.report_digest}")
    return 0


def _print_progress(message: str) -> None:
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
