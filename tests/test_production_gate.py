from __future__ import annotations

from pathlib import Path

from cc_harness.production_gate import GateCommandResult, ProductionReadinessGate


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    return project


def test_dry_run_plan_contains_deployment_and_cleanup_steps(tmp_path: Path) -> None:
    gate = ProductionReadinessGate(
        _project(tmp_path),
        health_url="http://127.0.0.1:8080/health",
        migration_command=("docker", "compose", "run", "migrate"),
        smoke_command=("pytest", "tests/production"),
    )

    assert [step.name for step in gate.plan()] == [
        "compose_config",
        "image_build",
        "compose_up",
        "migration",
        "health",
        "smoke",
        "compose_down",
    ]


def test_gate_requires_health_and_cleans_up_after_success(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...], _cwd: Path, _timeout: float) -> GateCommandResult:
        calls.append(command)
        return GateCommandResult(0, "ok", "")

    gate = ProductionReadinessGate(
        _project(tmp_path),
        health_url="http://health.local/ready",
        runner=runner,
        health_probe=lambda _url, _timeout: GateCommandResult(0, "healthy", ""),
    )
    report = gate.run()

    assert report.ready is True
    assert [check.status for check in report.checks] == ["pass"] * 5
    assert calls[-1][1:] == ("compose", "-f", str(gate.compose_file), "down", "--remove-orphans")


def test_gate_stops_before_start_when_config_fails(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...], _cwd: Path, _timeout: float) -> GateCommandResult:
        calls.append(command)
        return GateCommandResult(1, "", "invalid compose")

    gate = ProductionReadinessGate(_project(tmp_path), health_url="http://health.local/ready", runner=runner)
    report = gate.run()

    assert report.ready is False
    assert report.checks[0].name == "compose_config"
    assert report.checks[0].status == "fail"
    assert all(check.status == "skipped" for check in report.checks[1:])
    assert not any(command[-2:] == ("down", "--remove-orphans") for command in calls)


def test_gate_cleanup_failure_invalidates_readiness(tmp_path: Path) -> None:
    def runner(command: tuple[str, ...], _cwd: Path, _timeout: float) -> GateCommandResult:
        if command[-2:] == ("down", "--remove-orphans"):
            return GateCommandResult(1, "", "cleanup failed")
        return GateCommandResult(0)

    gate = ProductionReadinessGate(
        _project(tmp_path),
        health_url="http://health.local/ready",
        runner=runner,
        health_probe=lambda _url, _timeout: GateCommandResult(0),
    )
    report = gate.run()

    assert report.ready is False
    assert report.checks[-1].name == "compose_down"
    assert report.checks[-1].status == "fail"


def test_gate_cleans_up_after_partial_compose_start_failure(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...], _cwd: Path, _timeout: float) -> GateCommandResult:
        calls.append(command)
        if command[-1:] == ("-d",):
            return GateCommandResult(1, "created api", "one service failed")
        return GateCommandResult(0)

    gate = ProductionReadinessGate(
        _project(tmp_path),
        health_url="http://health.local/ready",
        runner=runner,
    )
    report = gate.run()

    assert report.ready is False
    assert report.checks[2].name == "compose_up"
    assert report.checks[2].status == "fail"
    assert report.checks[-1].name == "compose_down"
    assert report.checks[-1].status == "pass"
    assert calls[-1][-2:] == ("down", "--remove-orphans")
