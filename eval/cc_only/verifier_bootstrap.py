"""Offline verifier dependencies for Terminal-Bench containers.

Some Terminal-Bench tasks ship a ``tests/test.sh`` that bootstraps uv from
``astral.sh`` before invoking the actual pytest verifier.  That bootstrap is
outside the task's scoring logic and is not deterministic on a restricted
network.  This module prepares a small, content-addressed wheelhouse for the
verifier's Python-only dependencies so the official test module can run
without reaching Debian or Astral during every trial.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path

VERIFIER_BOOTSTRAP_VERSION = "pytest-8.4.1-ctrf-0.3.5-closure4"
VERIFIER_BOOTSTRAP_FILENAME = (
    f"terminal-verifier-{VERIFIER_BOOTSTRAP_VERSION}-py3.tar.gz"
)
VERIFIER_BOOTSTRAP_MAX_BYTES = 16 * 1024 * 1024
VERIFIER_BOOTSTRAP_ATTEMPTS = 3
VERIFIER_BOOTSTRAP_PACKAGES: tuple[str, ...] = (
    "pytest==8.4.1",
    "pytest-json-ctrf==0.3.5",
    "colorama==0.4.6",
    # 2.3.0 requires Python >=3.10, while Terminal-Bench includes Alpine
    # images whose system Python is 3.9.  2.1.0 is the compatible release and
    # satisfies pytest's iniconfig dependency without changing the verifier
    # entry point or scoring contract.
    "iniconfig==2.1.0",
    "packaging==26.3",
    "pluggy==1.6.0",
    "pygments==2.21.0",
    "exceptiongroup==1.3.1",
    "typing_extensions==4.15.0",
    "tomli==2.0.2",
)
VERIFIER_REQUIRED_IMPORTS: tuple[str, ...] = (
    "pytest",
    "ctrf",
    "exceptiongroup",
    "typing_extensions",
    "tomli",
)
VERIFIER_BOOTSTRAP_WHEELS: tuple[str, ...] = (
    "pytest-8.4.1-py3-none-any.whl",
    "pytest_json_ctrf-0.3.5-py3-none-any.whl",
    "colorama-0.4.6-py2.py3-none-any.whl",
    "iniconfig-2.1.0-py3-none-any.whl",
    "packaging-26.3-py3-none-any.whl",
    "pluggy-1.6.0-py3-none-any.whl",
    "pygments-2.21.0-py3-none-any.whl",
    "exceptiongroup-1.3.1-py3-none-any.whl",
    "typing_extensions-4.15.0-py3-none-any.whl",
    "tomli-2.0.2-py3-none-any.whl",
)
_MANIFEST_NAME = "manifest.json"
_WHEELHOUSE_DIR = "wheelhouse"
_HELPER_DIR = "bin"

_CURL_HELPER = """#!/bin/sh
set -eu
for argument in "$@"; do
    case "$argument" in
        *astral.sh/uv/*/install.sh)
            printf '%s\\n' '#!/bin/sh' 'exit 0'
            exit 0
            ;;
    esac
done
exec /usr/bin/curl "$@"
"""

_UVX_HELPER = r'''#!/usr/bin/env python3
"""Run the official verifier's pytest entry point without network access."""

from __future__ import annotations

import os
import sys


def main() -> None:
    arguments = sys.argv[1:]
    if os.environ.get("CC_HARNESS_TERMINAL_VERIFIER_BOOTSTRAP") != "1":
        os.execv("/root/.local/bin/uvx", ["/root/.local/bin/uvx", *arguments])
    pytest_index = None
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value in {"--with", "--from", "--python"}:
            index += 2
            continue
        if value.startswith(("--with=", "--from=", "--python=")):
            index += 1
            continue
        if value in {"--isolated", "--no-project", "--exact"}:
            index += 1
            continue
        if value in {"pytest", "py.test"}:
            pytest_index = index
            break
        index += 1
    if pytest_index is None:
        os.execv("/root/.local/bin/uvx", ["/root/.local/bin/uvx", *arguments])
    os.execv(sys.executable, [sys.executable, "-m", "pytest", *arguments[pytest_index + 1 :]])


if __name__ == "__main__":
    main()
'''

_ENV_HELPER = """# Installed by the frozen Terminal-Bench verifier bootstrap.
export CC_HARNESS_TERMINAL_VERIFIER_BOOTSTRAP=1
export PATH=\"/opt/cc-harness/terminal-verifier/bin:$HOME/.local/bin:$PATH\"
export PYTHONPATH=\"/opt/cc-harness/terminal-verifier/site-packages${PYTHONPATH:+:$PYTHONPATH}\"
"""

_WHEEL_INSTALLER = r'''#!/usr/bin/env python3
"""Install the frozen pure-Python wheelhouse without requiring pip."""

from __future__ import annotations

import argparse
import shutil
import zipfile
from pathlib import Path, PurePosixPath


def _safe_destination(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise RuntimeError(f"unsafe wheel member: {name!r}")
    destination = root.joinpath(*relative.parts).resolve()
    if destination != root and root not in destination.parents:
        raise RuntimeError(f"wheel member escapes target: {name!r}")
    return destination


def _copy_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo, destination: Path) -> None:
    if member.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(member) as source, destination.open("wb") as target:
        shutil.copyfileobj(source, target)


def _install_wheel(path: Path, target: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            name = member.filename
            if name.endswith("/"):
                _copy_member(archive, member, _safe_destination(target, name))
                continue
            destination_name = name
            if ".data/" in name:
                _, category, relative = name.split(".data/", 1)[1].split("/", 1)
                if category not in {"purelib", "platlib"}:
                    # These wheels are pure Python; scripts/data are not needed
                    # because the official uvx wrapper invokes pytest as a module.
                    continue
                destination_name = relative
            _copy_member(archive, member, _safe_destination(target, destination_name))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    wheelhouse = args.wheelhouse.resolve()
    target = args.target.resolve()
    if not wheelhouse.is_dir():
        raise SystemExit(f"wheelhouse does not exist: {wheelhouse}")
    target.mkdir(parents=True, exist_ok=True)
    wheels = sorted(wheelhouse.glob("*.whl"))
    if not wheels:
        raise SystemExit(f"wheelhouse has no wheels: {wheelhouse}")
    for wheel in wheels:
        _install_wheel(wheel, target)


if __name__ == "__main__":
    main()
'''

Progress = Callable[[str], None]


def verifier_bootstrap_cache_path(project_root: Path) -> Path:
    return (
        project_root
        / "eval"
        / "cache"
        / "terminal-bench"
        / VERIFIER_BOOTSTRAP_FILENAME
    )


def verifier_bootstrap_identity(path: Path) -> dict[str, object]:
    if not _valid_archive(path):
        raise ValueError("verifier bootstrap archive failed integrity validation")
    manifest = _read_manifest(path)
    return {
        "version": VERIFIER_BOOTSTRAP_VERSION,
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
        "packages": list(manifest.get("packages") or ()),
        "required_imports": list(manifest.get("required_imports") or ()),
    }


def ensure_verifier_bootstrap(
    project_root: Path,
    *,
    progress: Progress | None = None,
) -> Path:
    """Prepare and verify the local verifier wheelhouse archive.

    The download happens once on the host.  Trial containers receive the
    resulting archive and install from it with ``pip --no-index``.  The
    archive is intentionally small (pytest plus its pure-Python dependencies)
    and does not include the scientific packages already present in the task
    image.
    """

    target = verifier_bootstrap_cache_path(project_root.resolve())
    if _valid_archive(target):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(1, VERIFIER_BOOTSTRAP_ATTEMPTS + 1):
        staging_root = Path(tempfile.mkdtemp(prefix="terminal-verifier-", dir=target.parent))
        temporary = target.with_suffix(target.suffix + ".part")
        try:
            wheelhouse = staging_root / _WHEELHOUSE_DIR
            wheelhouse.mkdir()
            _download_wheels(wheelhouse)
            _write_helpers(staging_root)
            _write_manifest(staging_root, wheelhouse)
            if temporary.exists():
                temporary.unlink()
            _write_archive(staging_root, temporary)
            if not _valid_archive(temporary):
                raise ValueError("verifier bootstrap archive failed integrity validation")
            os.replace(temporary, target)
            if progress is not None:
                progress(f"prepared offline Terminal-Bench verifier {VERIFIER_BOOTSTRAP_VERSION}")
            return target
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
            last_error = exc
            if temporary.exists():
                temporary.unlink()
            if progress is not None:
                progress(
                    "verifier bootstrap attempt "
                    f"{attempt}/{VERIFIER_BOOTSTRAP_ATTEMPTS} failed: {exc}"
                )
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
    raise RuntimeError(
        "unable to prepare the offline Terminal-Bench verifier wheelhouse; "
        "fix host package access before starting Terminal-Bench"
    ) from last_error


def _download_wheels(destination: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "--no-deps",
        "--dest",
        str(destination),
        *VERIFIER_BOOTSTRAP_PACKAGES,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-2_000:]
        raise RuntimeError(f"pip download failed: {detail}")
    expected = set(VERIFIER_BOOTSTRAP_WHEELS)
    actual = {path.name for path in destination.glob("*.whl")}
    if actual != expected:
        raise ValueError(f"verifier wheelhouse mismatch: expected {sorted(expected)}, got {sorted(actual)}")


def _write_manifest(root: Path, wheelhouse: Path) -> None:
    entries = [
        {
            "name": path.name,
            "sha256": _sha256(path),
            "size_bytes": path.stat().st_size,
        }
        for path in sorted(wheelhouse.glob("*.whl"), key=lambda item: item.name)
    ]
    payload = {
        "schema_version": "terminal-bench.verifier-bootstrap.v1",
        "version": VERIFIER_BOOTSTRAP_VERSION,
        "packages": list(VERIFIER_BOOTSTRAP_PACKAGES),
        "required_imports": list(VERIFIER_REQUIRED_IMPORTS),
        "wheels": entries,
    }
    (root / _MANIFEST_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_helpers(root: Path) -> None:
    helper_root = root / _HELPER_DIR
    helper_root.mkdir(parents=True, exist_ok=True)
    (helper_root / "curl").write_text(_CURL_HELPER, encoding="utf-8", newline="\n")
    (helper_root / "uvx").write_text(_UVX_HELPER, encoding="utf-8", newline="\n")
    (helper_root / "install-wheelhouse.py").write_text(
        _WHEEL_INSTALLER, encoding="utf-8", newline="\n"
    )
    (root / "env").write_text(_ENV_HELPER, encoding="utf-8", newline="\n")


def _write_archive(source_root: Path, target: Path) -> None:
    with tarfile.open(target, mode="w:gz") as archive:
        for path in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
            archive.add(path, arcname=path.relative_to(source_root).as_posix(), recursive=False)


def _read_manifest(path: Path) -> dict[str, object]:
    if not path.is_file() or path.stat().st_size > VERIFIER_BOOTSTRAP_MAX_BYTES:
        raise ValueError("verifier bootstrap archive is missing or too large")
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            member = archive.extractfile(_MANIFEST_NAME)
            if member is None:
                raise ValueError("verifier bootstrap manifest is missing")
            payload = json.loads(member.read().decode("utf-8"))
    except (OSError, tarfile.TarError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid verifier bootstrap archive: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("verifier bootstrap manifest must be an object")
    if payload.get("version") != VERIFIER_BOOTSTRAP_VERSION:
        raise ValueError("verifier bootstrap version is stale")
    return payload


def _valid_archive(path: Path) -> bool:
    try:
        manifest = _read_manifest(path)
        wheel_entries = manifest.get("wheels")
        if not isinstance(wheel_entries, list):
            return False
        expected = {
            str(item.get("name"))
            for item in wheel_entries
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        if len(expected) != len(wheel_entries):
            return False
        if tuple(manifest.get("packages") or ()) != VERIFIER_BOOTSTRAP_PACKAGES:
            return False
        if tuple(manifest.get("required_imports") or ()) != VERIFIER_REQUIRED_IMPORTS:
            return False
        with tarfile.open(path, mode="r:gz") as archive:
            names = set(archive.getnames())
            helper_names = {
                f"{_HELPER_DIR}/curl",
                f"{_HELPER_DIR}/uvx",
                f"{_HELPER_DIR}/install-wheelhouse.py",
                "env",
            }
            wheel_names = {
                name.removeprefix(f"{_WHEELHOUSE_DIR}/")
                for name in names
                if name.startswith(f"{_WHEELHOUSE_DIR}/")
            }
            if wheel_names != expected or not helper_names.issubset(names):
                return False
            for item in wheel_entries:
                if not isinstance(item, dict):
                    return False
                name = str(item["name"])
                member = archive.extractfile(f"{_WHEELHOUSE_DIR}/{name}")
                if member is None:
                    return False
                content = member.read()
                if item.get("size_bytes") != len(content):
                    return False
                if item.get("sha256") != hashlib.sha256(content).hexdigest():
                    return False
        return True
    except (OSError, ValueError, tarfile.TarError):
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "VERIFIER_BOOTSTRAP_FILENAME",
    "VERIFIER_BOOTSTRAP_PACKAGES",
    "VERIFIER_BOOTSTRAP_WHEELS",
    "VERIFIER_BOOTSTRAP_VERSION",
    "VERIFIER_REQUIRED_IMPORTS",
    "ensure_verifier_bootstrap",
    "verifier_bootstrap_cache_path",
    "verifier_bootstrap_identity",
]
