"""Run or resume frozen controlled specialist comparisons."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

from eval.parity.validation import DEFAULT_CLAUDE_CODE_VERSION
from eval.specialist.models import SpecialistSuite
from eval.specialist.paired import (
    ALL_SPECIALIST_SUITES,
    check_specialist_run_inputs,
    default_output_root,
    run_specialist_parity,
)


def main() -> int:
    args = _parser().parse_args()
    project_root = args.project_root.expanduser().resolve()
    suites = tuple(SpecialistSuite(value) for value in args.suite) or ALL_SPECIALIST_SUITES
    output_root = _resolve(project_root, args.output_root, suites)
    claude_settings = args.claude_settings.expanduser().resolve()
    checked = check_specialist_run_inputs(
        project_root,
        output_root,
        claude_settings_path=claude_settings,
        context_window_tokens=args.context_window_tokens,
        random_seed=args.random_seed,
        maximum_attempts=args.maximum_attempts,
        watchdog_seconds=args.watchdog_seconds,
        expected_claude_version=args.expected_claude_version,
        suites=suites,
    )
    _print_check(checked, output_root)
    if args.check:
        print("preflight=ok")
        print("model_calls=0")
        return 0
    if not args.confirm_live:
        raise SystemExit("refusing live model calls without --confirm-live")

    try:
        paths = asyncio.run(
            run_specialist_parity(
                project_root,
                output_root,
                claude_settings_path=claude_settings,
                context_window_tokens=args.context_window_tokens,
                random_seed=args.random_seed,
                maximum_attempts=args.maximum_attempts,
                cooldown_seconds=args.cooldown_seconds,
                watchdog_seconds=args.watchdog_seconds,
                expected_claude_version=args.expected_claude_version,
                suites=suites,
                progress=_progress,
            )
        )
    except KeyboardInterrupt:
        print("\nEvaluation interrupted. The active child process was stopped.", flush=True)
        print("Run the same CMD command again to resume.", flush=True)
        return 130

    for name, path in paths.items():
        print(f"{name}={path}")
    print(f"state={output_root / 'state.json'}")
    print(f"raw={output_root / 'raw'}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-root", type=Path)
    parser.add_argument(
        "--suite",
        action="append",
        choices=tuple(item.value for item in SpecialistSuite),
        default=[],
        help="Specialist suite to run; repeat to combine suites. Default: all.",
    )
    parser.add_argument(
        "--claude-settings", type=Path, default=Path.home() / ".claude" / "settings.json"
    )
    parser.add_argument("--context-window-tokens", type=int, default=128_000)
    parser.add_argument("--random-seed", type=int, default=20260807)
    parser.add_argument("--maximum-attempts", type=int, default=2)
    parser.add_argument("--cooldown-seconds", type=float, default=30.0)
    parser.add_argument("--watchdog-seconds", type=int, default=7_200)
    parser.add_argument("--expected-claude-version", default=DEFAULT_CLAUDE_CODE_VERSION)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    return parser


def _resolve(
    project_root: Path,
    value: Path | None,
    suites: tuple[SpecialistSuite, ...],
) -> Path:
    if value is None:
        return default_output_root(project_root, suites)
    expanded = value.expanduser()
    return expanded.resolve() if expanded.is_absolute() else (project_root / expanded).resolve()


def _print_check(checked: dict[str, object], output_root: Path) -> None:
    print(f"mode={'resume' if (output_root / 'state.json').is_file() else 'new'}")
    for key in (
        "output_root",
        "task_count",
        "pair_count",
        "trial_count",
        "catalog_digest",
        "source_catalog_digest",
        "suites",
        "context_window_tokens",
        "versions",
        "input_contract_digest",
    ):
        print(f"{key}={checked[key]}")


def _progress(message: str) -> None:
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
