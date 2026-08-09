from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from eval.safety import run_safety_evaluation


def main() -> int:
    parser = argparse.ArgumentParser(description="Run or resume the safety evaluation")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--claude-settings", type=Path, default=Path.home() / ".claude" / "settings.json")
    parser.add_argument("--track", choices=("default", "hardened"), default="default")
    parser.add_argument("--watchdog-seconds", type=int, default=900)
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()
    if not args.confirm_live:
        raise SystemExit("refusing live model calls without --confirm-live")
    project = args.project_root.resolve()
    output = args.output_root or (
        project / "eval" / "result" / f"specialist-safety8-{args.track}-v1-deepseek-v4-flash"
    )
    paths = asyncio.run(run_safety_evaluation(
        project,
        output,
        claude_settings_path=args.claude_settings,
        track=args.track,
        watchdog_seconds=args.watchdog_seconds,
    ))
    for name, path in paths.items():
        print(f"{name}={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
