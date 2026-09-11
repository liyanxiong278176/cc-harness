"""Small subprocess and filesystem helpers shared by guards."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping


def run_command(
    command: list[str],
    *,
    timeout: float = 10.0,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run a bounded command and return stdout/stderr as evidence."""

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=dict(env) if env is not None else None,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "returncode": None,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout[-8_000:],
        "stderr": completed.stderr[-8_000:],
        "error": None,
    }


def command_available(name: str) -> bool:
    """Return whether an executable is discoverable without invoking it."""

    return shutil.which(name) is not None


def write_probe(root: Path) -> tuple[bool, str | None]:
    """Verify atomic creation/removal of a small file in *root*."""

    try:
        root.mkdir(parents=True, exist_ok=True)
        path = root / ".cc-harness-infra-probe"
        path.write_text("ok\n", encoding="utf-8")
        path.replace(root / ".cc-harness-infra-probe.done")
        (root / ".cc-harness-infra-probe.done").unlink(missing_ok=True)
        return True, None
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def env_flag(name: str, default: bool = False) -> bool:
    """Read a conventional opt-in boolean from the environment."""

    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}
