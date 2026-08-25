"""Offline Python runtime and task-container overlay for Terminal-Bench.

Official Terminal-Bench verifier scripts may bootstrap uv with apt-get/curl.
Those bootstrap operations are infrastructure, not task scoring.  This module
mounts a frozen Python runtime and narrowly scoped command wrappers while
leaving the official test script unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sysconfig
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .verifier_bootstrap import (
    VERIFIER_BOOTSTRAP_VERSION,
    ensure_verifier_bootstrap,
    verifier_bootstrap_cache_path,
)

VERIFIER_RUNTIME_VERSION = "python3.12-offline-v9"
VERIFIER_RUNTIME_DIRNAME = "verifier-runtime-python312"
VERIFIER_RUNTIME_SCHEMA = "terminal-bench.verifier-runtime.v1"
VERIFIER_OVERLAY_SCHEMA = "terminal-bench.verifier-overlay.v1"
AGENT_OVERLAY_SCHEMA = "terminal-bench.agent-runtime-overlay.v1"
_REQUIRED_IMPORTS = ("pytest", "ctrf", "exceptiongroup", "typing_extensions", "tomli")
_RUNTIME_LIBRARIES = (
    "libexpat.so.1",
    "libsqlite3.so.0",
    "libdl.so.2",
    "libpthread.so.0",
    "librt.so.1",
    "libutil.so.1",
    "libresolv.so.2",
    "libgcc_s.so.1",
    "libstdc++.so.6",
)
_RUNTIME_UV_PYTHON = "python-uv"
_RUNTIME_LOADER_TARGET = "/tmp/cc-harness-ld.so"
_AGENT_SITE_DIRNAME = "agent-site-packages"
Progress = Callable[[str], None]


def verifier_runtime_cache_path(project_root: Path) -> Path:
    return (
        project_root.resolve()
        / "eval"
        / "cache"
        / "terminal-bench"
        / VERIFIER_RUNTIME_DIRNAME
    )


def agent_site_packages_cache_path(project_root: Path) -> Path:
    return verifier_runtime_cache_path(project_root).parent / _AGENT_SITE_DIRNAME


def _ensure_agent_site_packages(project_root: Path) -> Path:
    """Materialize the already-frozen project environment for offline agent runs."""

    target = agent_site_packages_cache_path(project_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_dir():
        # Terminal-Bench runs through ``uv run`` with UV_PROJECT_ENVIRONMENT
        # outside the Windows-mounted repository. Resolve the active
        # interpreter's purelib instead of assuming a project-local venv.
        source = Path(sysconfig.get_path("purelib")).resolve()
        if not source.is_dir():
            raise RuntimeError(f"offline agent environment is missing: {source}")
        shutil.copytree(source, target, symlinks=False)
    for path in target.rglob("*.pth"):
        path.unlink(missing_ok=True)
    package = project_root.resolve() / "cc_harness"
    if not package.is_dir():
        raise RuntimeError(f"cc-harness source package is missing: {package}")
    shutil.copytree(package, target / "cc_harness", dirs_exist_ok=True, symlinks=False)
    return target


def verifier_runtime_identity(path: Path) -> dict[str, Any]:
    root = path.resolve()
    python = root / "python" / "bin" / "python"
    uv_python = root / "python" / "bin" / _RUNTIME_UV_PYTHON
    if not root.is_dir() or not python.is_file():
        raise ValueError(f"offline verifier runtime is missing: {root}")
    payload: dict[str, Any] = {
        "schema_version": VERIFIER_RUNTIME_SCHEMA,
        "version": VERIFIER_RUNTIME_VERSION,
        "verifier_bootstrap_version": VERIFIER_BOOTSTRAP_VERSION,
        "python_sha256": _sha256(python),
        "python_size_bytes": python.stat().st_size,
        "uv_python_sha256": _sha256(uv_python),
        "uv_python_size_bytes": uv_python.stat().st_size,
        "required_imports": list(_REQUIRED_IMPORTS),
        "libraries": [
            {
                "name": name,
                "sha256": _sha256(root / "lib" / name),
                "size_bytes": (root / "lib" / name).stat().st_size,
            }
            for name in _RUNTIME_LIBRARIES
        ],
    }
    manifest = root / "runtime.json"
    if manifest.is_file():
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                for key in ("python_version", "stdlib_source", "bundled_runtime_libraries"):
                    if key in value:
                        payload[key] = value[key]
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    return payload


def ensure_verifier_runtime(
    project_root: Path,
    *,
    bootstrap_path: Path | None = None,
    progress: Progress | None = None,
) -> Path:
    """Create a self-contained Python 3.12 runtime from frozen wheels."""

    target = verifier_runtime_cache_path(project_root)
    _ensure_agent_site_packages(project_root)
    bootstrap = (bootstrap_path or verifier_bootstrap_cache_path(project_root)).resolve()
    # Helper launchers are project code, not part of the expensive frozen
    # Python payload. Refresh them in place before deciding that the runtime
    # cache is stale; renaming a populated cache on a Windows-mounted WSL path
    # can fail even though the immutable Python and library closure is valid.
    if target.is_dir():
        helpers = target / "helpers"
        helpers.mkdir(exist_ok=True)
        _write_helpers(helpers)
    if _runtime_ready(target):
        return target
    python312 = shutil.which("python3.12") or "/usr/bin/python3.12"
    if not Path(python312).is_file():
        raise RuntimeError("offline verifier runtime requires WSL python3.12")
    if not bootstrap.is_file():
        bootstrap = ensure_verifier_bootstrap(project_root, progress=progress)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{VERIFIER_RUNTIME_DIRNAME}-", dir=target.parent))
    try:
        runtime_python = staging / "python"
        _run([python312, "-m", "venv", "--copies", str(runtime_python)])
        _patch_elf_interpreter(
            runtime_python / "bin" / "python",
            runtime_python / "bin" / _RUNTIME_UV_PYTHON,
            _RUNTIME_LOADER_TARGET,
        )
        runtime_lib = staging / "lib"
        runtime_lib.mkdir(parents=True, exist_ok=True)
        for name in _RUNTIME_LIBRARIES:
            shutil.copy2(_system_library(name), runtime_lib / name)
        stdlib = _stdlib_path(python312)
        target_stdlib = runtime_python / "lib" / "python3.12"
        shutil.rmtree(target_stdlib)
        shutil.copytree(stdlib, target_stdlib, symlinks=True)
        (runtime_python / "pyvenv.cfg").unlink(missing_ok=True)
        extracted = staging / "bootstrap"
        extracted.mkdir()
        _run(["tar", "-xzf", str(bootstrap), "-C", str(extracted)])
        site_packages = target_stdlib / "site-packages"
        site_packages.mkdir(parents=True, exist_ok=True)
        _run(
            [
                str(runtime_python / "bin" / "python"),
                str(extracted / "bin" / "install-wheelhouse.py"),
                "--wheelhouse",
                str(extracted / "wheelhouse"),
                "--target",
                str(site_packages),
            ]
        )
        # The agent site is mounted read-only and its manylinux wheels carry
        # their own $ORIGIN libraries.  Only the verifier runtime's ELF
        # closure is bundled here; scanning every third-party extension on a
        # Windows-mounted tree would turn preflight into an unbounded I/O job.
        bundled_libraries = _bundle_runtime_dependencies(runtime_python, runtime_lib)
        helpers = staging / "helpers"
        helpers.mkdir()
        _write_helpers(helpers)
        env = os.environ.copy()
        env.update(
            {
                "PYTHONHOME": str(runtime_python),
                "PYTHONPATH": str(site_packages),
                "LD_LIBRARY_PATH": f"{runtime_lib}:{env.get('LD_LIBRARY_PATH', '')}",
            }
        )
        version = _run(
            [
                str(runtime_python / "bin" / "python"),
                "-c",
                "import platform; print(platform.python_version())",
            ],
            env=env,
            capture_output=True,
        ).stdout.strip()
        (staging / "runtime.json").write_text(
            json.dumps(
                {
                    "schema_version": VERIFIER_RUNTIME_SCHEMA,
                    "version": VERIFIER_RUNTIME_VERSION,
                    "verifier_bootstrap_version": VERIFIER_BOOTSTRAP_VERSION,
                    "python_version": version,
                    "python_sha256": _sha256(runtime_python / "bin" / "python"),
                    "stdlib_source": stdlib,
                    "libraries": [
                        {
                            "name": name,
                            "sha256": _sha256(runtime_lib / name),
                            "size_bytes": (runtime_lib / name).stat().st_size,
                        }
                        for name in _RUNTIME_LIBRARIES
                    ],
                    "bundled_runtime_libraries": bundled_libraries,
                    "required_imports": list(_REQUIRED_IMPORTS),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        _verify_runtime(staging)
        old = target.with_name(target.name + ".old")
        if old.exists():
            shutil.rmtree(old)
        if target.exists():
            target.replace(old)
        staging.replace(target)
        if old.exists():
            shutil.rmtree(old)
        if progress is not None:
            progress(f"prepared offline verifier runtime {version}")
        return target
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verifier_runtime_overlay(
    project_root: Path,
    output_root: Path,
    *,
    runtime_path: Path | None = None,
) -> Path:
    """Write the Compose overlay used by both prewarm and formal trials."""

    runtime = (runtime_path or verifier_runtime_cache_path(project_root)).resolve()
    # Keep generated helpers in sync when the source overlay logic changes.  The
    # runtime directory is cached for expensive Python/wheel work, but the
    # wrappers are project code and must be refreshed on every overlay build.
    _write_helpers(runtime / "helpers")
    agent_site = _ensure_agent_site_packages(project_root)
    _verify_runtime(runtime)
    output = output_root.resolve() / "verifier-runtime-compose.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    mount_root = "/opt/cc-harness/verifier-runtime"
    payload = {
        "x-cc-harness": {
            "schema_version": VERIFIER_OVERLAY_SCHEMA,
            "version": VERIFIER_RUNTIME_VERSION,
        },
        "services": {
            "main": {
                "environment": {
                    "CC_HARNESS_TERMINAL_VERIFIER_BOOTSTRAP": "1",
                    "CC_HARNESS_TERMINAL_VERIFIER_RUNTIME": "1",
                    "PATH": (
                        "/opt/cc-harness/verifier-offline-bin:"
                        "/root/.local/bin:"
                        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
                    ),
                },
                "volumes": [
                    f"{(runtime / 'python').as_posix()}:{mount_root}/python:ro",
                    f"{(runtime / 'lib').as_posix()}:{mount_root}/lib:ro",
                    f"{(runtime / 'lib' / 'ld-linux-x86-64.so.2').as_posix()}:{_RUNTIME_LOADER_TARGET}:ro",
                    f"{(runtime / 'lib').as_posix()}:/tmp/cc-lib:ro",
                    f"{(runtime / 'helpers').as_posix()}:/opt/cc-harness/verifier-offline-bin:ro",
                    f"{agent_site.as_posix()}:/opt/cc-harness/agent-site:ro",
                    f"{(runtime / 'helpers' / 'env').as_posix()}:/root/.local/bin/env:ro",
                ],
            }
        },
    }
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output


def agent_runtime_overlay(
    project_root: Path,
    output_root: Path,
    *,
    runtime_path: Path | None = None,
) -> Path:
    """Mount only the custom agent runtime, leaving the official verifier untouched.

    Harbor explicitly supports installed custom agents. Some official task
    images do not contain a sufficiently new Python, so cc-harness needs a
    private runtime mount. Unlike :func:`verifier_runtime_overlay`, this
    overlay does not change PATH and does not mount replacements for apt-get,
    curl, uvx, Python, or the official ``/tests/test.sh``.
    """

    runtime = (runtime_path or verifier_runtime_cache_path(project_root)).resolve()
    _write_helpers(runtime / "helpers")
    agent_site = _ensure_agent_site_packages(project_root)
    _verify_runtime(runtime)
    output = output_root.resolve() / "agent-runtime-compose.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    mount_root = "/opt/cc-harness/agent-runtime"
    payload = {
        "x-cc-harness": {
            "schema_version": AGENT_OVERLAY_SCHEMA,
            "version": VERIFIER_RUNTIME_VERSION,
            "scope": "custom-agent-only",
            "official_verifier_modified": False,
        },
        "services": {
            "main": {
                "environment": {"CC_HARNESS_TERMINAL_AGENT_RUNTIME": "1"},
                "volumes": [
                    f"{(runtime / 'python').as_posix()}:{mount_root}/python:ro",
                    f"{(runtime / 'lib').as_posix()}:{mount_root}/lib:ro",
                    f"{agent_site.as_posix()}:/opt/cc-harness/agent-site:ro",
                    (
                        f"{(runtime / 'helpers' / 'cc-harness-agent').as_posix()}:"
                        "/root/.local/bin/cc-harness:ro"
                    ),
                ],
            }
        },
    }
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output


def _runtime_ready(root: Path) -> bool:
    try:
        _verify_runtime(root)
        return True
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return False


def _verify_runtime(root: Path) -> None:
    python = root / "python" / "bin" / "python"
    site = root / "python" / "lib" / "python3.12" / "site-packages"
    helpers = root / "helpers"
    required = (
        python,
        root / "python" / "bin" / _RUNTIME_UV_PYTHON,
        site,
        *(root / "lib" / name for name in _RUNTIME_LIBRARIES),
        helpers / "apt-get",
        helpers / "curl",
        helpers / "python3",
        helpers / "uvx",
        helpers / "cc-harness",
        helpers / "cc-harness-agent",
        helpers / "env",
        root / "lib" / "ld-linux-x86-64.so.2",
    )
    if not all(path.exists() for path in required):
        raise ValueError("offline verifier runtime is incomplete")
    manifest = root / "runtime.json"
    try:
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("offline verifier runtime metadata is missing") from exc
    if not isinstance(metadata, dict) or metadata.get("version") != VERIFIER_RUNTIME_VERSION:
        raise ValueError("offline verifier runtime version is stale")
    env = os.environ.copy()
    env.update(
        {
            "PYTHONHOME": str(root / "python"),
            "PYTHONPATH": str(site),
            "LD_LIBRARY_PATH": f"{root / 'lib'}:{env.get('LD_LIBRARY_PATH', '')}",
        }
    )
    _run(
        [
            str(root / "lib" / "ld-linux-x86-64.so.2"),
            "--library-path",
            str(root / "lib"),
            str(python),
            "-c",
            "import pytest, ctrf, exceptiongroup, typing_extensions, tomli",
        ],
        env=env,
    )


def _bundle_runtime_dependencies(
    runtime_python: Path,
    runtime_lib: Path,
    *,
    extra_roots: tuple[Path, ...] = (),
) -> list[str]:
    """Bundle the loader and ELF dependencies needed on older task images.

    The host WSL Python is commonly linked against a newer glibc than the
    benchmark's Debian/Ubuntu images.  Invoking it through its bundled loader
    keeps the fallback usable without altering the task image or relying on
    network package installation.
    """

    pending = [runtime_python / "bin" / "python"]
    pending.extend(runtime_python.rglob("*.so"))
    for root in extra_roots:
        pending.extend(root.rglob("*.so"))
    seen: set[Path] = set()
    bundled: set[str] = set()
    while pending:
        binary = pending.pop()
        try:
            resolved = binary.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        completed = subprocess.run(
            ["ldd", str(resolved)],
            check=False,
            capture_output=True,
            text=True,
        )
        for line in completed.stdout.splitlines():
            match = re.search(r"=>\s+(\S+)", line)
            candidate = match.group(1) if match else line.strip().split(" ", 1)[0]
            if not candidate.startswith("/"):
                continue
            source = Path(candidate)
            if not source.is_file():
                continue
            target = runtime_lib / source.name
            if not target.exists():
                shutil.copy2(source, target)
            bundled.add(source.name)
            if source not in seen:
                pending.append(source)
    loader = Path("/lib64/ld-linux-x86-64.so.2")
    if loader.is_file():
        shutil.copy2(loader, runtime_lib / loader.name)
        bundled.add(loader.name)
    return sorted(bundled)


def _patch_elf_interpreter(source: Path, target: Path, interpreter: str) -> None:
    """Copy an ELF Python binary with a short, overlay-mounted interpreter path."""

    data = bytearray(source.read_bytes())
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise RuntimeError(f"unsupported ELF binary for runtime shim: {source}")
    phoff = struct.unpack_from("<Q", data, 32)[0]
    phentsize = struct.unpack_from("<H", data, 54)[0]
    phnum = struct.unpack_from("<H", data, 56)[0]
    for index in range(phnum):
        offset = phoff + index * phentsize
        p_type = struct.unpack_from("<I", data, offset)[0]
        if p_type != 3:  # PT_INTERP
            continue
        p_offset = struct.unpack_from("<Q", data, offset + 8)[0]
        p_filesz = struct.unpack_from("<Q", data, offset + 32)[0]
        encoded = interpreter.encode("ascii") + b"\0"
        if len(encoded) > p_filesz:
            raise RuntimeError(
                f"runtime interpreter path is too long for {source}: {interpreter}"
            )
        data[p_offset : p_offset + p_filesz] = encoded.ljust(p_filesz, b"\0")
        _patch_elf_runpath(data, phoff, phentsize, phnum)
        target.write_bytes(data)
        shutil.copymode(source, target)
        return
    raise RuntimeError(f"ELF interpreter segment missing: {source}")


def _patch_elf_runpath(
    data: bytearray, phoff: int, phentsize: int, phnum: int
) -> None:
    """Reuse the optional DT_DEBUG slot to add a short bundled-library path."""

    loads: list[tuple[int, int, int, int]] = []
    dynamic: tuple[int, int] | None = None
    for index in range(phnum):
        offset = phoff + index * phentsize
        p_type = struct.unpack_from("<I", data, offset)[0]
        p_offset = struct.unpack_from("<Q", data, offset + 8)[0]
        p_vaddr = struct.unpack_from("<Q", data, offset + 16)[0]
        p_filesz = struct.unpack_from("<Q", data, offset + 32)[0]
        if p_type == 1:
            p_memsz = struct.unpack_from("<Q", data, offset + 40)[0]
            loads.append((p_offset, p_vaddr, p_filesz, p_memsz))
        elif p_type == 2:
            dynamic = (p_offset, p_filesz)
    if dynamic is None:
        raise RuntimeError("ELF dynamic segment missing")

    def vaddr_to_file(address: int) -> int:
        for file_offset, virtual, file_size, _ in loads:
            if virtual <= address < virtual + file_size:
                return file_offset + (address - virtual)
        raise RuntimeError(f"ELF virtual address is not file-backed: {address:#x}")

    dynamic_offset, dynamic_size = dynamic
    strtab_vaddr: int | None = None
    strtab_size: int | None = None
    entries: list[int] = []
    for entry_offset in range(dynamic_offset, dynamic_offset + dynamic_size, 16):
        tag, value = struct.unpack_from("<QQ", data, entry_offset)
        entries.append(entry_offset)
        if tag == 5:  # DT_STRTAB
            strtab_vaddr = value
        elif tag == 10:  # DT_STRSZ
            strtab_size = value
    if strtab_vaddr is None or strtab_size is None:
        raise RuntimeError("ELF dynamic string table metadata missing")
    strtab_offset = vaddr_to_file(strtab_vaddr)
    replacement = b"/tmp/cc-lib\0"
    string_offset = strtab_size
    strtab_end = strtab_offset + strtab_size
    segment_end = max(
        file_offset + file_size
        for file_offset, virtual, file_size, _ in loads
        if file_offset <= strtab_offset < file_offset + file_size
    )
    if strtab_end + len(replacement) > segment_end:
        raise RuntimeError("ELF dynamic string table has no writable extension")
    data[strtab_end : strtab_end + len(replacement)] = replacement
    debug_entry = None
    for entry_offset in entries:
        tag = struct.unpack_from("<Q", data, entry_offset)[0]
        if tag == 21:  # DT_DEBUG is optional outside a debugger.
            debug_entry = entry_offset
            break
    if debug_entry is None:
        raise RuntimeError("ELF dynamic segment has no reusable DT_DEBUG slot")
    struct.pack_into("<QQ", data, debug_entry, 15, string_offset)  # DT_RPATH
    for entry_offset in entries:
        tag = struct.unpack_from("<Q", data, entry_offset)[0]
        if tag == 10:  # DT_STRSZ
            struct.pack_into("<Q", data, entry_offset + 8, strtab_size + len(replacement))
            break


def _stdlib_path(python: str) -> str:
    result = _run(
        [python, "-c", "import sysconfig; print(sysconfig.get_path('stdlib'))"],
        capture_output=True,
    )
    value = result.stdout.strip()
    if not value or not Path(value).is_dir():
        raise RuntimeError(f"python3.12 stdlib is unavailable: {value!r}")
    return value


def _write_helpers(root: Path) -> None:
    (root / "apt-get").write_text(
        """#!/bin/sh
