"""Run a resumable live Harbor cc-harness versus Claude Code comparison."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from eval.harbor.paired import run_harbor_parity
from eval.parity import ParitySuite


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--task", action="append", required=True)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--random-seed", type=int, default=20260806)
    parser.add_argument("--maximum-attempts", type=int, default=2)
    parser.add_argument("--cooldown-seconds", type=float, default=30.0)
    parser.add_argument("--suite", choices=[item.value for item in ParitySuite], default="dev")
    parser.add_argument(
        "--wheel",
        type=Path,
        default=Path("eval/result/harbor-wheel/cc_harness-0.1.0-py3-none-any.whl"),
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--claude-settings", type=Path, default=Path.home() / ".claude" / "settings.json"
    )
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()
    if not args.confirm_live:
        raise SystemExit("refusing live Harbor model calls without --confirm-live")
    project_root = args.project_root.resolve()
    bundle = run_harbor_parity(
        project_root,
        args.output_root,
        task_names=tuple(args.task),
        wheel_path=(project_root / args.wheel if not args.wheel.is_absolute() else args.wheel),
        env_file=(
            project_root / args.env_file if not args.env_file.is_absolute() else args.env_file
        ),
        claude_settings_path=args.claude_settings,
        repetitions=args.repetitions,
        random_seed=args.random_seed,
        maximum_attempts=args.maximum_attempts,
        cooldown_seconds=args.cooldown_seconds,
        suite=ParitySuite(args.suite),
        progress=_progress,
    )
    print(f"bundle={bundle}")
    print(f"analysis={args.output_root.resolve() / 'analysis'}")
    return 0


def _progress(message: str) -> None:
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
