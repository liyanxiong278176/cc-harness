"""No-model production readiness gate.

The gate is deliberately separate from the agent loop.  It validates the
runtime a deployment will actually use (compose config/build/start, health,
optional migration and smoke checks, and cleanup) and emits structured
evidence.  A green unit-test suite alone never makes this report ready.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence


_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|password|secret)(\s*[=:]\s*)[^\s,;]+"
)


def _redact(text: str) -> str:
    return _SECRET_RE.sub(r"\1\2<redacted>", str(text))


@dataclass(frozen=True)
class GateCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class ReadinessStep:
    name: str
    command: tuple[str, ...] = ()
    timeout_seconds: float = 120.0
    required: bool = True


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    status: str
    command: tuple[str, ...] = ()
    returncode: int | None = None
    duration_ms: int = 0
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "command": list(self.command),
            "returncode": self.returncode,
            "duration_ms": self.duration_ms,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
        }


@dataclass(frozen=True)
class ProductionReadinessReport:
    project_root: str
    compose_file: str
    generated_at: str
    ready: bool
    checks: tuple[ReadinessCheck, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "cc-harness.production-readiness.v1",
            "project_root": self.project_root,
            "compose_file": self.compose_file,
            "generated_at": self.generated_at,
            "ready": self.ready,
            "checks": [check.to_dict() for check in self.checks],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"

    def to_markdown(self) -> str:
        lines = [
            "# Production readiness",
            "",
            f"- Ready: **{'yes' if self.ready else 'no'}**",
            f"- Compose: `{self.compose_file}`",
            f"- Generated: `{self.generated_at}`",
            "",
            "| Step | Status | Exit | Duration |",
            "| --- | --- | ---: | ---: |",
        ]
        for check in self.checks:
            lines.append(
                f"| `{check.name}` | `{check.status}` | "
                f"{check.returncode if check.returncode is not None else '-'} | "
                f"{check.duration_ms} ms |"
            )
        lines.append("")
        lines.append(
            "Readiness is false when a required step, health probe, smoke check, "
            "or cleanup step fails."
        )
        return "\n".join(lines) + "\n"


Runner = Callable[[tuple[str, ...], Path, float], GateCommandResult]
HealthProbe = Callable[[str, float], GateCommandResult]


def _subprocess_runner(command: tuple[str, ...], cwd: Path, timeout: float) -> GateCommandResult:
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=max(1.0, timeout),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return GateCommandResult(
            returncode=124,
            stdout=_redact(exc.stdout or ""),
            stderr=_redact(exc.stderr or "command timed out"),
        )
    except OSError as exc:
        return GateCommandResult(returncode=127, stderr=_redact(str(exc)))
    return GateCommandResult(
        returncode=int(completed.returncode),
        stdout=_redact(completed.stdout or ""),
        stderr=_redact(completed.stderr or ""),
    )


def _http_probe(url: str, timeout: float) -> GateCommandResult:
    request = urllib.request.Request(url, headers={"User-Agent": "cc-harness-readiness/1"})
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, timeout)) as response:
            body = response.read(2048).decode("utf-8", errors="replace")
            status = int(getattr(response, "status", 200))
            return GateCommandResult(0 if status == 200 else 1, body, f"HTTP {status}")
    except urllib.error.HTTPError as exc:
        return GateCommandResult(int(exc.code), "", f"HTTP {exc.code}")
    except (OSError, urllib.error.URLError) as exc:
        return GateCommandResult(1, "", str(exc))


class ProductionReadinessGate:
    """Run the deployment checks with injectable command and health runners."""

    def __init__(
        self,
        project_root: Path,
        *,
        compose_file: Path | str = "docker-compose.yml",
        health_url: str | None = None,
        migration_command: Sequence[str] | None = None,
        smoke_command: Sequence[str] | None = None,
        runner: Runner = _subprocess_runner,
        health_probe: HealthProbe = _http_probe,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=True)
        compose = Path(compose_file)
        self.compose_file = (self.project_root / compose).resolve() if not compose.is_absolute() else compose.resolve()
        self.health_url = health_url
        self.migration_command = tuple(str(value) for value in (migration_command or ()))
        self.smoke_command = tuple(str(value) for value in (smoke_command or ()))
        self.runner = runner
        self.health_probe = health_probe
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def _compose(self, *args: str) -> tuple[str, ...]:
        return ("docker", "compose", "-f", str(self.compose_file), *args)

    def plan(self) -> tuple[ReadinessStep, ...]:
        steps: list[ReadinessStep] = [
            ReadinessStep("compose_config", self._compose("config")),
            ReadinessStep("image_build", self._compose("build", "--pull"), 900.0),
            ReadinessStep("compose_up", self._compose("up", "-d"), self.timeout_seconds),
        ]
        if self.migration_command:
            steps.append(ReadinessStep("migration", self.migration_command, 600.0))
        if self.health_url:
            steps.append(ReadinessStep("health", ("HTTP", self.health_url), self.timeout_seconds))
        else:
            steps.append(ReadinessStep("health", (), self.timeout_seconds))
        if self.smoke_command:
            steps.append(ReadinessStep("smoke", self.smoke_command, 900.0))
        steps.append(ReadinessStep("compose_down", self._compose("down", "--remove-orphans"), 120.0))
        return tuple(steps)

    def _record_command(self, step: ReadinessStep) -> ReadinessCheck:
        started = time.monotonic()
        result = self.runner(step.command, self.project_root, step.timeout_seconds)
        return ReadinessCheck(
            name=step.name,
            status="pass" if result.returncode == 0 else "fail",
            command=step.command,
            returncode=result.returncode,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            stdout=_redact(result.stdout),
            stderr=_redact(result.stderr),
        )

    def run(self) -> ProductionReadinessReport:
        checks: list[ReadinessCheck] = []
        started_runtime = False
        failed = False
        for step in self.plan():
            if step.name == "compose_down":
                continue
            if failed:
                checks.append(
                    ReadinessCheck(
                        name=step.name,
                        status="skipped",
                        command=step.command,
                        error="a required readiness step failed",
                    )
                )
                continue
            if step.name == "health":
                if not self.health_url:
                    check = ReadinessCheck(
                        name="health",
                        status="fail",
                        error="health_url is required for a production readiness verdict",
                    )
                else:
                    started = time.monotonic()
                    try:
                        result = self.health_probe(self.health_url, step.timeout_seconds)
                        check = ReadinessCheck(
                            name="health",
                            status="pass" if result.returncode == 0 else "fail",
                            command=step.command,
                            returncode=result.returncode,
                            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                            stdout=_redact(result.stdout),
                            stderr=_redact(result.stderr),
                        )
                    except Exception as exc:  # probe failures are evidence, not gate crashes
                        check = ReadinessCheck(
                            name="health",
                            status="fail",
                            command=step.command,
                            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                            error=_redact(str(exc)),
                        )
                checks.append(check)
                failed = check.status != "pass"
                continue
            check = self._record_command(step)
            checks.append(check)
            if step.name == "compose_up":
                # `docker compose up` may create some containers before
                # returning a non-zero code. Treat every attempted `up` as
                # potentially dirty and run the idempotent cleanup command.
                started_runtime = True
            failed = check.status != "pass"

        # Always clean up after a successful start, even when a later check fails.
        if started_runtime:
            down_step = next(step for step in self.plan() if step.name == "compose_down")
            down_check = self._record_command(down_step)
            checks.append(down_check)
            if down_check.status != "pass":
                failed = True
        else:
            checks.append(
                ReadinessCheck(
                    name="compose_down",
                    status="skipped",
                    command=self._compose("down", "--remove-orphans"),
                    error="compose was not started",
                )
            )
        return ProductionReadinessReport(
            project_root=str(self.project_root),
            compose_file=str(self.compose_file),
            generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            ready=not failed and all(check.status == "pass" for check in checks),
            checks=tuple(checks),
        )


__all__ = [
    "GateCommandResult",
    "ProductionReadinessGate",
    "ProductionReadinessReport",
    "ReadinessCheck",
    "ReadinessStep",
]
