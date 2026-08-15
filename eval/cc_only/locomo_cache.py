"""Validated, source-only LoCoMo pre-ingestion snapshot storage."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from .storage import atomic_json, digest_file, read_json, utc_now

CACHE_SCHEMA_VERSION = "locomo-preingestion-cache.v2"
INGESTION_CONTRACT_VERSION = "locomo-ingestion-contract.v2-fact-preserving"
CACHE_MAX_BYTES = 50 * 1024**3
_USAGE_FIELDS = (
    "wall_time_ms",
    "model_calls",
    "tool_calls",
    "input_tokens",
    "uncached_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
    "cost_microusd",
)


class CacheBusyError(RuntimeError):
    """Another live process is building the same LoCoMo snapshot."""


class CacheCapacityError(RuntimeError):
    """The retained LoCoMo cache has reached its managed-data limit."""


class CacheValidationError(ValueError):
    """A published or promoted snapshot failed admission checks."""


@dataclass(frozen=True)
class CacheIdentity:
    sample_id: str
    sample_digest: str
    model: str
    protocol_version: str
    capability_profile: str
    ingestion_contract: str
    implementation_digest: str
    memory_scope: str

    def as_dict(self) -> dict[str, str]:
        return {
            "sample_id": self.sample_id,
            "sample_digest": self.sample_digest,
            "model": self.model,
            "protocol_version": self.protocol_version,
            "capability_profile": self.capability_profile,
            "ingestion_contract": self.ingestion_contract,
            "implementation_digest": self.implementation_digest,
            "memory_scope": self.memory_scope,
        }

    @property
    def key(self) -> str:
        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CacheHit:
    root: Path
    manifest: Mapping[str, Any]

    @property
    def snapshot(self) -> Path:
        return self.root / "snapshot"

    @property
    def preparation_usage(self) -> dict[str, int]:
        return _usage(self.manifest.get("preparation_usage"))


class SnapshotBuildLock:
    def __init__(self, path: Path, *, identity: CacheIdentity) -> None:
        self.path = path
        self.identity = identity
        self.held = False

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "created_at": utc_now(),
            "cache_key": self.identity.key,
            "sample_id": self.identity.sample_id,
        }
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError as exc:
            owner = _read_lock_owner(self.path)
            if owner is not None and _pid_exists(owner):
                raise CacheBusyError(
                    f"LoCoMo snapshot build is already active for {self.identity.sample_id} "
                    f"(pid {owner})"
                ) from exc
            self.path.unlink(missing_ok=True)
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
        self.held = True
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False


class LoCoMoSnapshotStore:
    """Deep module owning cache identity, locking, publication and admission."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.root = (
            self.project_root
            / "eval"
            / "cache"
            / "cc-only"
            / "locomo-memory"
            / "deepseek-v4-flash"
        )

    def published_root(self, identity: CacheIdentity) -> Path:
        return self.root / "published" / identity.sample_id / identity.key[:16]

    def build_root(self, identity: CacheIdentity) -> Path:
        return self.root / "builds" / identity.sample_id / identity.key[:16]

    def lock(self, identity: CacheIdentity) -> SnapshotBuildLock:
        return SnapshotBuildLock(
            self.root / "locks" / f"{identity.sample_id}-{identity.key[:16]}.lock",
            identity=identity,
        )

    def ensure_capacity_for_build(self, identity: CacheIdentity) -> None:
        """Reject a new build once retained cache data reaches the soft cap."""

        if self.build_root(identity).exists():
            return
        used = _tree_size(self.root)
        if used >= CACHE_MAX_BYTES:
            raise CacheCapacityError(
                f"LoCoMo snapshot cache reached its {CACHE_MAX_BYTES} byte limit "
                f"({used} bytes); prune retained evidence before starting a new build"
            )

    def admit(
        self,
        identity: CacheIdentity,
        *,
        session_names: Sequence[str],
        expected_atom_scope: str,
    ) -> CacheHit | None:
        root = self.published_root(identity)
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            return None
        try:
            manifest = read_json(manifest_path)
            if not isinstance(manifest, Mapping):
                return None
            self._validate_manifest(
                root,
                manifest,
                identity=identity,
                session_names=session_names,
                expected_atom_scope=expected_atom_scope,
            )
        except (OSError, TypeError, ValueError, sqlite3.Error, CacheValidationError):
            return None
        return CacheHit(root=root, manifest=manifest)

    def restore_build(self, identity: CacheIdentity, attempt_root: Path) -> int:
        """Restore the highest contiguous cache-build checkpoint into an attempt."""
        source = self.build_root(identity)
        source_checkpoints = source / "ingestion-checkpoints"
        source_ingestion = source / "ingestion"
        target_checkpoints = attempt_root / "ingestion-checkpoints"
        target_ingestion = attempt_root / "ingestion"
        if not source_checkpoints.is_dir():
            return 0
        target_checkpoints.mkdir(parents=True, exist_ok=True)
        target_ingestion.mkdir(parents=True, exist_ok=True)
        completed = 0
        for checkpoint in sorted(source_checkpoints.iterdir(), key=lambda item: item.name):
            if not checkpoint.is_dir() or not checkpoint.name.isdigit():
                break
            checkpoint_index = int(checkpoint.name)
            if checkpoint_index != completed + 1:
                break
            target = target_checkpoints / checkpoint.name
            if not target.exists():
                shutil.copytree(checkpoint, target)
            ingestion_name = next(
                (
                    item.name
                    for item in source_ingestion.iterdir()
                    if item.is_dir() and item.name.startswith(f"{checkpoint_index:03d}-")
                ),
                None,
            ) if source_ingestion.is_dir() else None
            if ingestion_name is not None:
                target_ingestion_path = target_ingestion / ingestion_name
                if not target_ingestion_path.exists():
                    shutil.copytree(source_ingestion / ingestion_name, target_ingestion_path)
            completed = checkpoint_index
        return completed

    def sync_checkpoint(self, identity: CacheIdentity, attempt_root: Path, index: int) -> None:
        source_checkpoint = attempt_root / "ingestion-checkpoints" / f"{index:04d}"
        if not source_checkpoint.is_dir():
            raise CacheValidationError(f"missing attempt checkpoint {source_checkpoint}")
        target_root = self.build_root(identity)
        target_root.mkdir(parents=True, exist_ok=True)
        target_checkpoint = target_root / "ingestion-checkpoints" / f"{index:04d}"
        _replace_tree(source_checkpoint, target_checkpoint)
        source_ingestion = attempt_root / "ingestion"
        if source_ingestion.is_dir():
            for item in source_ingestion.iterdir():
                if item.is_dir() and item.name.startswith(f"{index:03d}-"):
                    _replace_tree(item, target_root / "ingestion" / item.name)

    def restore_published(self, hit: CacheHit, destination: Path) -> None:
        _replace_tree(hit.snapshot, destination)

    def publish(
        self,
        identity: CacheIdentity,
        *,
        attempt_root: Path,
        snapshot: Path,
        session_names: Sequence[str],
        preparation_usage: Mapping[str, Any],
        source: str,
    ) -> CacheHit:
        if not snapshot.is_dir():
            raise CacheValidationError(f"snapshot does not exist: {snapshot}")
        parent = self.root / "published" / identity.sample_id
        parent.mkdir(parents=True, exist_ok=True)
        staging = parent / f".{identity.key[:16]}.staging-{os.getpid()}-{time.time_ns()}"
        staging.mkdir(parents=True, exist_ok=False)
        try:
            shutil.copytree(snapshot, staging / "snapshot")
            ingestion = attempt_root / "ingestion"
            if ingestion.is_dir():
                shutil.copytree(ingestion, staging / "ingestion")
            manifest = {
                "schema_version": CACHE_SCHEMA_VERSION,
                "status": "published",
                "cache_key": identity.key,
                "identity": identity.as_dict(),
                "sample_id": identity.sample_id,
                "session_names": list(session_names),
                "session_count": len(session_names),
                "preparation_usage": _usage(preparation_usage),
                "source": source,
                "created_at": utc_now(),
                "files": _file_manifest(staging),
            }
            atomic_json(staging / "manifest.json", manifest)
            atomic_json(
                staging / "published.json",
                {"schema_version": CACHE_SCHEMA_VERSION, "published": True},
            )
            used = _tree_size(self.root)
            if used > CACHE_MAX_BYTES:
                raise CacheCapacityError(
                    f"publishing this snapshot would exceed the {CACHE_MAX_BYTES} byte "
                    f"LoCoMo cache limit ({used} bytes)"
                )
            destination = self.published_root(identity)
            if destination.exists():
                archive = (
                    self.root
                    / "archive"
                    / identity.sample_id
                    / identity.key[:16]
                    / str(time.time_ns())
                )
                archive.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(destination), str(archive))
            staging.replace(destination)
            admitted = self.admit(
                identity,
                session_names=session_names,
                expected_atom_scope=identity.memory_scope,
            )
            if admitted is None:
                raise CacheValidationError("newly published snapshot failed admission")
            self.archive_stale_versions(identity)
            return admitted
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def promote_attempt(
        self,
        identity: CacheIdentity,
        *,
        attempt_root: Path,
        session_names: Sequence[str],
        expected_atom_scope: str,
        source: str,
    ) -> CacheHit:
        snapshot = attempt_root / "snapshot"
        _validate_snapshot(snapshot, expected_atom_scope=expected_atom_scope)
        usage = _ingestion_usage(attempt_root / "ingestion", session_names)
        with self.lock(identity):
            existing = self.admit(
                identity,
                session_names=session_names,
                expected_atom_scope=expected_atom_scope,
            )
            if existing is not None:
                self.archive_stale_versions(identity)
                return existing
            return self.publish(
                identity,
                attempt_root=attempt_root,
                snapshot=snapshot,
                session_names=session_names,
                preparation_usage=usage,
                source=source,
            )

    def archive_stale_versions(self, identity: CacheIdentity) -> None:
        """Move older identities for this sample to retained archive storage."""

        parent = self.root / "published" / identity.sample_id
        if not parent.is_dir():
            return
        archive_parent = self.root / "archive" / identity.sample_id
        for sibling in sorted(parent.iterdir(), key=lambda item: item.name):
            if not sibling.is_dir() or sibling.name.startswith(".") or sibling.name == identity.key[:16]:
                continue
            archive = archive_parent / sibling.name / str(time.time_ns())
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(sibling), str(archive))

    def _validate_manifest(
        self,
        root: Path,
        manifest: Mapping[str, Any],
        *,
        identity: CacheIdentity,
        session_names: Sequence[str],
        expected_atom_scope: str,
    ) -> None:
        if manifest.get("schema_version") != CACHE_SCHEMA_VERSION:
            raise CacheValidationError("cache schema mismatch")
        if manifest.get("status") != "published":
            raise CacheValidationError("cache is not published")
        if manifest.get("cache_key") != identity.key:
            raise CacheValidationError("cache identity mismatch")
        if manifest.get("identity") != identity.as_dict():
            raise CacheValidationError("cache identity fields mismatch")
        if list(manifest.get("session_names") or []) != list(session_names):
            raise CacheValidationError("cache session coverage mismatch")
        snapshot = root / "snapshot"
        if not (root / "published.json").is_file():
            raise CacheValidationError("cache publication marker is missing")
        _validate_snapshot(snapshot, expected_atom_scope=expected_atom_scope)
        files = manifest.get("files")
        if not isinstance(files, list):
            raise CacheValidationError("cache file manifest is missing")
        for item in files:
            if not isinstance(item, Mapping):
                raise CacheValidationError("cache file manifest entry is invalid")
            relative = Path(str(item.get("path") or ""))
            candidate = (root / relative).resolve()
            if root.resolve() not in candidate.parents:
                raise CacheValidationError("cache file escapes its root")
            if not candidate.is_file():
                raise CacheValidationError(f"cache file missing: {relative}")
            expected_size = item.get("size_bytes")
            if expected_size is None or candidate.stat().st_size != int(expected_size):
                raise CacheValidationError(f"cache file size mismatch: {relative}")
            if digest_file(candidate) != item.get("sha256"):
                raise CacheValidationError(f"cache file digest mismatch: {relative}")


