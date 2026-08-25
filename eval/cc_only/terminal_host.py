"""Host contract for Terminal-Bench running on WSL2 native Docker."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

TERMINAL_EXECUTION_BACKEND = "wsl2-ubuntu-native-docker.v1"
EXPECTED_DISTRIBUTION = "Ubuntu"
EXPECTED_PROJECT_ROOT = Path("/mnt/d/agent_learning/cc-harness")
EXPECTED_DOCKER_ROOT = Path("/var/lib/docker")
_NATIVE_FILESYSTEMS = {"ext2", "ext3", "ext4", "xfs", "btrfs"}


def inspect_terminal_host(project_root: Path) -> dict[str, Any]:
    """Return an auditable, non-mutating WSL/native-Docker readiness record."""

    resolved_project = project_root.resolve()
    release = _read_text(Path("/proc/sys/kernel/osrelease"))
    distro = os.environ.get("WSL_DISTRO_NAME") or ""
    docker = shutil.which("docker")
    context = _run_text([docker, "context", "show"]) if docker else None
    info = _docker_info(docker)
    docker_root = Path(str(info.get("docker_root") or ""))
    # The readiness contract is evaluated in WSL, but its unit tests also run
    # on Windows.  A POSIX Docker root such as /var/lib/docker is not
    # considered absolute by WindowsPath, so use the serialized value here.
    mount = (
        _docker_mount(docker_root)
        if str(docker_root).startswith(("/", "\\"))
        else {}
    )
    docker_host = os.environ.get("DOCKER_HOST") or ""
    checks = {
        "linux": platform.system() == "Linux",
        "wsl2": "microsoft" in release.casefold(),
        "distribution": distro == EXPECTED_DISTRIBUTION,
        "project_on_mnt_d": (
            resolved_project == EXPECTED_PROJECT_ROOT
            or EXPECTED_PROJECT_ROOT in resolved_project.parents
        ),
        "docker_binary_native": bool(
            docker
            and Path(docker).as_posix() in {"/usr/bin/docker", "/usr/local/bin/docker"}
        ),
        "docker_context_default": context == "default",
        "docker_socket_native": docker_host in {"", "unix:///var/run/docker.sock"},
        "docker_daemon_ready": bool(info.get("ready")),
        "docker_server_linux": str(info.get("server_os") or "").casefold() == "linux",
        "docker_root_native": (
            docker_root == EXPECTED_DOCKER_ROOT
            or EXPECTED_DOCKER_ROOT in docker_root.parents
        ),
        "docker_storage_ext": str(mount.get("filesystem") or "") in _NATIVE_FILESYSTEMS,
        "docker_desktop_absent": not any(
            marker in " ".join(
                (
                    str(context or ""),
                    str(docker or ""),
                    str(info.get("name") or ""),
                    docker_host,
                )
            ).casefold()
            for marker in ("desktop-linux", "dockerdesktop", "/mnt/d/docker")
        ),
    }
    errors = [name for name, ready in checks.items() if not ready]
    return {
        "schema_version": TERMINAL_EXECUTION_BACKEND,
        "ready": not errors,
        "errors": errors,
        "checks": checks,
        "distribution": distro,
        "kernel_release": release,
        "project_root": resolved_project.as_posix(),
        "docker_binary": docker,
        "docker_context": context,
        "docker_host": docker_host or None,
        "docker_root": (
            docker_root.as_posix()
            if str(docker_root).startswith(("/", "\\"))
            else None
        ),
        "docker_storage": mount,
        "docker_server_version": info.get("server_version"),
        "docker_server_os": info.get("server_os"),
        "docker_name": info.get("name"),
    }


def require_terminal_host(project_root: Path) -> dict[str, Any]:
    identity = inspect_terminal_host(project_root)
    if not identity["ready"]:
        raise RuntimeError(
            "Terminal-Bench requires Ubuntu WSL2 native Docker; failed checks: "
            + ", ".join(identity["errors"])
        )
    return identity


def _docker_info(docker: str | None) -> dict[str, Any]:
    if not docker:
        return {"ready": False}
    completed = _run(
        [
            docker,
            "info",
            "--format",
            (
                "{{json .DockerRootDir}}\t{{json .Driver}}\t{{json .ServerVersion}}\t"
                "{{json .OSType}}\t{{json .Name}}"
            ),
        ],
        timeout=15,
    )
    if completed is None or completed.returncode != 0:
        return {
            "ready": False,
            "error": (
                completed.stderr.strip()[-2_000:]
                if completed is not None
                else "docker info timed out or could not start"
            ),
        }
    values = completed.stdout.strip().split("\t")
    if len(values) != 5:
        return {"ready": False, "error": "docker info returned an unexpected identity"}
    try:
        docker_root, driver, server_version, server_os, name = (
            json.loads(value) for value in values
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"ready": False, "error": "docker info identity is not valid JSON"}
    return {
        "ready": True,
        "docker_root": docker_root,
        "driver": driver,
        "server_version": server_version,
        "server_os": server_os,
        "name": name,
    }


def _docker_mount(path: Path) -> dict[str, Any]:
    completed = _run(
        ["findmnt", "-T", str(path), "-n", "-o", "FSTYPE,SOURCE,TARGET"],
        timeout=10,
    )
    if completed is None or completed.returncode != 0:
        return {}
    fields = completed.stdout.strip().split(maxsplit=2)
    if len(fields) != 3:
        return {}
    return {"filesystem": fields[0], "source": fields[1], "target": fields[2]}


def _run_text(command: Sequence[str]) -> str | None:
    completed = _run(command, timeout=10)
    if completed is None or completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _run(command: Sequence[str], *, timeout: int) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
