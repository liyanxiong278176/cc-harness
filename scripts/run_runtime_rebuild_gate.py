"""Run the non-compensating Runtime Rebuild Release Gate."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    exit_code: int
    command: tuple[str, ...]
    output_tail: str


def _run(name: str, command: list[str]) -> GateResult:
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    combined = (completed.stdout + "\n" + completed.stderr).strip()
    return GateResult(name, completed.returncode == 0, completed.returncode, tuple(command), combined[-4000:])


def _ruff_command() -> list[str] | None:
    candidates = [
        ROOT / ".venv" / "Scripts" / "ruff.exe",
        Path(shutil.which("ruff") or ""),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return [str(candidate), "check", "cc_harness", "tests", "scripts"]
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="run every rebuild gate")
    parser.add_argument("--skip-static", action="store_true")
    parser.add_argument("--fixture", type=Path, default=ROOT / "tests" / "runtime_rebuild" / "fixtures" / "legacy")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)
    if not args.all:
        parser.error("--all is required for the release gate")

    results: list[GateResult] = []
    results.append(
        _run(
            "runtime_rebuild_tests",
            [sys.executable, "-m", "pytest", "tests/runtime_rebuild", "-q"],
        )
    )
    if not args.skip_static:
        command = _ruff_command()
        if command is None:
            results.append(GateResult("static_check", False, 127, (), "ruff executable not found"))
        else:
            results.append(_run("static_check", command))
    results.append(
        _run(
            "migration_reconciliation",
            [sys.executable, "scripts/check_runtime_rebuild_migration.py", "--dry-run", "--fixture", str(args.fixture)],
        )
    )
    report = {
        "schema_version": "runtime-rebuild-gate.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "passed": all(item.passed for item in results),
        "gates": [asdict(item) for item in results],
        "non_compensating": True,
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(encoded)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
