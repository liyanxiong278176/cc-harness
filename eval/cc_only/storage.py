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


def _observability_resume_compatible(
    manifest: dict[str, Any], immutable_contract: dict[str, Any]
) -> bool:
    """Allow an explicitly authorized resume after liveness-only instrumentation.

    The adapter identity includes a dirty-worktree digest.  That is useful for
    preventing accidental result mixing, but it also blocks resuming a run
    when the only code change is supervisor observability.  Require an explicit
    opt-in and compare every frozen field except that digest.  A Docker Desktop
    restart can also change the daemon's display name, API patch/minor version,
    or the implicit value of ``DOCKER_HOST`` while keeping the same native
    Linux socket and ext4 data root.  Those are runtime observations rather
    than scoring inputs, so normalize only that narrowly-defined native-daemon
    drift.  Dataset, model, task/verifier inputs, wheel, and time budgets stay
    frozen.  A narrowly-scoped runtime repair may also change the Harbor
    plugin (for example, to repair an agent-container trust store before the
    official verifier runs), but only with the explicit
    ``CC_HARNESS_ALLOW_RESUME_RUNTIME_REPAIR=1`` opt-in.  The repair is
    recorded in ``resume-compatibility.json``; task data and verifier inputs
    remain frozen.
    """

    if os.environ.get("CC_HARNESS_ALLOW_OBSERVABILITY_RESUME") != "1":
        return False
    if manifest.get("benchmark") != "terminal-bench-2.1":
        return False
    previous = {
        key: value
        for key, value in manifest.items()
        if key not in {"contract_digest", "created_at"}
    }
    current = dict(immutable_contract)
    previous_identity = previous.get("adapter_run_identity")
    current_identity = current.get("adapter_run_identity")
    if not isinstance(previous_identity, dict) or not isinstance(current_identity, dict):
        return False
    previous_identity = dict(previous_identity)
    current_identity = dict(current_identity)
    previous_identity.pop("git_dirty_digest", None)
    current_identity.pop("git_dirty_digest", None)
    if previous_identity.get("harbor_plugin_sha256") != current_identity.get(
        "harbor_plugin_sha256"
    ):
        if os.environ.get("CC_HARNESS_ALLOW_RESUME_RUNTIME_REPAIR") != "1":
            return False
        previous_identity.pop("harbor_plugin_sha256", None)
        current_identity.pop("harbor_plugin_sha256", None)
    if previous_identity.get("wheel_sha256") != current_identity.get("wheel_sha256"):
        # A functional harness repair is shipped through the frozen wheel.  It
        # is allowed only with a second, explicit opt-in; the ordinary
        # observability resume flag must never silently change the agent code
        # used by an existing benchmark run.
        if os.environ.get("CC_HARNESS_ALLOW_RESUME_ARTIFACT_REFRESH") != "1":
            return False
        previous_identity.pop("wheel_sha256", None)
        current_identity.pop("wheel_sha256", None)
    backends: list[dict[str, Any]] = []
    for identity in (previous_identity, current_identity):
        backend = identity.get("execution_backend")
        if not isinstance(backend, dict):
            continue
        backends.append(backend)
        storage = backend.get("docker_storage")
        if not isinstance(storage, dict):
            continue
        source = storage.get("source")
        filesystem = storage.get("filesystem")
        target = storage.get("target")
        if not isinstance(source, str) or filesystem != "ext4":
            continue

        # Docker Desktop's WSL integration reports the same ext4 data volume
        # in two equivalent ways across a distro restart:
        #   /dev/sdf[/var/lib/docker] mounted at /var/lib/docker
        #   /dev/sdf mounted at / (with DockerRootDir=/var/lib/docker)
        # Treat only these native Docker-root forms as equivalent.  Do not
        # weaken the rest of the immutable backend identity (daemon name,
        # version, socket, etc.).
        source_device = source.split("[", 1)[0]
        if target not in {"/", "/var/lib/docker"}:
            continue
        if not re.fullmatch(r"/dev/sd[a-z]+", source_device):
            continue
        normalized_backend = dict(backend)
        normalized_storage = dict(storage)
        normalized_storage["source"] = "/dev/sd*"
        normalized_storage["target"] = "/var/lib/docker"
        normalized_backend["docker_storage"] = normalized_storage
        identity["execution_backend"] = normalized_backend

    # Docker Desktop may recreate the native daemon during a restart or
    # upgrade.  The daemon name and server version are not benchmark inputs;
    # requiring the stable native checks above prevents accepting a remote or
    # non-Linux daemon under this compatibility path.  Treat an omitted host
    # (Docker's default) and the native Unix socket as equivalent.
    if len(backends) == 2:
        for identity in (previous_identity, current_identity):
            backend = identity.get("execution_backend")
            if not isinstance(backend, dict):
                continue
            checks = backend.get("checks")
            storage = backend.get("docker_storage")
            if not isinstance(checks, dict) or not isinstance(storage, dict):
                continue
            required_checks = (
                "linux",
                "wsl2",
                "docker_binary_native",
                "docker_context_default",
                "docker_daemon_ready",
                "docker_server_linux",
                "docker_root_native",
                "docker_socket_native",
                "docker_storage_ext",
            )
            if not all(checks.get(name) is True for name in required_checks):
                continue
            normalized_backend = dict(backend)
            normalized_backend["docker_name"] = "<native-linux-daemon>"
            normalized_backend["docker_server_version"] = "<native-linux-daemon>"
            if backend.get("docker_host") in {None, "", "unix:///var/run/docker.sock"}:
                normalized_backend["docker_host"] = "unix:///var/run/docker.sock"
            identity["execution_backend"] = normalized_backend
    previous["adapter_run_identity"] = previous_identity
    current["adapter_run_identity"] = current_identity
    return previous == current


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


