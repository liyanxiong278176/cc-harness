"""Atomic, resumable state for treatment-only context-memory runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eval.cc_only.storage import (
    atomic_json,
    digest_file,
    digest_json,
    read_json,
    safe_slug,
    utc_now,
)

from .contracts import Arm, BenchmarkTask, ExecutionStatus
from .isolation import verify_sealed_runtime

STATE_VERSION = "eval.context-memory-state.v2"
MANIFEST_VERSION = "eval.context-memory-manifest.v2"
CATALOG_VERSION = "eval.context-memory-catalog.v1"


class TreatmentStateStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.manifest_path = self.root / "manifest.json"
        self.catalog_path = self.root / "catalog.json"
        self.state_path = self.root / "state.json"

    def initialize(
        self, *, contract: dict[str, Any], tasks: tuple[BenchmarkTask, ...]
    ) -> dict[str, Any]:
        catalog = {
            "schema_version": CATALOG_VERSION,
            "tasks": [task.as_dict() for task in tasks],
        }
        catalog["catalog_digest"] = digest_json(catalog["tasks"])
        immutable = {
            **contract,
            "schema_version": MANIFEST_VERSION,
            "catalog_digest": catalog["catalog_digest"],
            "task_count": len(tasks),
            "arms": [Arm.TREATMENT.value],
            "execution_mode": "treatment-only",
            "attempts_per_arm": 1,
        }
        contract_digest = digest_json(immutable)
        if self.manifest_path.is_file():
            manifest = read_json(self.manifest_path)
            if manifest.get("contract_digest") != contract_digest:
                raise ValueError(
                    "existing context-memory result root has a different immutable "
                    "contract; use the original command or a new result root"
                )
            existing_catalog = read_json(self.catalog_path)
            if existing_catalog.get("catalog_digest") != catalog["catalog_digest"]:
                raise ValueError("frozen context-memory catalog changed")
            state = read_json(self.state_path)
            self._recover_interrupted(state)
            self.save(state)
            return state

        self._validate_new_root()
        for name in ("raw", "normalized", "preflight", "frozen-inputs", "canary"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        manifest = {
            **immutable,
            "contract_digest": contract_digest,
            "created_at": utc_now(),
        }
        state = {
            "schema_version": STATE_VERSION,
            "contract_digest": contract_digest,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "preflight": {"status": ExecutionStatus.PENDING.value},
            "canaries": {},
            "trials": {
                task.task_id: {
                    arm.value: {
                        "status": ExecutionStatus.PENDING.value,
                        "attempt": 1,
                    }
                    for arm in Arm
                }
                for task in tasks
            },
        }
        atomic_json(self.catalog_path, catalog)
        atomic_json(self.root / "frozen-inputs" / "catalog.json", catalog)
        atomic_json(self.manifest_path, manifest)
        atomic_json(self.state_path, state)
        return state

    def save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = utc_now()
        atomic_json(self.state_path, state)

    def begin(
        self,
        state: dict[str, Any],
        task: BenchmarkTask,
        arm: Arm,
        sequence: int,
    ) -> tuple[Path, dict[str, Any], bool]:
        record = state["trials"][task.task_id][arm.value]
        attempt_root = (
            self.root
            / "raw"
            / f"{sequence:04d}-{safe_slug(task.task_id)}"
            / arm.value
            / "attempt-1"
        )
        resumed = attempt_root.exists()
        if not resumed:
            attempt_root.mkdir(parents=True)
            record["path"] = attempt_root.relative_to(self.root).as_posix()
            record["started_at"] = utc_now()
        record["status"] = ExecutionStatus.RUNNING.value
        record["resumed"] = resumed
        self.save(state)
        return attempt_root, record, resumed

    def interrupt(
        self, state: dict[str, Any], task_id: str, arm: Arm, record: dict[str, Any]
    ) -> None:
        record["status"] = ExecutionStatus.INTERRUPTED.value
        record["interrupted_at"] = utc_now()
        state["trials"][task_id][arm.value] = record
        self.save(state)

    def finish(
        self,
        state: dict[str, Any],
        task_id: str,
        arm: Arm,
        record: dict[str, Any],
        result_path: Path,
        status: ExecutionStatus,
        integrity_path: Path,
    ) -> None:
        record["status"] = status.value
        record["finished_at"] = utc_now()
        record["result"] = result_path.relative_to(self.root).as_posix()
        record["result_digest"] = digest_file(result_path)
        record["attempt_integrity"] = integrity_path.relative_to(self.root).as_posix()
        record["attempt_integrity_digest"] = digest_file(integrity_path)
        state["trials"][task_id][arm.value] = record
        self.save(state)

    def selected_result(
        self, state: dict[str, Any], task_id: str, arm: Arm
    ) -> dict[str, Any] | None:
        record = state["trials"][task_id][arm.value]
        if record.get("status") not in {
            ExecutionStatus.COMPLETE.value,
            ExecutionStatus.INVALID.value,
            ExecutionStatus.UNSUPPORTED.value,
        }:
            return None
        relative = record.get("result")
        if not isinstance(relative, str):
            raise TypeError(f"terminal trial lacks result path: {task_id}.{arm.value}")
        path = self.root / relative
        integrity_relative = record.get("attempt_integrity")
        integrity = self.root / integrity_relative if isinstance(integrity_relative, str) else None
        verified, errors = verify_attempt_integrity(
            integrity,
            expected_manifest_digest=record.get("attempt_integrity_digest"),
        )
        if not path.is_file() or digest_file(path) != record.get("result_digest") or not verified:
            record["status"] = ExecutionStatus.INVALID.value
            record["integrity_error"] = errors or ["result-digest-mismatch"]
            self.save(state)
            return {
                "schema_version": "eval.context-memory-arm-outcome.v1",
                "status": ExecutionStatus.INVALID.value,
                "prediction": None,
                "metrics": {},
                "usage": {},
                "protocol": {"tamper_detected": True},
                "invalid_reason": "terminal trial evidence failed integrity",
                "task_id": task_id,
                "arm": arm.value,
            }
        return read_json(path)

    def _validate_new_root(self) -> None:
        if not self.root.exists():
            self.root.mkdir(parents=True)
            return
        if not self.root.is_dir() or any(self.root.iterdir()):
            raise ValueError(f"new context-memory result root is not empty: {self.root}")

    @staticmethod
    def _recover_interrupted(state: dict[str, Any]) -> None:
        if (state.get("preflight") or {}).get("status") == ExecutionStatus.RUNNING.value:
            state["preflight"]["status"] = ExecutionStatus.PENDING.value
        for trial in state.get("trials", {}).values():
            for record in trial.values():
                if record.get("status") == ExecutionStatus.RUNNING.value:
                    record["status"] = ExecutionStatus.INTERRUPTED.value
                    record["recovered_on_resume"] = True


# Keep the old import name as a compatibility shim for callers that only use the
# storage API. It now creates and validates treatment-only state and never
# creates a control record.
PairedStateStore = TreatmentStateStore


def write_attempt_integrity(attempt_root: Path) -> Path:
    sealed = attempt_root / "sealed-state"
    sealed_ok, sealed_errors = verify_sealed_runtime(sealed)
    if not sealed_ok:
        raise ValueError(f"cannot finalize invalid sealed runtime: {sealed_errors}")
    manifest_path = attempt_root / "attempt-integrity.json"
    paths = [
        path
        for path in sorted(attempt_root.rglob("*"))
        if path.is_file() and path != manifest_path and not path.is_relative_to(sealed)
    ]
    atomic_json(
        manifest_path,
        {
            "schema_version": "eval.context-memory-attempt-integrity.v1",
            "sealed_manifest": {
                "path": "sealed-state/seal.json",
                "sha256": digest_file(sealed / "seal.json"),
            },
            "files": [
                {
                    "path": path.relative_to(attempt_root).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": digest_file(path),
                }
                for path in paths
            ],
        },
    )
    return manifest_path


def verify_attempt_integrity(
    manifest_path: Path | None,
    *,
    expected_manifest_digest: Any = None,
) -> tuple[bool, list[str]]:
    if manifest_path is None or not manifest_path.is_file():
        return False, ["missing-attempt-integrity"]
    if expected_manifest_digest and digest_file(manifest_path) != expected_manifest_digest:
        return False, ["attempt-integrity-digest-mismatch"]
    attempt_root = manifest_path.parent
    manifest = read_json(manifest_path)
    errors = []
    sealed_ok, sealed_errors = verify_sealed_runtime(attempt_root / "sealed-state")
    if not sealed_ok:
        errors.extend(f"sealed:{error}" for error in sealed_errors)
    sealed_manifest = manifest.get("sealed_manifest") or {}
    seal = attempt_root / str(sealed_manifest.get("path") or "")
    if not seal.is_file() or digest_file(seal) != sealed_manifest.get("sha256"):
        errors.append("sealed-manifest-digest-mismatch")
    for item in manifest.get("files") or []:
        path = attempt_root / str(item.get("path") or "")
        if not path.is_file():
            errors.append(f"missing:{item.get('path')}")
        elif path.stat().st_size != item.get("size_bytes") or digest_file(path) != item.get(
            "sha256"
        ):
            errors.append(f"digest-mismatch:{item.get('path')}")
    return not errors, errors
