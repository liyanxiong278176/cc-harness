"""Per-benchmark, per-task and per-arm runtime isolation."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from eval.cc_only.storage import atomic_json, digest_file, utc_now


@dataclass(frozen=True)
class IsolatedRuntime:
    active_root: Path
    workspace: Path
    home: Path
    namespace: str
    snapshot_root: Path


def open_runtime(attempt_root: Path, namespace: str, *, resumed: bool) -> IsolatedRuntime:
    default_active = attempt_root / "active"
    active = default_active
    if _needs_short_runtime(attempt_root):
        short_active = _short_runtime_root(attempt_root, namespace)
        # A run interrupted before this Windows-path fix may already have an
        # owner under the deep default path. Move that whole tree atomically
        # before reopening it, preserving every partial artifact.
        if resumed and default_active.exists() and not short_active.exists():
            short_active.parent.mkdir(parents=True, exist_ok=True)
            os.replace(default_active, short_active)
        if not default_active.exists() or resumed:
            active = short_active
    owner = active / "eval-owner.json"
    sealed = attempt_root / "sealed-state"
    if sealed.exists():
        raise ValueError(f"sealed trial cannot be reopened: {sealed}")
    if resumed:
        if not owner.is_file():
            raise ValueError("interrupted trial has no isolation owner record")
        payload = json.loads(owner.read_text(encoding="utf-8"))
        if payload.get("namespace") != namespace:
            raise ValueError("interrupted trial namespace mismatch")
    else:
        if active.exists() and any(active.iterdir()):
            raise ValueError(f"new trial active state is not empty: {active}")
        active.mkdir(parents=True, exist_ok=True)
        atomic_json(
            owner,
            {
                "schema_version": "eval.context-memory-owner.v1",
                "namespace": namespace,
                "created_at": utc_now(),
            },
        )
    workspace = active / "workspace"
    home = active / "home"
    workspace.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    snapshot_root = (
        active / "query-snapshot"
        if active != default_active
        else attempt_root / "query-snapshot"
    )
    return IsolatedRuntime(active, workspace, home, namespace, snapshot_root)


def _needs_short_runtime(attempt_root: Path) -> bool:
    """Keep cc-harness's nested session/offload paths below Windows MAX_PATH."""

    if os.name != "nt":
        return False
    probe = (
        attempt_root
        / "active"
        / "workspace"
        / ".cc-harness"
        / "context"
        / ("session-" + "0" * 32)
        / "offload"
        / "refs"
    )
    return len(os.fspath(probe)) >= 220


def _short_runtime_root(attempt_root: Path, namespace: str) -> Path:
    digest = hashlib.sha256(
        f"{attempt_root.resolve()}:{namespace}".encode()
    ).hexdigest()[:24]
    return Path(attempt_root.anchor) / ".cm-runtime" / digest


def resolve_sealed_root(sealed: Path) -> Path:
    """Resolve an attempt-local sealed marker to its short external root."""

    pointer = sealed / "pointer.json"
    if not pointer.is_file():
        return sealed
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    external = payload.get("external_path")
    if not isinstance(external, str) or not Path(external).is_absolute():
        raise ValueError(f"sealed runtime pointer is invalid: {pointer}")
    return Path(external)


def seal_runtime(runtime: IsolatedRuntime, attempt_root: Path) -> Path:
    owner = runtime.active_root / "eval-owner.json"
    if not owner.is_file():
        raise ValueError("active runtime lost its isolation owner")
    files = []
    for path in sorted(runtime.active_root.rglob("*")):
        if path.is_symlink():
            target = path.resolve()
            try:
                target.relative_to(runtime.active_root.resolve())
            except ValueError as exc:
                raise ValueError(f"runtime contains escaping symlink: {path}") from exc
        if path.is_file():
            files.append(
                {
                    "path": path.relative_to(runtime.active_root).as_posix(),
                    "sha256": digest_file(path),
                    "size_bytes": path.stat().st_size,
                }
            )
    atomic_json(
        runtime.active_root / "seal.json",
        {
            "schema_version": "eval.context-memory-seal.v1",
            "namespace": runtime.namespace,
            "sealed_at": utc_now(),
            "files": files,
        },
    )
    if runtime.active_root == attempt_root / "active":
        sealed = attempt_root / "sealed-state"
        os.replace(runtime.active_root, sealed)
        return sealed

    # Keep a long-lived short root on Windows. Moving it into the deep result
    # tree would make the sealed files unaddressable again under MAX_PATH.
    sealed = runtime.active_root.with_name(runtime.active_root.name + "-sealed")
    if sealed.exists():
        # A canary or a resumed attempt can leave the deterministic sealed
        # name behind after a process crash. Preserve that evidence and use a
        # fresh external name for the runtime being sealed now.
        sealed = runtime.active_root.with_name(
            runtime.active_root.name + "-sealed-" + uuid.uuid4().hex[:12]
        )
    os.replace(runtime.active_root, sealed)
    marker = attempt_root / "sealed-state"
    marker.mkdir(parents=True, exist_ok=False)
    atomic_json(
        marker / "pointer.json",
        {
            "schema_version": "eval.context-memory-sealed-pointer.v1",
            "external_path": str(sealed),
        },
    )
    return sealed


def verify_sealed_runtime(sealed: Path) -> tuple[bool, list[str]]:
    try:
        sealed = resolve_sealed_root(sealed)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return False, [f"invalid-sealed-pointer:{exc}"]
    manifest_path = sealed / "seal.json"
    if not manifest_path.is_file():
        return False, ["missing-seal-manifest"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, ["invalid-seal-manifest"]
    errors = []
    for item in manifest.get("files") or []:
        relative = item.get("path")
        if not isinstance(relative, str):
            errors.append("invalid-seal-entry")
            continue
        path = sealed / relative
        if not path.is_file():
            errors.append(f"missing:{relative}")
            continue
        if path.stat().st_size != item.get("size_bytes") or digest_file(path) != item.get("sha256"):
            errors.append(f"digest-mismatch:{relative}")
    return not errors, errors
