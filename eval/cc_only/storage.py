"""Atomic state and immutable-input handling for resumable benchmark runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.core import canonical_json_bytes

from .contracts import BenchmarkTask, TrialStatus

STATE_VERSION = "eval.cc-only-state.v1"
MANIFEST_VERSION = "eval.cc-only-run-manifest.v1"
CATALOG_VERSION = "eval.cc-only-catalog.v1"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def digest_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(path, canonical_json_bytes(value))


def atomic_text(path: Path, value: str) -> None:
    atomic_bytes(path, value.encode("utf-8"))


def _replace_with_retry(source: Path, target: Path) -> None:
    """Replace a file, tolerating short-lived Windows sharing violations."""
    for attempt in range(20):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(min(0.01 * (attempt + 1), 0.1))


def atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temporary, path)
    finally:
        # ``os.replace`` removes the temporary name on success.  If writing or
        # replacing fails, clean up the unique file so retries do not inherit
        # stale state.
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON document must be an object: {path}")
    return value


def safe_slug(value: str, *, maximum: int = 96) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-.")
    return (slug or "task")[:maximum]


class RunStateStore:
    def __init__(self, output_root: Path) -> None:
        self.root = output_root.resolve()
        self.manifest_path = self.root / "manifest.json"
        self.catalog_path = self.root / "catalog.json"
        self.state_path = self.root / "state.json"

    def initialize(
        self,
        *,
        contract: dict[str, Any],
        tasks: tuple[BenchmarkTask, ...],
    ) -> dict[str, Any]:
        self._validate_target()
        catalog = {
            "schema_version": CATALOG_VERSION,
            "tasks": [task.as_dict() for task in tasks],
        }
        catalog["catalog_digest"] = digest_json(catalog["tasks"])
        immutable_contract = {
            **contract,
            "schema_version": MANIFEST_VERSION,
            "catalog_digest": catalog["catalog_digest"],
            "task_count": len(tasks),
        }
        contract_digest = digest_json(immutable_contract)

        if self.manifest_path.is_file():
            manifest = read_json(self.manifest_path)
            if manifest.get("contract_digest") != contract_digest:
                raise ValueError(
                    "existing result root has a different immutable input contract; "
                    "use the original command or a different profile/result root"
                )
            existing_catalog = read_json(self.catalog_path)
            if existing_catalog.get("catalog_digest") != catalog["catalog_digest"]:
                raise ValueError("existing frozen catalog does not match the current catalog")
            state = read_json(self.state_path)
            self._recover_interrupted(state)
            atomic_json(self.state_path, state)
            return state

        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("raw", "preflight", "frozen-inputs"):
            (self.root / name).mkdir(exist_ok=True)
        manifest = {
            **immutable_contract,
            "contract_digest": contract_digest,
            "created_at": utc_now(),
        }
        state = {
            "schema_version": STATE_VERSION,
            "contract_digest": contract_digest,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "retry_generation": 0,
            "preflight": {"status": "pending", "attempts": []},
            "trials": {
                task.task_id: {
                    "status": "pending",
                    "attempts": [],
                    "selected_attempt": None,
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

    def begin_attempt(
        self, state: dict[str, Any], task: BenchmarkTask, sequence: int
    ) -> tuple[int, Path, dict[str, Any]]:
        trial = state["trials"][task.task_id]
        # An interrupted attempt owns durable per-item evidence (for example a
        # LoCoMo ingestion checkpoint).  Reuse that attempt on the next
        # invocation so the adapter can continue in place instead of creating a
        # new attempt and replaying the whole sample.
        if trial.get("attempts"):
            previous = trial["attempts"][-1]
            previous_path = previous.get("path")
            reusable_invalid = False
            if previous.get("status") == TrialStatus.INVALID.value and isinstance(
                previous_path, str
            ):
                previous_result = self.root / Path(previous_path) / "result.json"
                try:
                    payload = read_json(previous_result)
                    reusable_invalid = bool(
                        (payload.get("protocol") or {}).get("checkpoint_preserving")
                    )
                except (OSError, TypeError, ValueError):
                    reusable_invalid = False
            if (
                previous.get("status") == TrialStatus.INTERRUPTED.value or reusable_invalid
            ) and isinstance(previous_path, str):
                attempt_root = self.root / Path(previous_path)
                result_path = attempt_root / "result.json"
                if reusable_invalid and result_path.is_file():
                    retained = attempt_root / "infrastructure-results"
                    retained.mkdir(parents=True, exist_ok=True)
                    sequence_number = len(list(retained.glob("result-*.json"))) + 1
                    os.replace(result_path, retained / f"result-{sequence_number:04d}.json")
                    previous.setdefault("infrastructure_failures", []).append(
                        {
                            "result": (
                                retained / f"result-{sequence_number:04d}.json"
                            ).relative_to(self.root).as_posix(),
                            "retained_at": utc_now(),
                        }
                    )
                if attempt_root.is_dir() and not result_path.exists():
                    previous["status"] = "running"
                    previous["resumed_at"] = utc_now()
                    previous["resume_count"] = int(previous.get("resume_count", 0)) + 1
                    trial["status"] = "running"
                    self.save(state)
                    return int(previous["attempt"]), attempt_root, previous
        attempt = len(trial["attempts"]) + 1
        attempt_root = (
            self.root
            / "raw"
            / f"{sequence:04d}-{safe_slug(task.task_id)}"
            / f"attempt-{attempt}"
        )
        if attempt_root.exists():
            raise FileExistsError(f"attempt directory already exists but is unjournaled: {attempt_root}")
        attempt_root.mkdir(parents=True)
        record = {
            "attempt": attempt,
            "status": "running",
            "started_at": utc_now(),
            "path": attempt_root.relative_to(self.root).as_posix(),
            "retry_generation": int(state.get("retry_generation", 0)),
        }
        trial["status"] = "running"
        trial["attempts"].append(record)
        self.save(state)
        return attempt, attempt_root, record

    def finish_attempt(
        self,
        state: dict[str, Any],
        task_id: str,
        record: dict[str, Any],
        status: TrialStatus,
        result_path: Path,
    ) -> None:
        record["status"] = status.value
        record["finished_at"] = utc_now()
        record["result"] = result_path.relative_to(self.root).as_posix()
        trial = state["trials"][task_id]
        trial["status"] = status.value
        trial["selected_attempt"] = record["attempt"]
        trial["result"] = record["result"]
        self.save(state)

    def interrupt_attempt(
        self, state: dict[str, Any], task_id: str, record: dict[str, Any]
    ) -> None:
        record["status"] = TrialStatus.INTERRUPTED.value
        record["finished_at"] = utc_now()
        state["trials"][task_id]["status"] = "pending"
        self.save(state)

    def _validate_target(self) -> None:
        if not self.root.exists():
            return
        if not self.root.is_dir():
            raise ValueError(f"result root is not a directory: {self.root}")
        contents = list(self.root.iterdir())
        if contents and not all(
            path.is_file()
            for path in (self.manifest_path, self.catalog_path, self.state_path)
        ):
            raise ValueError(
                f"result root is nonempty but lacks a complete cc-only state: {self.root}"
            )

    @staticmethod
    def _recover_interrupted(state: dict[str, Any]) -> None:
        preflight = state.get("preflight") or {}
        if preflight.get("status") == "running":
            attempts = preflight.get("attempts") or []
            if attempts and attempts[-1].get("status") == "running":
                attempts[-1]["status"] = TrialStatus.INTERRUPTED.value
                attempts[-1]["finished_at"] = utc_now()
                attempts[-1]["recovered_on_resume"] = True
            preflight["status"] = "pending"
        for trial in state.get("trials", {}).values():
            if trial.get("status") != "running":
                continue
            attempts = trial.get("attempts") or []
            if attempts and attempts[-1].get("status") == "running":
                attempts[-1]["status"] = TrialStatus.INTERRUPTED.value
                attempts[-1]["finished_at"] = utc_now()
                attempts[-1]["recovered_on_resume"] = True
            trial["status"] = "pending"
