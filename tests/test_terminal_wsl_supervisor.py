from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from scripts import terminal_bench_wsl_supervisor as supervisor


def test_dead_worker_is_reconciled_before_restarting(
    monkeypatch, tmp_path: Path
) -> None:
    state_root = tmp_path / "supervisor"
    monkeypatch.setattr(supervisor, "STATE_ROOT", state_root)
    monkeypatch.setattr(supervisor, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(supervisor, "_alive", lambda _pid: False)
    monkeypatch.setenv("CC_HARNESS_TERMINAL_SUPERVISOR_KIND", "evaluation")

    arguments = ["--task-manifest", "hard.json", "--confirm-live"]
    run_root = state_root / "evaluation" / supervisor._identity(arguments)
    run_root.mkdir(parents=True)
    (run_root / "status.json").write_text(
        json.dumps(
            {
                "state": "running",
                "worker_pid": 4242,
                "started_at": "2026-08-24T08:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        supervisor,
        "_start_systemd_worker",
        lambda *args, **kwargs: (5151, "cc-harness-terminal-test.service"),
    )
    observed: dict[str, object] = {}

    def fake_follow(path: Path, pid: int) -> int:
        observed.update(json.loads((path / "status.json").read_text(encoding="utf-8")))
        observed["follow_pid"] = pid
        return 0

    monkeypatch.setattr(supervisor, "_follow", fake_follow)
    monkeypatch.setattr(supervisor.sys, "argv", ["supervisor", *arguments])

    assert supervisor.main() == 0
    assert observed["state"] == "starting"
    assert observed["worker_pid"] == 5151
    assert observed["follow_pid"] == 5151
    assert observed["recovery_events"] == [
        {
            "previous_state": "running",
            "previous_worker_pid": 4242,
            "reason": "stale-worker-recovered",
            "detected_at": observed["recovery_events"][0]["detected_at"],
        }
    ]


def test_worker_is_owned_by_user_systemd(monkeypatch, tmp_path: Path) -> None:
    state_root = tmp_path / "supervisor"
    monkeypatch.setattr(supervisor, "STATE_ROOT", state_root)
    monkeypatch.setattr(supervisor, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(supervisor, "_alive", lambda _pid: False)
    monkeypatch.setenv("CC_HARNESS_TERMINAL_SUPERVISOR_KIND", "evaluation")
    systemd_run = tmp_path / "systemd-run"
    systemctl = tmp_path / "systemctl"
    systemd_run.touch()
    systemctl.touch()
    monkeypatch.setattr(supervisor, "SYSTEMD_RUN", systemd_run)
    monkeypatch.setattr(supervisor, "SYSTEMCTL", systemctl)
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("worker must not remain a child of the WSL client")
        ),
    )

    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        commands.append(list(command))
        if command[0].endswith("systemd-run"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[0].endswith("systemctl") and command[1:3] == ["--user", "show"]:
            return SimpleNamespace(returncode=0, stdout="6161\n", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(supervisor.subprocess, "run", fake_run)
    observed: dict[str, object] = {}

    def fake_follow(path: Path, pid: int) -> int:
        observed.update(json.loads((path / "status.json").read_text(encoding="utf-8")))
        observed["follow_pid"] = pid
        return 0

    monkeypatch.setattr(supervisor, "_follow", fake_follow)
    arguments = ["--task-manifest", "hard.json", "--confirm-live"]
    monkeypatch.setattr(supervisor.sys, "argv", ["supervisor", *arguments])

    assert supervisor.main() == 0
    launch = next(command for command in commands if command[0].endswith("systemd-run"))
    assert launch[1:3] == ["--user", "--quiet"]
    assert "--collect" in launch
    assert any(item.startswith("--unit=cc-harness-terminal-") for item in launch)
    assert any(item.startswith("--property=StandardOutput=append:") for item in launch)
    assert observed["follow_pid"] == 6161


def test_systemd_worker_receives_wsl_docker_and_terminal_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.setenv("CC_HARNESS_TERMINAL_AGENT_RUNTIME", "1")
    monkeypatch.setenv("CC_HARNESS_TERMINAL_NETWORK_TRANSPORT", "proxy")

    arguments = supervisor._worker_environment_arguments()

    assert "--setenv=WSL_DISTRO_NAME=Ubuntu" in arguments
    assert "--setenv=DOCKER_HOST=unix:///var/run/docker.sock" in arguments
    assert "--setenv=CC_HARNESS_TERMINAL_AGENT_RUNTIME=1" in arguments
    assert "--setenv=CC_HARNESS_TERMINAL_NETWORK_TRANSPORT=proxy" in arguments


def test_new_run_rotates_stale_worker_log_before_launch(monkeypatch, tmp_path: Path) -> None:
    state_root = tmp_path / "supervisor"
    monkeypatch.setattr(supervisor, "STATE_ROOT", state_root)
    monkeypatch.setattr(supervisor, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(supervisor, "_alive", lambda _pid: False)
    monkeypatch.setenv("CC_HARNESS_TERMINAL_SUPERVISOR_KIND", "evaluation")
    arguments = ["--task-manifest", "hard.json", "--new-run", "--confirm-live"]
    run_root = state_root / "evaluation" / supervisor._identity(arguments)
    run_root.mkdir(parents=True)
    (run_root / "worker.log").write_text("old scored run\n", encoding="utf-8")
    (run_root / "status.json").write_text(
        json.dumps({"state": "interrupted", "worker_pid": 4242}), encoding="utf-8"
    )
    monkeypatch.setattr(
        supervisor,
        "_start_systemd_worker",
        lambda *args, **kwargs: (5151, "cc-harness-terminal-test.service"),
    )
    monkeypatch.setattr(supervisor, "_follow", lambda _path, _pid: 0)
    monkeypatch.setattr(supervisor.sys, "argv", ["supervisor", *arguments])

    assert supervisor.main() == 0
    archived = list(run_root.glob("worker.previous-*.log"))
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == "old scored run\n"
    assert not (run_root / "worker.log").exists()