set -e
if [ "$CC_HARNESS_TERMINAL_VERIFIER_RUNTIME" = 1 ]; then
  if [ "$1" = update ]; then exit 0; fi
  if [ "$1" = install ]; then
    shift
    packages=""
    for arg in "$@"; do
      case "$arg" in
        -*) ;;
        *) packages="$packages $arg" ;;
      esac
    done
    case "$packages" in
      " curl") exit 0 ;;
    esac
  fi
fi
exec /usr/bin/apt-get "$@"
""",
        encoding="utf-8",
        newline="\n",
    )
    (root / "curl").write_text(
        """#!/bin/sh
set -e
for arg in "$@"; do
  case "$arg" in
    *astral.sh/uv/*/install.sh)
      printf '%s\n' '#!/bin/sh' 'exit 0'
      exit 0
      ;;
  esac
done
if [ -x /usr/bin/curl ]; then exec /usr/bin/curl "$@"; fi
if [ -x /bin/curl ]; then exec /bin/curl "$@"; fi
echo 'curl is unavailable outside the frozen verifier bootstrap' >&2
exit 127
""",
        encoding="utf-8",
        newline="\n",
    )
    for name in ("python3", "python"):
        (root / name).write_text(
            """#!/bin/sh
set -e
        for candidate in /usr/bin/python3 /usr/local/bin/python3 /usr/bin/python /usr/local/bin/python; do
  if [ -x "$candidate" ] && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    unset PYTHONHOME
    exec "$candidate" "$@"
  fi
done
export PYTHONHOME=/opt/cc-harness/verifier-runtime/python
export PYTHONPATH=/opt/cc-harness/verifier-runtime/python/lib/python3.12/site-packages
export LD_LIBRARY_PATH=/opt/cc-harness/verifier-runtime/lib:${LD_LIBRARY_PATH:-}
exec /opt/cc-harness/verifier-runtime/lib/ld-linux-x86-64.so.2 \
  --library-path /opt/cc-harness/verifier-runtime/lib \
  /opt/cc-harness/verifier-runtime/python/bin/python "$@"
""",
            encoding="utf-8",
            newline="\n",
        )
    (root / "uvx").write_text(
            """#!/bin/sh
set -e
for candidate in /usr/bin/python3 /usr/local/bin/python3 /usr/bin/python /usr/local/bin/python; do
  if [ -x "$candidate" ]; then
    # uvx_impl is executed with the frozen runtime's interpreter search path;
    # the old terminal-verifier path was never mounted and made pytest appear
    # missing even though it was present in the frozen wheelhouse.
    export PYTHONPATH=/opt/cc-harness/verifier-runtime/python/lib/python3.12/site-packages:${PYTHONPATH:-}
    exec "$candidate" /opt/cc-harness/verifier-offline-bin/uvx_impl.py "$@"
  fi
done
export PYTHONHOME=/opt/cc-harness/verifier-runtime/python
export PYTHONPATH=/opt/cc-harness/verifier-runtime/python/lib/python3.12/site-packages
export LD_LIBRARY_PATH=/opt/cc-harness/verifier-runtime/lib:${LD_LIBRARY_PATH:-}
exec /opt/cc-harness/verifier-runtime/lib/ld-linux-x86-64.so.2 \
  --library-path /opt/cc-harness/verifier-runtime/lib \
  /opt/cc-harness/verifier-runtime/python/bin/python \
  /opt/cc-harness/verifier-offline-bin/uvx_impl.py "$@"
""",
        encoding="utf-8",
            newline="\n",
        )
    (root / "cc-harness").write_text(
        """#!/bin/sh
set -e
export PYTHONHOME=/opt/cc-harness/verifier-runtime/python
export PYTHONPATH=/opt/cc-harness/agent-site:/opt/cc-harness/verifier-runtime/python/lib/python3.12/site-packages
export LD_LIBRARY_PATH=/opt/cc-harness/verifier-runtime/lib:${LD_LIBRARY_PATH:-}
exec /opt/cc-harness/verifier-runtime/lib/ld-linux-x86-64.so.2 \\
  --library-path /opt/cc-harness/verifier-runtime/lib \\
  /opt/cc-harness/verifier-runtime/python/bin/python \\
  -c 'from cc_harness.entrypoint import main; raise SystemExit(main())' "$@"
""",
        encoding="utf-8",
        newline="\n",
    )
    (root / "cc-harness-agent").write_text(
        """#!/bin/sh
set -e
export PYTHONHOME=/opt/cc-harness/agent-runtime/python
export PYTHONPATH=/opt/cc-harness/agent-site:/opt/cc-harness/agent-runtime/python/lib/python3.12/site-packages
export LD_LIBRARY_PATH=/opt/cc-harness/agent-runtime/lib:${LD_LIBRARY_PATH:-}
exec /opt/cc-harness/agent-runtime/lib/ld-linux-x86-64.so.2 \\
  --library-path /opt/cc-harness/agent-runtime/lib \\
  /opt/cc-harness/agent-runtime/python/bin/python \\
  -c 'from cc_harness.entrypoint import main; raise SystemExit(main())' "$@"
""",
        encoding="utf-8",
        newline="\n",
    )
    (root / "uvx_impl.py").write_text(
        """#!/usr/bin/env python3
from __future__ import annotations
import os
import sys

args = sys.argv[1:]
if os.environ.get("CC_HARNESS_TERMINAL_VERIFIER_RUNTIME") != "1":
    fallback = "/root/.local/bin/uvx"
    if os.path.exists(fallback):
        os.execv(fallback, [fallback, *args])
    raise SystemExit("frozen verifier uvx wrapper is disabled")
pytest_index = None
i = 0
while i < len(args):
    value = args[i]
    if value in {"--with", "--from", "--python"}:
        i += 2
        continue
    if value.startswith(("--with=", "--from=", "--python=")):
        i += 1
        continue
    if value in {"--isolated", "--no-project", "--exact"}:
        i += 1
        continue
    if value in {"pytest", "py.test"}:
        pytest_index = i
        break
    i += 1
if pytest_index is None:
    raise SystemExit("official verifier did not invoke pytest through uvx")
os.execv(sys.executable, [sys.executable, "-m", "pytest", *args[pytest_index + 1 :]])
""",
        encoding="utf-8",
        newline="\n",
    )
    (root / "env").write_text(
        """# Frozen Terminal-Bench verifier runtime.
export CC_HARNESS_TERMINAL_VERIFIER_BOOTSTRAP=1
export CC_HARNESS_TERMINAL_VERIFIER_RUNTIME=1
export PATH=/opt/cc-harness/verifier-offline-bin:$HOME/.local/bin:$PATH
""",
        encoding="utf-8",
        newline="\n",
    )
    for path in root.iterdir():
        path.chmod(path.stat().st_mode | 0o111)


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        env=env,
        capture_output=capture_output,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _system_library(name: str) -> Path:
    for directory in (
        Path("/lib/x86_64-linux-gnu"),
        Path("/usr/lib/x86_64-linux-gnu"),
        Path("/lib64"),
    ):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"required verifier runtime library is unavailable: {name}")


__all__ = [
    "VERIFIER_OVERLAY_SCHEMA",
    "VERIFIER_RUNTIME_DIRNAME",
    "VERIFIER_RUNTIME_VERSION",
    "ensure_verifier_runtime",
    "verifier_runtime_cache_path",
    "verifier_runtime_identity",
    "verifier_runtime_overlay",
]