def implementation_digest(project_root: Path) -> str:
    paths = sorted(
        {
            path
            for path in (
                *project_root.joinpath("cc_harness").rglob("*.py"),
                project_root / "eval" / "cc_only" / "adapters" / "memory.py",
                project_root / "eval" / "cc_only" / "adapters" / "common.py",
                project_root / "eval" / "cc_only" / "launch.py",
                project_root / "eval" / "cc_only" / "contracts.py",
            )
            if (
                path.is_file()
                and "__pycache__" not in path.parts
                and not path.name.startswith("test_")
            )
        }
    )
    entries = []
    for path in paths:
        if path.is_file():
            entries.append(
                {
                    "path": path.relative_to(project_root).as_posix(),
                    "sha256": digest_file(path),
                }
            )
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_snapshot(snapshot: Path, *, expected_atom_scope: str) -> None:
    marker = snapshot / "checkpoint.json"
    if not marker.is_file():
        raise CacheValidationError("snapshot completion marker is missing")
    payload = read_json(marker)
    if payload.get("complete") is not True:
        raise CacheValidationError("snapshot is incomplete")
    workspace = snapshot / "workspace-state"
    home = snapshot / "home-state"
    if not workspace.is_dir() or not home.is_dir():
        raise CacheValidationError("snapshot state directories are missing")
    database = workspace / "memory.db"
    if not database.is_file():
        raise CacheValidationError("snapshot memory database is missing")
    connection = sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
        if "memories" not in tables:
            raise CacheValidationError("snapshot memory table is missing")
        active = int(
            connection.execute(
                "select count(*) from memories where validity = 'active' and project_scope = ?",
                (expected_atom_scope,),
            ).fetchone()[0]
        )
        if active <= 0:
            raise CacheValidationError("snapshot has no active memory atom in the expected scope")
        if "conversation" in tables and int(connection.execute("select count(*) from conversation").fetchone()[0]) <= 0:
            raise CacheValidationError("snapshot has no conversation events")
    finally:
        connection.close()
    forbidden = {"qa", "grading", "result.json", "prediction.json"}
    for path in snapshot.rglob("*"):
        if path.name.casefold() in forbidden:
            raise CacheValidationError(f"snapshot contains forbidden evaluation material: {path.name}")


def _file_manifest(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": digest_file(path),
            "size_bytes": path.stat().st_size,
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "manifest.json" and not _is_ephemeral_file(path)
    ]


def _is_ephemeral_file(path: Path) -> bool:
    """Return whether a file is a transient SQLite sidecar, not snapshot state."""

    return path.name in {"memory.db-wal", "memory.db-shm"}


def _tree_size(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _ingestion_usage(root: Path, session_names: Sequence[str]) -> dict[str, int]:
    total = _usage(None)
    if not root.is_dir():
        return total
    for index, session_name in enumerate(session_names, 1):
        evidence = root / f"{index:03d}-{session_name}" / "launch.json"
        if not evidence.is_file():
            continue
        payload = read_json(evidence)
        for field in _USAGE_FIELDS:
            total[field] += int(payload.get(field) or 0)
    return total


def _usage(value: Mapping[str, Any] | None) -> dict[str, int]:
    return {field: int((value or {}).get(field) or 0) for field in _USAGE_FIELDS}


def _replace_tree(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def _read_lock_owner(path: Path) -> int | None:
    try:
        value = read_json(path).get("pid")
        return int(value) if value is not None else None
    except (OSError, TypeError, ValueError):
        return None


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True
