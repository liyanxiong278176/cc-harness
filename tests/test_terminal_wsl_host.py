from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from eval.cc_only import terminal_host


def _completed(stdout: str, *, returncode: int = 0, stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_wsl_native_host_contract_accepts_native_docker(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(terminal_host.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        terminal_host,
        "_read_text",
        lambda _path: "6.6.87.2-microsoft-standard-WSL2",
    )
    monkeypatch.setattr(terminal_host, "EXPECTED_PROJECT_ROOT", tmp_path.resolve())
    monkeypatch.setattr(terminal_host.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")

    def fake_run(command, *, timeout):
        del timeout
        if command[1:3] == ["context", "show"]:
            return _completed("default\n")
        if command[1] == "info":
            return _completed(
                '"/var/lib/docker"\t"overlayfs"\t"29.1.3"\t"linux"\t"ubuntu"\n'
            )
        if command[0] == "findmnt":
            return _completed("ext4 /dev/sdd /\n")
        raise AssertionError(command)

    monkeypatch.setattr(terminal_host, "_run", fake_run)

    identity = terminal_host.inspect_terminal_host(tmp_path)

    assert identity["ready"] is True
    assert identity["errors"] == []
    assert identity["docker_root"] == "/var/lib/docker"
    assert identity["docker_storage"]["filesystem"] == "ext4"


def test_wsl_native_host_contract_rejects_docker_desktop(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(terminal_host.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        terminal_host,
        "_read_text",
        lambda _path: "6.6.87.2-microsoft-standard-WSL2",
    )
    monkeypatch.setattr(terminal_host, "EXPECTED_PROJECT_ROOT", tmp_path.resolve())
    monkeypatch.setattr(
        terminal_host.shutil,
        "which",
        lambda name: "/mnt/d/docker/resources/bin/docker",
    )
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.delenv("DOCKER_HOST", raising=False)

    def fake_run(command, *, timeout):
        del timeout
        if command[1:3] == ["context", "show"]:
            return _completed("desktop-linux\n")
        if command[1] == "info":
            return _completed(
                '"/var/lib/docker"\t"overlay2"\t"29.2.0"\t"linux"\t"docker-desktop"\n'
            )
        if command[0] == "findmnt":
            return _completed("ext4 /dev/sdd /\n")
        raise AssertionError(command)

    monkeypatch.setattr(terminal_host, "_run", fake_run)

    identity = terminal_host.inspect_terminal_host(tmp_path)

    assert identity["ready"] is False
    assert "docker_binary_native" in identity["errors"]
    assert "docker_context_default" in identity["errors"]
    assert "docker_desktop_absent" in identity["errors"]
