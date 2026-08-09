"""Prepare and verify local prerequisites for the four specialist eval suites."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from eval.specialist.readiness import run_specialist_readiness


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--claude-settings", type=Path, default=None)
    return parser


async def async_main() -> int:
    args = build_parser().parse_args()
    project_root = args.project_root.resolve()
    output_root = (
        args.output_root.resolve()
        if args.output_root is not None
        else project_root / "eval" / "result" / "specialist-readiness"
    )
    report = await run_specialist_readiness(
        project_root,
        output_root,
        claude_settings_path=args.claude_settings,
    )
    print(f"ready={'yes' if report.ready else 'no'}")
    print(f"catalog_digest={report.catalog_digest}")
    print(f"catalog={output_root / 'catalog.json'}")
    print(f"readiness={output_root / 'readiness.json'}")
    print(f"report={output_root / 'readiness.md'}")
    print(f"integrity={output_root / 'integrity.json'}")
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
