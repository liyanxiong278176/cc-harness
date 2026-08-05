"""Run real Docker sandbox probes and preserve versioned evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from cc_harness.sandbox_evidence import REPORT_SCHEMA, control_bundle_digest

ROOT = Path(__file__).resolve().parents[1]


def _preferred_python() -> Path:
    executable = "python.exe" if sys.platform == "win32" else "python"
    candidate = ROOT / ".venv" / ("Scripts" if sys.platform == "win32" else "bin") / executable
    return candidate if candidate.exists() else Path(sys.executable)


def _run_text(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _run_text_or_none(command: list[str]) -> str | None:
    try:
        return _run_text(command)
    except (OSError, subprocess.CalledProcessError):
        return None


def _git_value(*args: str) -> str:
    return _run_text(["git", *args])


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _package_version_or_none(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _junit_results(path: Path) -> dict:
    if not path.exists():
        return {"passed": [], "failed": [], "skipped": []}
    root = ET.parse(path).getroot()
    results = {"passed": [], "failed": [], "skipped": []}
    for case in root.iter("testcase"):
        name = case.attrib.get("name", "")
        if case.find("failure") is not None or case.find("error") is not None:
            results["failed"].append(name)
        elif case.find("skipped") is not None:
            results["skipped"].append(name)
        else:
            results["passed"].append(name)
    return results


def main() -> int:
    preferred_python = _preferred_python().resolve()
    if Path(sys.executable).resolve() != preferred_python:
        return subprocess.run(
            [str(preferred_python), str(Path(__file__).resolve()), *sys.argv[1:]],
            cwd=ROOT,
            check=False,
        ).returncode

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="evidence directory; defaults to eval/result/sandbox-conformance/<UTC timestamp>",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="reuse the existing cc-harness-runtime:local image",
    )
    args = parser.parse_args()

    started = datetime.now(UTC)
    source_commit = _git_value("rev-parse", "HEAD")
    source_status = _git_value("status", "--porcelain")
    bundle_digest = control_bundle_digest(ROOT)
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or (
        ROOT / "eval" / "result" / "sandbox-conformance" / run_id
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    junit_path = output_dir / "pytest.junit.xml"
    build_log_path = output_dir / "docker-build.log"

    build_exit_code = None
    if not args.no_build:
        with build_log_path.open("wb") as build_log:
            build = subprocess.run(
                [
                    "docker",
                    "build",
                    "--tag",
                    "cc-harness-runtime:local",
                    str(ROOT / "sandboxes"),
                ],
                cwd=ROOT,
                check=False,
                stdout=build_log,
                stderr=subprocess.STDOUT,
            )
        build_exit_code = build.returncode

    pytest_exit_code = None
    if build_exit_code in (None, 0):
        environment = os.environ.copy()
        environment["CC_HARNESS_RUN_SANDBOX_CONFORMANCE"] = "1"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_sandbox_conformance.py",
                "-m",
                "sandbox_conformance",
                "-q",
                f"--junitxml={junit_path}",
            ],
            cwd=ROOT,
            env=environment,
            check=False,
        )
        pytest_exit_code = completed.returncode
    finished = datetime.now(UTC)
    passed = pytest_exit_code == 0
    report = {
        "schema_version": REPORT_SCHEMA,
        "run_id": run_id,
        "status": "passed" if passed else "failed",
        "failure_stage": (
            None if passed else "build" if build_exit_code not in (None, 0) else "pytest"
        ),
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_ms": int((finished - started).total_seconds() * 1000),
        "source": {
            "commit": source_commit,
            "dirty": bool(source_status),
        },
        "control_bundle_digest": bundle_digest,
        "tests": _junit_results(junit_path),
        "environment": {
            "os": platform.system(),
            "os_release": platform.release(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "docker_server": _run_text_or_none(
                ["docker", "info", "--format", "{{.ServerVersion}}"]
            ),
            "opensandbox": _package_version_or_none("opensandbox"),
            "opensandbox_server": _package_version_or_none("opensandbox-server"),
            "image_id": _run_text_or_none(
                [
                    "docker",
                    "image",
                    "inspect",
                    "cc-harness-runtime:local",
                    "--format",
                    "{{.Id}}",
                ]
            ),
        },
        "artifacts": {
            "docker_build_log": build_log_path.name if build_log_path.exists() else None,
            "docker_build_log_digest": _sha256(build_log_path),
            "junit": junit_path.name,
            "junit_digest": _sha256(junit_path),
        },
        "build_exit_code": build_exit_code,
        "pytest_exit_code": pytest_exit_code,
    }
    report_path = output_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(report_path)
    return pytest_exit_code if pytest_exit_code is not None else build_exit_code or 1


if __name__ == "__main__":
    raise SystemExit(main())
