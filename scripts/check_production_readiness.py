"""Run the no-model production readiness gate for a compose deployment."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

# Running a script by path puts ``scripts/`` (not the repository root) on
# sys.path.  Keep the documented checkout invocation working without asking
# users to install the package first.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_harness.production_gate import ProductionReadinessGate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--compose-file", type=Path, default=Path("docker-compose.yml"))
    parser.add_argument("--health-url", required=False)
    parser.add_argument("--migration-command", nargs="+", default=())
    parser.add_argument("--smoke-command", nargs="+", default=())
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.project_root.resolve()
    gate = ProductionReadinessGate(
        root,
        compose_file=args.compose_file,
        health_url=args.health_url,
        migration_command=args.migration_command,
        smoke_command=args.smoke_command,
        timeout_seconds=args.timeout,
    )
    if args.dry_run:
        payload = {
            "schema_version": "cc-harness.production-readiness-plan.v1",
            "project_root": str(root),
            "steps": [
                {
                    "name": step.name,
                    "command": list(step.command),
                    "timeout_seconds": step.timeout_seconds,
                    "required": step.required,
                }
                for step in gate.plan()
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    report = gate.run()
    print(report.to_json() if args.as_json else report.to_markdown(), end="")
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
