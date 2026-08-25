from __future__ import annotations

import json
from pathlib import Path

from eval.cc_only import terminal_preflight


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_formal_gate_requires_only_official_readiness_contract(
    tmp_path: Path, monkeypatch
) -> None:
    gate = tmp_path / "gates"
    monkeypatch.setattr(terminal_preflight, "gate_root", lambda _root: gate)
    monkeypatch.setattr(
        terminal_preflight, "require_terminal_host", lambda _root: {"ready": True}
    )
    network_checks: list[Path] = []
    monkeypatch.setattr(
        terminal_preflight,
        "require_formal_verifier_network",
        lambda root: network_checks.append(root),
    )
    monkeypatch.setattr(
        terminal_preflight,
        "_identity",
        lambda _wheel: {"wheel_sha256": "sha:test", "dataset": "d", "mode": "coding", "model": "m", "harbor_version": "h"},
    )
    _write_json(gate / "check" / "summary.json", {"status": "ready"})
    _write_json(
        gate / "check" / "manifest.json",
        {"adapter_run_identity": {"wheel_sha256": "sha:test"}},
    )
    terminal_preflight.require_formal_gates(tmp_path, task_limit=5)
    terminal_preflight.require_formal_gates(tmp_path)
    assert network_checks == [tmp_path, tmp_path]


def test_formal_gate_can_validate_an_existing_runs_frozen_wheel(
    tmp_path: Path, monkeypatch
) -> None:
    gate = tmp_path / "gates"
    run_wheel = tmp_path / "run" / "frozen-inputs" / "agent.whl"
    run_wheel.parent.mkdir(parents=True)
    run_wheel.write_bytes(b"frozen-run-wheel")
    monkeypatch.setattr(terminal_preflight, "gate_root", lambda _root: gate)
    monkeypatch.setattr(
        terminal_preflight, "require_terminal_host", lambda _root: {"ready": True}
    )
    monkeypatch.setattr(
        terminal_preflight, "require_formal_verifier_network", lambda _root: None
    )
    observed: list[Path] = []

    def identity(wheel: Path) -> dict[str, str]:
        observed.append(wheel)
        return {"wheel_sha256": "sha:frozen"}

    monkeypatch.setattr(terminal_preflight, "_identity", identity)
    _write_json(gate / "check" / "summary.json", {"status": "ready"})
    _write_json(
        gate / "check" / "manifest.json",
        {"adapter_run_identity": {"wheel_sha256": "sha:frozen"}},
    )

    terminal_preflight.require_formal_gates(
        tmp_path, task_limit=30, frozen_wheel=run_wheel
    )

    assert observed == [run_wheel]


def test_formal_network_gate_uses_a_local_official_task_image(
    tmp_path: Path, monkeypatch
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs):
        commands.append(command)
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(terminal_preflight.subprocess, "run", run)
    monkeypatch.setattr(terminal_preflight.time, "sleep", lambda _seconds: None)

    terminal_preflight.require_formal_verifier_network(tmp_path)

    probe = commands[-1]
    assert probe[:3] == ["docker", "run", "--rm"]
    assert "alexgshaw/bn-fit-modify:20251031" in probe
    assert probe[-3:-1] == ["bash", "-lc"]
    assert "astral.sh/uv" in probe[-1]
    # The official Astral installer performs the full GitHub release download,
    # and uvx resolves the pinned verifier wheels from PyPI.  Keep this probe
    # on that real path instead of replacing it with shallow HEAD requests.
    assert "uvx" in probe[-1]
    assert "pytest-json-ctrf==0.3.5" in probe[-1]
    assert len(commands) == terminal_preflight.FORMAL_NETWORK_PROBE_ATTEMPTS


def test_formal_network_gate_exercises_the_official_verifier_download_path(
    tmp_path: Path, monkeypatch
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs):
        commands.append(command)
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(terminal_preflight.subprocess, "run", run)
    monkeypatch.setattr(terminal_preflight.time, "sleep", lambda _seconds: None)

    terminal_preflight.require_formal_verifier_network(tmp_path)

    script = commands[0][-1]
    assert "apt-get update" in script
    assert "curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh" in script
    assert "source /root/.local/bin/env" in script
    assert "pytest==8.4.1" in script
    assert "pandas==2.3.2" in script
    assert "scipy==1.16.1" in script
    assert "pytest-json-ctrf==0.3.5" in script


def test_formal_network_gate_requires_all_soak_attempts_to_pass(
    tmp_path: Path, monkeypatch
) -> None:
    returncodes = iter((0, 56, 0))
    calls = 0

    def run(_command: list[str], **_kwargs):
        nonlocal calls
        calls += 1
        code = next(returncodes)
        return type(
            "Completed",
            (),
            {"returncode": code, "stdout": "", "stderr": "curl: (56) peer reset"},
        )()

    monkeypatch.setattr(terminal_preflight.subprocess, "run", run)
    monkeypatch.setattr(terminal_preflight.time, "sleep", lambda _seconds: None)

    try:
        terminal_preflight.require_formal_verifier_network(tmp_path)
    except RuntimeError as exc:
        assert "official verifier network is not ready" in str(exc)
    else:
        raise AssertionError("one successful probe must not bypass the soak gate")

    assert calls == terminal_preflight.FORMAL_NETWORK_PROBE_ATTEMPTS