def task_path_slug(value: str, *, maximum: int = 12) -> str:
    """Return a readable, collision-resistant slug safe for deep trial paths.

    Windows creates temporary files below each trial workspace (for example,
    activation manifests).  Keeping the task id's full slug in that path can
    exceed the legacy MAX_PATH limit before the model process even starts.
    Long ids therefore keep a short readable prefix plus a digest, while short
    ids remain unchanged for backwards-compatible resume paths.
    """
    if maximum < 12:
        raise ValueError("task path slug maximum must be at least 12")
    full = safe_slug(value)
    if len(full) <= maximum:
        return full
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    prefix = safe_slug(value, maximum=maximum - len(digest) - 1)
    return f"{prefix}-{digest}"


# Keep enough budget for Harbor's appended timestamp/task/artifacts suffix on
# both /mnt/d and WSL-local temporary roots.  The previous 150-character
# threshold made compaction depend on the host's pytest temp prefix: a legacy
# task path could pass the check in WSL and still exceed the Windows-compatible
# budget once Harbor appended its nested job directories.
_HARBOR_JOBS_ROOT_MAX_CHARS = 100


def _compact_interrupted_attempt_path(
    root: Path,
    attempt_root: Path,
    *,
    sequence: int,
    task: BenchmarkTask,
    attempt: int,
) -> tuple[Path, bool]:
    """Move an old interrupted attempt to a short path before Harbor resumes it.

    Harbor appends a timestamp, task slug and ``artifacts/logs/artifacts`` to
    ``--jobs-dir``.  On Windows that suffix can exceed the legacy MAX_PATH
    limit even though the cc-only attempt path itself is valid.  Resumable
    attempts created by older versions may still carry the long slug, so
    compact them in-place while retaining the same attempt number and all
    evidence.
    """
    jobs_root = attempt_root / "jobs"
    if len(str(jobs_root)) <= _HARBOR_JOBS_ROOT_MAX_CHARS:
        return attempt_root, False
    compact_root = (
        root
        / "raw"
        / f"{sequence:04d}-{task_path_slug(task.task_id)}"
        / f"attempt-{attempt}"
    )
    if compact_root == attempt_root:
        return attempt_root, False
    if compact_root.exists():
        raise FileExistsError(
            "short resumable attempt path already exists; refusing to merge evidence: "
            f"{compact_root}"
        )
    compact_root.parent.mkdir(parents=True, exist_ok=True)
    attempt_root.replace(compact_root)
    return compact_root, True


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
                if not _observability_resume_compatible(manifest, immutable_contract):
                    raise ValueError(
                        "existing result root has a different immutable input contract; "
                        "use the original command or a different profile/result root"
                    )
                previous_digest = str(manifest.get("contract_digest") or "")
                atomic_json(
                    self.root / "resume-compatibility.json",
                    {
                        "schema_version": "eval.cc-only-observability-resume.v1",
                        "reason": (
                            "liveness-observability-and-authorized-artifact-refresh"
                            if os.environ.get("CC_HARNESS_ALLOW_RESUME_ARTIFACT_REFRESH")
                            == "1"
                            else "liveness-observability-only-change"
                        ),
                        "authorized_by": [
                            "CC_HARNESS_ALLOW_OBSERVABILITY_RESUME=1",
                            *(
                                ["CC_HARNESS_ALLOW_RESUME_RUNTIME_REPAIR=1"]
                                if os.environ.get("CC_HARNESS_ALLOW_RESUME_RUNTIME_REPAIR")
                                == "1"
                                else []
                            ),
                            *(
                                ["CC_HARNESS_ALLOW_RESUME_ARTIFACT_REFRESH=1"]
                                if os.environ.get("CC_HARNESS_ALLOW_RESUME_ARTIFACT_REFRESH")
                                == "1"
                                else []
                            ),
                        ],
                        "previous_contract_digest": previous_digest,
                        "current_contract_digest": contract_digest,
                        "recorded_at": utc_now(),
                    },
                )
                contract_digest = previous_digest
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
        self,
        state: dict[str, Any],
        task: BenchmarkTask,
        sequence: int,
        *,
        reuse_interrupted: bool = True,
    ) -> tuple[int, Path, dict[str, Any]]:
        trial = state["trials"][task.task_id]
        # An interrupted attempt owns durable per-item evidence (for example a
        # LoCoMo ingestion checkpoint).  Reuse that attempt on the next
        # invocation so the adapter can continue in place instead of creating a
        # new attempt and replaying the whole sample.
        if reuse_interrupted and trial.get("attempts"):
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
                attempt_root, previous_path_changed = _compact_interrupted_attempt_path(
                    self.root,
                    attempt_root,
                    sequence=sequence,
                    task=task,
                    attempt=int(previous.get("attempt") or 1),
                )
                if previous_path_changed:
                    previous["path"] = attempt_root.relative_to(self.root).as_posix()
                result_path = attempt_root / "result.json"
                infrastructure_result = attempt_root / "infrastructure-result.json"
                if infrastructure_result.is_file():
                    retained = attempt_root / "infrastructure-results"
                    retained.mkdir(parents=True, exist_ok=True)
                    sequence_number = len(list(retained.glob("infrastructure-*.json"))) + 1
                    os.replace(
                        infrastructure_result,
                        retained / f"infrastructure-{sequence_number:04d}.json",
                    )
                    previous.setdefault("infrastructure_failures", []).append(
                        {
                            "result": (
                                retained / f"infrastructure-{sequence_number:04d}.json"
                            ).relative_to(self.root).as_posix(),
                            "retained_at": utc_now(),
                        }
                    )
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
            / f"{sequence:04d}-{task_path_slug(task.task_id)}"
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
