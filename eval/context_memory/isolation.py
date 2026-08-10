"""Per-benchmark, per-task and per-arm runtime isolation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from eval.cc_only.storage import atomic_json, digest_file, utc_now


@dataclass(frozen=True)
class IsolatedRuntime:
    active_root: Path
    workspace: Path
    home: Path
    namespace: str


def open_runtime(attempt_root: Path, namespace: str, *, resumed: bool) -> IsolatedRuntime:
    active = attempt_root / "active"
    owner = active / "eval-owner.json"
    sealed = attempt_root / "sealed-state"
    if sealed.exists():
        raise ValueError(f"sealed trial cannot be reopened: {sealed}")
    if resumed:
        if not owner.is_file():
            raise ValueError("interrupted trial has no isolation owner record")
        import json

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
    return IsolatedRuntime(active, workspace, home, namespace)


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
    sealed = attempt_root / "sealed-state"
    os.replace(runtime.active_root, sealed)
    return sealed


def verify_sealed_runtime(sealed: Path) -> tuple[bool, list[str]]:
    manifest_path = sealed / "seal.json"
    if not manifest_path.is_file():
        return False, ["missing-seal-manifest"]
    import json

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
